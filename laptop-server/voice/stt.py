"""
Speech-to-text via MLX Whisper, running locally on the Mac's Neural Engine / GPU.

The model is large (whisper-large-v3-turbo) and takes several seconds to load, so it is
loaded once, lazily, on the first transcription rather than at import. Transcription
itself is CPU/GPU-bound and blocking, so callers run it in a thread executor to keep the
asyncio event loop free for the WebSocket and dashboard traffic.
"""

import asyncio
import io
import threading
import wave

import numpy as np

MODEL_REPO = "mlx-community/whisper-large-v3-turbo"

# Whisper always resamples to 16 kHz internally; the firmware records at this rate so
# nothing has to be resampled on the way in.
SAMPLE_RATE = 16_000

_load_lock = threading.Lock()
_loaded = False


def pcm16_to_float32(payload):
    """Convert raw little-endian signed 16-bit PCM bytes to the float32 [-1, 1] array
    Whisper expects. This is the format the ESP32 streams."""
    if len(payload) % 2:
        payload = payload[:-1]  # drop a trailing odd byte from a truncated frame
    samples = np.frombuffer(payload, dtype="<i2").astype(np.float32)
    return samples / 32768.0


def wav_to_float32(payload):
    """Accept a full WAV container as well as bare PCM, so the pipeline can be tested
    from a file without the hardware attached."""
    with wave.open(io.BytesIO(payload), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        channels = handle.getnchannels()
    audio = pcm16_to_float32(frames)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def _ensure_loaded():
    """Warm the model. Safe to call from several threads."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        import mlx_whisper
        # A one-sample transcribe forces the weights to download and materialise, so
        # the first real command does not pay the load cost.
        mlx_whisper.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32),
                               path_or_hf_repo=MODEL_REPO)
        _loaded = True


def transcribe_sync(audio):
    """Blocking transcription. `audio` is float32 mono at 16 kHz."""
    import mlx_whisper

    if audio is None or len(audio) == 0:
        return "", None

    _ensure_loaded()
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL_REPO)
    text = (result.get("text") or "").strip()
    # Whisper's own language guess is a useful cross-check, but the NLU does its own
    # detection from the vocabulary, which is more reliable on short Hinglish commands.
    return text, result.get("language")


async def transcribe(audio):
    """Async wrapper — runs the blocking call off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(
        None, transcribe_sync, audio)


async def warmup():
    """Preload the model in the background at server startup."""
    try:
        await asyncio.get_running_loop().run_in_executor(None, _ensure_loaded)
        print("[stt] Whisper model ready")
    except Exception as exc:
        print(f"[stt] WARNING: warmup failed ({exc}); "
              f"will retry on first command")
