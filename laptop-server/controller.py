"""
The system's decision layer: everything between "a reading arrived" and "a command
goes out", and between "a transcript arrived" and "a confirmation is spoken".

Kept separate from the transport in main.py so the control logic can be exercised
without a WebSocket or a device attached.
"""

import asyncio
import time
import uuid

import protocol
from state import AUTO, MANUAL
from voice import nlu, tts


class Controller:
    def __init__(self, config, mode_state, window, predictor, history):
        self.config = config
        self.state = mode_state
        self.window = window
        self.predictor = predictor
        self.history = history        # SQLite log; records only real-device sessions

        self.device = None            # the connected DeviceSession, if any
        self.last_indoor_lux = 0.0
        self.last_outdoor_lux = 0.0
        self.last_brightness = 0.0
        self.last_sensor_at = None
        self.sensor_error = None      # why readings are unavailable, if they are

        # Dashboard sockets subscribe here; each gets its own queue so a slow client
        # cannot block the others or the device loop.
        self._subscribers = set()
        self._lock = asyncio.Lock()

    # --- lux <-> brightness -------------------------------------------------

    def brightness_for_target_lux(self, target_lux):
        """Panel output needed to reach `target_lux` given the daylight already present.

        Mirrors the target definition the model was trained on: subtract the panel's
        own current contribution from the measured reading to get the ambient
        component, then fill the remaining gap.
        """
        panel_full = float(self.config.get("panel_full_output_lux", 500))
        if panel_full <= 0:
            return 0.0
        ambient = max(0.0, self.last_indoor_lux - self.last_brightness * panel_full)
        return max(0.0, min(1.0, (float(target_lux) - ambient) / panel_full))

    # --- telemetry ----------------------------------------------------------

    async def on_sensor_unavailable(self, reason):
        """The device has told us it has no trustworthy reading.

        AUTO cannot run without one, so the panel is left exactly where it is rather
        than being driven from a guess. The dashboard is told why.
        """
        self.sensor_error = reason
        self.last_sensor_at = None
        self.history.on_unavailable(reason)
        print(f"[controller] sensor unavailable: {reason}")
        await self.broadcast_state()

    async def on_sensor(self, indoor_lux, outdoor_lux, simulated=False):
        """Handle one reading: predict, resolve against the mode state, command.

        A `simulated` reading drives the system exactly like a real one but is kept
        out of the persistent history, along with the command it produces.
        """
        self.sensor_error = None
        self.last_indoor_lux = float(indoor_lux)
        self.last_outdoor_lux = float(outdoor_lux)
        self.last_sensor_at = time.time()

        # Predict even while MANUAL holds, so the dashboard can show what AUTO *would*
        # be doing and the value is fresh the instant MANUAL expires.
        from datetime import datetime
        predicted = self.predictor.predict(indoor_lux, outdoor_lux, datetime.now())

        brightness = self.state.resolve_brightness(predicted)
        self.last_brightness = brightness

        snapshot = self.state.snapshot()
        self.window.add(indoor_lux, outdoor_lux, brightness,
                        snapshot["mode"], snapshot["active_mode_name"])

        if not simulated:
            self.history.on_reading(indoor_lux, outdoor_lux, brightness,
                                    snapshot["mode"], snapshot["active_mode_name"])
        await self.send_brightness(
            brightness, cause=None if simulated
            else ("auto" if snapshot["mode"] == AUTO else "hold"))
        await self.broadcast_state(predicted=predicted)
        return brightness

    async def send_brightness(self, brightness, await_ack=False, cause=None,
                              transcript=None):
        """Push a brightness command to the device.

        With `await_ack`, waits for the device to confirm it actually applied the
        change — the gate the spoken confirmation sits behind. `cause` names who
        asked for it (auto / hold / voice / voice_typed / dashboard) and is what the
        history's change log records; None keeps the command out of the log.
        """
        if self.device is None:
            return None
        if cause is not None:
            snapshot = self.state.snapshot()
            reason = snapshot["last_reason"]
            if cause == "auto":
                # In AUTO the model made the call; last_reason only says how AUTO
                # was entered, which is context, not the cause.
                reason = ("model prediction" if reason == "startup"
                          else f"model prediction ({reason})")
            self.history.on_command(brightness, cause, reason,
                                    snapshot["mode"], snapshot["active_mode_name"],
                                    transcript)
        command_id = uuid.uuid4().hex[:12]
        await self.device.send_json({
            "type": protocol.BRIGHTNESS,
            "command_id": command_id,
            "value": round(float(brightness), 4),
        })
        if not await_ack:
            return None
        return await self.device.wait_for_ack(command_id)

    async def request_recording(self, duration_ms=None):
        """Ask the device to capture audio — the dashboard's push-to-talk button.

        Returns True if the request reached a device. False means no ESP32 is
        attached, and the caller should fall back to the browser's own microphone.
        """
        if self.device is None:
            return False
        await self.device.send_json({
            "type": protocol.RECORD,
            "duration_ms": int(duration_ms or protocol.RECORD_DURATION_MS),
        })
        await self.broadcast({"type": "recording_started",
                              "source": "esp32",
                              "duration_ms": int(duration_ms
                                                 or protocol.RECORD_DURATION_MS)})
        return True

    async def request_sweep(self):
        """Ask the device to ramp the panel 0 -> 255 -> 0.

        The only way to tell a firmware fault from a wiring fault without a
        meter: if the panel does not visibly move during this, PWM is reaching
        the pin but the wiring cannot act on it.
        """
        if self.device is None:
            return False
        await self.device.send_json({"type": protocol.SWEEP})
        return True

    # --- voice --------------------------------------------------------------

    async def handle_transcript(self, transcript, return_audio=False, typed=False):
        """Parse a transcript, apply it, and speak the result.

        Returns a dict describing what happened, which the dashboard also receives.
        With `return_audio`, the synthesised confirmation is returned to the caller
        instead of only being pushed at the device — so a browser that captured the
        command can also play the reply when no ESP32 is attached.
        """
        modes = self.config.modes
        settings = self.config.settings
        intent = nlu.parse(transcript, modes, settings)

        action, snapshot, mode_name = self._apply(intent)

        # Gate the confirmation on the device actually applying the change
        # (decision #13). If no device is attached — e.g. dashboard-only testing —
        # there is nothing to wait for.
        applied = True
        if action != "unknown" and self.device is not None:
            ack = await self.send_brightness(self.state.snapshot()["target_brightness"],
                                             await_ack=True,
                                             cause="voice_typed" if typed else "voice",
                                             transcript=transcript)
            applied = ack is not None
            if not applied:
                print("[controller] device did not acknowledge within "
                      f"{protocol.ACK_TIMEOUT_SECONDS}s; speaking anyway")

        brightness_percent = self.state.snapshot()["target_brightness"] * 100.0
        savings = self.window.savings_percent()
        text = tts.confirmation_text(action, brightness_percent, savings,
                                     intent.language, mode_name)

        result = {
            "transcript": transcript,
            "intent": intent.as_dict(),
            "action": action,
            "applied": applied,
            "brightness": brightness_percent / 100.0,
            "savings_percent": savings,
            "confirmation_text": text,
            "state": snapshot,
        }

        await self.broadcast({"type": "voice_result", **result})

        if return_audio:
            # The caller wants the audio itself, so synthesise inline rather than in
            # a background task and hand back the PCM.
            pcm, source = await tts.synthesise(text, intent.language)
            if self.device is not None and pcm:
                await self.device.send_audio(pcm)
            result["audio_pcm"] = pcm
            result["audio_source"] = source
            result["audio_sample_rate"] = protocol.AUDIO_SAMPLE_RATE
        else:
            # Speaking is slow (network round trip to edge-tts); do not make the
            # caller wait on it before the dashboard learns what happened.
            asyncio.create_task(self._speak(text, intent.language))
        return result

    def _apply(self, intent):
        """Route a parsed intent into the state machine.

        Returns (action_tag_for_the_confirmation_template, snapshot, mode_name).
        """
        if intent.kind == nlu.AUTO:
            # Explicit "auto" cancels MANUAL immediately, bypassing the timer.
            return "auto", self.state.set_auto("voice: auto mode"), None

        if intent.kind == nlu.MODE:
            mode = intent.mode
            brightness = self.brightness_for_target_lux(mode["target_lux"])
            snapshot = self.state.set_mode(mode, brightness,
                                           f"voice: {mode['name']} mode")
            return "mode", snapshot, mode["name"]

        if intent.kind == nlu.ABSOLUTE:
            if intent.lux is not None:
                brightness = self.brightness_for_target_lux(intent.lux)
            else:
                brightness = intent.percent / 100.0
            snapshot = self.state.set_brightness(brightness, "voice: absolute")
            return "set", snapshot, None

        if intent.kind == nlu.RELATIVE:
            snapshot = self.state.adjust_brightness(
                intent.delta, self.last_brightness, "voice: relative")
            return ("up" if intent.direction > 0 else "down"), snapshot, None

        # Unrecognised: change nothing at all. Silently guessing at a command the user
        # did not give is worse than saying "I did not understand".
        return "unknown", self.state.snapshot(), None

    async def _speak(self, text, language):
        pcm, source = await tts.synthesise(text, language)
        if not pcm or self.device is None:
            return
        await self.device.send_audio(pcm)
        print(f"[controller] spoke {len(pcm)/2/protocol.AUDIO_SAMPLE_RATE:.1f}s "
              f"via {source}")

    # --- dashboard control --------------------------------------------------

    async def apply_override(self, *, auto=False, mode_id=None, brightness=None):
        """Manual control from the dashboard. Same state machine, no voice involved."""
        if auto:
            snapshot = self.state.set_auto("dashboard: auto")
        elif mode_id is not None:
            mode = self.config.find_mode(mode_id)
            if mode is None:
                raise ValueError(f"unknown mode: {mode_id}")
            snapshot = self.state.set_mode(
                mode, self.brightness_for_target_lux(mode["target_lux"]),
                f"dashboard: {mode['name']} mode")
        elif brightness is not None:
            snapshot = self.state.set_brightness(float(brightness),
                                                 "dashboard: brightness")
        else:
            raise ValueError("no override specified")

        await self.send_brightness(snapshot["target_brightness"], cause="dashboard")
        await self.broadcast_state()
        return snapshot

    # --- dashboard fan-out --------------------------------------------------

    def subscribe(self):
        queue = asyncio.Queue(maxsize=32)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)

    async def broadcast(self, message):
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A dashboard that has fallen this far behind will resync from its next
                # full state message; dropping is better than stalling the device loop.
                pass

    def status(self, predicted=None):
        snapshot = self.state.snapshot()
        connected = (self.last_sensor_at is not None
                     and time.time() - self.last_sensor_at
                     < protocol.DEVICE_TIMEOUT_SECONDS)
        return {
            "type": "state",
            "mode": snapshot["mode"],
            "active_mode_id": snapshot["active_mode_id"],
            "active_mode_name": snapshot["active_mode_name"],
            "manual_seconds_remaining": snapshot["manual_seconds_remaining"],
            "last_reason": snapshot["last_reason"],
            "indoor_lux": self.last_indoor_lux,
            "outdoor_lux": self.last_outdoor_lux,
            "brightness": self.last_brightness,
            "predicted_brightness": predicted,
            # Two distinct facts: whether a device is on the socket at all, and
            # whether it is currently supplying readings. A board with no light
            # sensors fitted is connected but has nothing to report.
            "device_connected": self.device is not None,
            # The dashboard must be able to tell a mock device from the real board.
            "device_id": getattr(self.device, "device_id", None),
            "device_simulated": bool(getattr(self.device, "simulated", False)),
            "readings_live": connected,
            "sensor_error": self.sensor_error,
            # The chart needs the rated wattage to turn an output fraction into watts,
            # and it is a config value the user can change while the page is open.
            "panel_rated_watts": float(self.config.get("panel_rated_watts", 18.0)),
            "energy": self.window.energy_summary(),
            "model": self.predictor.info,
        }

    async def broadcast_state(self, predicted=None):
        await self.broadcast(self.status(predicted))
