"""
The laptop server — one process, everything in it (architectural decision #4).

Hosts, on a single asyncio event loop:
  * the ESP32 WebSocket (sensor telemetry in, brightness + audio out)
  * ML inference for AUTO mode
  * STT (MLX Whisper), rule-based NLU, TTS (edge-tts)
  * the mode state machine and the in-memory rolling window
  * the dashboard's REST API and its live WebSocket

Run:  python main.py            (or: uvicorn main:app --host 0.0.0.0 --port 8000)
"""

import asyncio
import base64
import contextlib
import csv
import io
import json
import time
from pathlib import Path

from fastapi import (Body, FastAPI, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import protocol
from config_store import ConfigStore
from controller import Controller
from history import HistoryStore, is_simulated
from predictor import BrightnessPredictor
from rolling import RollingWindow
from state import ModeState
from voice import stt, tts

HERE = Path(__file__).resolve().parent

config = ConfigStore()
mode_state = ModeState(config)
window = RollingWindow(config)
predictor = BrightnessPredictor()
history = HistoryStore(config)
controller = Controller(config, mode_state, window, predictor, history)

app = FastAPI(title="Adaptive Lighting Server")

# The dashboard runs on a different port in development (Vite on 5173), so it needs
# CORS. In production it is served from this same origin and this is a no-op.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- DEVICE SESSION -----------------------------------------------------------

class DeviceSession:
    """One connected ESP32. Owns the socket and the pending-ack bookkeeping."""

    def __init__(self, socket):
        self.socket = socket
        self.device_id = "unknown"
        self.recording = False        # whether this session owns the open history row
        self._send_lock = asyncio.Lock()
        self._pending_acks = {}

        # Inbound audio assembly: the device brackets binary PCM frames with
        # audio_begin / audio_end, so chunks are accumulated between the two.
        self._audio_buffer = bytearray()
        self._receiving_audio = False
        self._audio_sample_rate = protocol.AUDIO_SAMPLE_RATE

    async def send_json(self, payload):
        # Serialise sends: audio streaming and a concurrent brightness command must not
        # interleave frames on the same socket.
        async with self._send_lock:
            await self.socket.send_text(json.dumps(payload))

    async def send_audio(self, pcm):
        """Stream PCM to the device in chunks, bracketed by begin/end."""
        async with self._send_lock:
            await self.socket.send_text(json.dumps({
                "type": protocol.AUDIO_BEGIN,
                "sample_rate": protocol.AUDIO_SAMPLE_RATE,
                "format": protocol.AUDIO_FORMAT,
                "bytes": len(pcm),
            }))
            for offset in range(0, len(pcm), protocol.AUDIO_CHUNK_BYTES):
                await self.socket.send_bytes(
                    pcm[offset:offset + protocol.AUDIO_CHUNK_BYTES])
            await self.socket.send_text(json.dumps({"type": protocol.AUDIO_END}))

    async def wait_for_ack(self, command_id):
        """Block until the device confirms this command, or the timeout elapses."""
        future = asyncio.get_running_loop().create_future()
        self._pending_acks[command_id] = future
        try:
            return await asyncio.wait_for(future, protocol.ACK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_acks.pop(command_id, None)

    def resolve_ack(self, command_id, payload):
        future = self._pending_acks.get(command_id)
        if future is not None and not future.done():
            future.set_result(payload)

    # --- inbound ---------------------------------------------------------

    async def handle_text(self, raw):
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = message.get("type")

        if kind == protocol.HELLO:
            self.device_id = message.get("device_id", "esp32")
            print(f"[device] hello from {self.device_id} "
                  f"(fw {message.get('fw', '?')})")
            # History starts at the hello, and only for real hardware.
            if is_simulated(self.device_id, message):
                print("[history] simulated device — not recording")
            else:
                await history.start_session(self.device_id, message.get("fw"))
                self.recording = True

        elif kind == protocol.SENSOR:
            await controller.on_sensor(
                message.get("indoor_lux", 0.0),
                message.get("outdoor_lux", 0.0),
            )

        elif kind == protocol.SENSOR_UNAVAILABLE:
            await controller.on_sensor_unavailable(
                message.get("reason", "unspecified"))

        elif kind == protocol.ACK:
            self.resolve_ack(message.get("command_id"), message)

        elif kind == protocol.AUDIO_BEGIN:
            self._audio_buffer = bytearray()
            self._receiving_audio = True
            self._audio_sample_rate = message.get("sample_rate",
                                                  protocol.AUDIO_SAMPLE_RATE)

        elif kind == protocol.AUDIO_END:
            self._receiving_audio = False
            payload = bytes(self._audio_buffer)
            self._audio_buffer = bytearray()
            if payload:
                # Transcription takes seconds; do not block the socket read loop on it,
                # or sensor telemetry would stall for the duration.
                asyncio.create_task(self._process_audio(payload))

    def handle_binary(self, payload):
        if self._receiving_audio:
            self._audio_buffer.extend(payload)

    async def _process_audio(self, payload):
        duration = len(payload) / 2 / self._audio_sample_rate
        print(f"[device] received {duration:.1f}s of audio, transcribing...")
        try:
            audio = stt.pcm16_to_float32(payload)
            text, language = await stt.transcribe(audio)
        except Exception as exc:
            print(f"[device] transcription failed: {exc}")
            await controller.broadcast({"type": "voice_error", "error": str(exc)})
            return

        print(f"[device] transcript ({language}): {text!r}")
        if not text:
            await controller.broadcast({"type": "voice_error",
                                        "error": "empty transcript"})
            return
        await controller.handle_transcript(text)


@app.websocket("/ws/device")
async def device_socket(socket: WebSocket):
    await socket.accept()

    # Exactly one device at a time. A second connection used to silently
    # overwrite `controller.device`, leaving the first socket open but orphaned:
    # it kept streaming telemetry while the server believed no device was
    # attached and could no longer send it commands. Close the old one properly
    # so the state always matches reality.
    previous = controller.device
    if previous is not None:
        print("[device] replacing an existing connection")
        try:
            await previous.socket.close(code=1000, reason="replaced by a new device")
        except Exception:
            pass

    if previous is not None and previous.recording:
        # The old socket's own cleanup runs later; close its history row now so
        # the new device's readings are never credited to the old session.
        previous.recording = False
        history.end_session("replaced")

    session = DeviceSession(socket)
    controller.device = session
    print("[device] connected")
    await controller.broadcast_state()

    try:
        while True:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if (text := message.get("text")) is not None:
                await session.handle_text(text)
            elif (payload := message.get("bytes")) is not None:
                session.handle_binary(payload)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[device] socket error: {exc}")
    finally:
        if controller.device is session:
            controller.device = None
        if session.recording:
            session.recording = False
            history.end_session(
                "disconnect" if controller.device is None else "replaced")
        print("[device] disconnected")
        await controller.broadcast_state()


# --- DASHBOARD WEBSOCKET ------------------------------------------------------

@app.websocket("/ws/dashboard")
async def dashboard_socket(socket: WebSocket):
    await socket.accept()
    queue = controller.subscribe()
    try:
        # Send the full picture immediately so a freshly opened tab is not blank until
        # the next sensor sample arrives.
        await socket.send_text(json.dumps(controller.status()))
        await socket.send_text(json.dumps({"type": "series",
                                           "points": window.series()}))
        while True:
            message = await queue.get()
            await socket.send_text(json.dumps(message))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        controller.unsubscribe(queue)


# --- REST API -----------------------------------------------------------------

@app.get("/api/state")
def get_state():
    return controller.status()


@app.get("/api/series")
def get_series():
    """The rolling window, for the live chart. Memory only — nothing persisted."""
    return {"points": window.series(), "energy": window.energy_summary()}


@app.get("/api/modes")
def get_modes():
    return {"modes": config.modes}


@app.post("/api/modes")
def create_mode(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        target_lux = int(payload.get("target_lux", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "target_lux must be a number")
    if not 0 <= target_lux <= 20_000:
        raise HTTPException(400, "target_lux must be between 0 and 20000")
    return config.add_mode(name, target_lux)


@app.put("/api/modes/{mode_id}")
def edit_mode(mode_id: str, payload: dict = Body(...)):
    target_lux = payload.get("target_lux")
    if target_lux is not None:
        try:
            target_lux = int(target_lux)
        except (TypeError, ValueError):
            raise HTTPException(400, "target_lux must be a number")
        if not 0 <= target_lux <= 20_000:
            raise HTTPException(400, "target_lux must be between 0 and 20000")
    updated = config.update_mode(mode_id, payload.get("name"), target_lux)
    if updated is None:
        raise HTTPException(404, "no such mode")
    return updated


@app.delete("/api/modes/{mode_id}")
def remove_mode(mode_id: str):
    if not config.delete_mode(mode_id):
        raise HTTPException(404, "no such mode")
    # If the deleted mode was the one currently held, fall back to AUTO rather than
    # holding a target the user can no longer see or edit.
    if mode_state.snapshot()["active_mode_id"] == mode_id:
        mode_state.set_auto("active mode deleted")
    return {"deleted": mode_id}


@app.get("/api/settings")
def get_settings():
    return config.settings


@app.put("/api/settings")
def put_settings(payload: dict = Body(...)):
    return config.update_settings(payload)


@app.post("/api/override")
async def post_override(payload: dict = Body(...)):
    try:
        return await controller.apply_override(
            auto=bool(payload.get("auto")),
            mode_id=payload.get("mode_id"),
            brightness=payload.get("brightness"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/light/sweep")
async def sweep_panel():
    """Ramp the panel up and down so a human can confirm it physically responds."""
    started = await controller.request_sweep()
    if not started:
        raise HTTPException(409, "no device connected")
    return {"ok": True, "note": "watch the panel for ~2.5s"}


@app.post("/api/voice/record")
async def start_recording(payload: dict = Body(None)):
    """Push-to-talk: ask the ESP32 to capture audio from its own microphone.

    Returns `{"source": "esp32"}` when a device took the request. If no device is
    attached it returns `{"source": null}` and the dashboard falls back to the
    browser's microphone — the caller is always told which microphone was used, so
    nothing on screen is ambiguous about where the audio came from.
    """
    duration_ms = (payload or {}).get("duration_ms", protocol.RECORD_DURATION_MS)
    started = await controller.request_recording(duration_ms)
    return {"source": "esp32" if started else None,
            "duration_ms": duration_ms,
            "device_connected": controller.device is not None}


@app.post("/api/voice/audio")
async def upload_audio(request: Request):
    """Accept a WAV captured by the browser and run it through the voice pipeline.

    This is the fallback path for when no ESP32 is attached. The body is a raw WAV
    file: 16 kHz mono signed 16-bit, which is what the dashboard's recorder produces
    and what Whisper wants, so nothing has to be resampled or transcoded here.

    The synthesised confirmation comes back as base64 PCM so the browser can play it,
    since without a device there is no speaker on the other end.
    """
    payload = await request.body()
    if not payload:
        raise HTTPException(400, "empty body — expected a WAV file")

    try:
        audio = stt.wav_to_float32(payload)
    except Exception as exc:
        raise HTTPException(400, f"could not read WAV: {exc}")

    duration = len(audio) / stt.SAMPLE_RATE
    print(f"[browser] received {duration:.1f}s of audio, transcribing...")

    try:
        text, language = await stt.transcribe(audio)
    except Exception as exc:
        raise HTTPException(500, f"transcription failed: {exc}")

    print(f"[browser] transcript ({language}): {text!r}")
    if not text:
        return {"transcript": "", "action": "unknown", "applied": False,
                "confirmation_text": "Nothing was heard.", "audio_b64": None,
                "source": "browser"}

    result = await controller.handle_transcript(text, return_audio=True)

    pcm = result.pop("audio_pcm", b"")
    result["audio_b64"] = base64.b64encode(pcm).decode() if pcm else None
    result["source"] = "browser"
    return result


@app.post("/api/simulate/sensor")
async def simulate_sensor(payload: dict = Body(...)):
    """Inject a sensor reading without hardware.

    Exists so the whole AUTO path, the chart and the energy maths can be exercised
    before the ESP32 is wired up. Not used in normal operation.
    """
    brightness = await controller.on_sensor(
        float(payload.get("indoor_lux", 0.0)),
        float(payload.get("outdoor_lux", 0.0)),
        simulated=True,
    )
    return {"brightness": brightness, "state": controller.status()}


@app.post("/api/simulate/voice")
async def simulate_voice(payload: dict = Body(...)):
    """Run a transcript through NLU + state + TTS without speaking into a mic."""
    transcript = payload.get("transcript", "")
    return await controller.handle_transcript(transcript, typed=True)


@app.get("/api/health")
def health():
    return {"ok": True, "device_connected": controller.device is not None}


# --- HISTORY ------------------------------------------------------------------
# Everything below reads the SQLite log. Times are unix seconds; omitting `start`
# or `end` means "from the beginning" / "until now".

@app.get("/api/history/summary")
def history_summary(start: float | None = None, end: float | None = None):
    return history.summary(start, end)


@app.get("/api/history/samples")
def history_samples(start: float | None = None, end: float | None = None,
                    max_points: int = 1500):
    return history.samples(start, end, max_points)


@app.get("/api/history/changes")
def history_changes(start: float | None = None, end: float | None = None,
                    limit: int = 500):
    return {"changes": history.changes(start, end, limit)}


@app.get("/api/history/sessions")
def history_sessions(start: float | None = None, end: float | None = None,
                     limit: int = 200):
    return {"sessions": history.sessions(start, end, limit)}


@app.get("/api/history/export/{table}.csv")
def history_export(table: str, start: float | None = None, end: float | None = None):
    """The raw rows as CSV, for analysis outside the dashboard."""
    if table not in ("samples", "light_changes"):
        raise HTTPException(404, "export 'samples' or 'light_changes'")
    header, rows = history.export_rows(table, start, end)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{table}.csv"'})


# --- LIFECYCLE ----------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    # Make sure there is always something to fall back to before the first command.
    await asyncio.get_running_loop().run_in_executor(None, tts.ensure_fallback_clip)
    # Warm Whisper in the background so the first voice command is not slow. The
    # server is fully usable while this runs.
    asyncio.create_task(stt.warmup())
    print("[server] ready on http://0.0.0.0:8000")


@app.on_event("shutdown")
async def on_shutdown():
    history.end_session("server_stopped")
    history.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
