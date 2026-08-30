"""
A stand-in for the ESP32, for testing the server without hardware.

Speaks the exact protocol in protocol.py: streams sensor readings, acknowledges
brightness commands, can send a recorded WAV as if the button had been pressed, and
captures any audio the server sends back.

Usage:
    python tools/mock_device.py                        # telemetry only
    python tools/mock_device.py --say "dim the lights"  # synthesise + send a command
    python tools/mock_device.py --wav clip.wav          # send a real recording
    python tools/mock_device.py --sweep 4                # compress a day into 4 minutes
"""

import argparse
import asyncio
import json
import math
import sys
import time
import wave
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol  # noqa: E402

SERVER = "ws://localhost:8000/ws/device"


def lux_for_hour(hour):
    """A plausible indoor/outdoor lux pair for a given hour of the day."""
    # Daylight bell curve peaking at 13:00, zero outside 06:00-19:00.
    daylight = max(0.0, math.sin(math.pi * (hour - 6.0) / 13.0))
    outdoor = 95_000.0 * daylight
    indoor = outdoor * 0.004 + 30.0  # a modest daylight factor plus ambient spill
    return round(indoor, 1), round(outdoor, 1)


def simulated_lux(now=None):
    """The lux pair for the real current time of day."""
    now = now or time.localtime()
    return lux_for_hour(now.tm_hour + now.tm_min / 60.0)


def swept_hour(started_at, sweep_minutes):
    """Map elapsed wall time onto a repeating 24-hour cycle.

    Watching the controller at 3am shows a flat line, because it is correctly holding
    the lights near off. Compressing a whole day into a few minutes makes the model's
    response to changing daylight visible in one screenful — dark at night, dimming as
    the sun climbs, back up through dusk.
    """
    elapsed = time.time() - started_at
    return (elapsed / (sweep_minutes * 60.0) * 24.0) % 24.0


async def send_wav(socket, path):
    """Stream a WAV file as if the button had just been released."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    print(f"[mock] sending {len(frames)/2/rate:.1f}s of audio at {rate} Hz")
    await socket.send(json.dumps({"type": protocol.AUDIO_BEGIN,
                                  "sample_rate": rate,
                                  "format": protocol.AUDIO_FORMAT}))
    for offset in range(0, len(frames), protocol.AUDIO_CHUNK_BYTES):
        await socket.send(frames[offset:offset + protocol.AUDIO_CHUNK_BYTES])
    await socket.send(json.dumps({"type": protocol.AUDIO_END}))


async def receive_loop(socket, state):
    """Handle everything the server sends: commands to apply, audio to play."""
    inbound_audio = bytearray()
    receiving = False

    async for message in socket:
        if isinstance(message, bytes):
            if receiving:
                inbound_audio.extend(message)
            continue

        payload = json.loads(message)
        kind = payload.get("type")

        if kind == protocol.BRIGHTNESS:
            value = payload["value"]
            state["brightness"] = value
            # A real device would drive the PWM here, then acknowledge once applied.
            await socket.send(json.dumps({
                "type": protocol.ACK,
                "command_id": payload["command_id"],
                "brightness": value,
            }))
            print(f"[mock] panel -> {value*100:5.1f}%")

        elif kind == protocol.RECORD:
            # The dashboard's push-to-talk button. A real device would capture from
            # its INMP441 here; the mock replays a synthesised clip instead so the
            # trigger path can be tested without hardware.
            duration = payload.get("duration_ms", 5000)
            print(f"[mock] server asked for {duration} ms of audio")
            clip = state.get("clip")
            if clip:
                await send_wav(socket, clip)
            else:
                print("[mock] no --say clip loaded, ignoring record request")

        elif kind == protocol.AUDIO_BEGIN:
            inbound_audio = bytearray()
            receiving = True
            print(f"[mock] receiving {payload.get('bytes', 0)} bytes of audio")

        elif kind == protocol.AUDIO_END:
            receiving = False
            seconds = len(inbound_audio) / 2 / protocol.AUDIO_SAMPLE_RATE
            out = Path(__file__).parent / "last_playback.wav"
            with wave.open(str(out), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(protocol.AUDIO_SAMPLE_RATE)
                handle.writeframes(bytes(inbound_audio))
            print(f"[mock] played {seconds:.1f}s of audio (saved to {out.name})")
            state["played"] = seconds


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", help="WAV file to send as a voice command")
    parser.add_argument("--say", help="text to synthesise and send as a voice command")
    parser.add_argument("--seconds", type=float, default=20.0,
                        help="how long to run")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between sensor readings")
    parser.add_argument("--sweep", type=float, default=0.0,
                        help="compress a 24-hour daylight cycle into this many "
                             "minutes, instead of using the real clock")
    args = parser.parse_args()

    async with websockets.connect(SERVER, max_size=None) as socket:
        state = {"brightness": 0.0, "played": None, "clip": None}
        if args.say:
            state["clip"] = await synthesise_command(args.say)
        await socket.send(json.dumps({"type": protocol.HELLO,
                                      "device_id": "mock-esp32", "fw": "test"}))
        receiver = asyncio.create_task(receive_loop(socket, state))

        # Let a few readings land so the rolling window and energy totals are real
        # before any voice command is evaluated.
        deadline = time.time() + args.seconds
        started_at = time.time()
        sent_command = False
        last_reported_hour = -1
        while time.time() < deadline:
            if args.sweep > 0:
                hour = swept_hour(started_at, args.sweep)
                indoor, outdoor = lux_for_hour(hour)
                # Announce each simulated hour so the console reads as a clock.
                if int(hour) != last_reported_hour:
                    last_reported_hour = int(hour)
                    print(f"[mock] --- simulated {last_reported_hour:02d}:00 "
                          f"(outdoor {outdoor:,.0f} lx) ---")
            else:
                indoor, outdoor = simulated_lux()
            await socket.send(json.dumps({"type": protocol.SENSOR,
                                          "indoor_lux": indoor,
                                          "outdoor_lux": outdoor}))
            await asyncio.sleep(args.interval)

            if not sent_command and (args.wav or args.say):
                sent_command = True
                path = args.wav
                if args.say:
                    path = await synthesise_command(args.say)
                await send_wav(socket, path)

        receiver.cancel()
        with contextlib_suppress():
            await receiver


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(asyncio.CancelledError)


async def synthesise_command(text):
    """Turn text into a WAV using the same TTS stack, to fake a spoken command."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from voice import tts

    pcm, _ = await tts.synthesise(text, "en")
    out = Path(__file__).parent / "spoken_command.wav"
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(protocol.AUDIO_SAMPLE_RATE)
        handle.writeframes(pcm)
    return out


if __name__ == "__main__":
    asyncio.run(main())
