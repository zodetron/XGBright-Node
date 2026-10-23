"""
Text-to-speech via edge-tts, with a pre-recorded fallback.

edge-tts runs as a local library but calls Microsoft's servers, so it needs internet
even though nothing else here does. That is an accepted tradeoff (architectural
decision #11) — and precisely why the fallback exists: generation is attempted with a
short timeout, and if it fails or stalls the user still hears a static "done" clip
rather than silence (architectural decision #13).

Confirmation text follows Option C (decision #12): say what changed *and* what it
saved, built from a template over the resolved action, the new value, and the live
savings figure from the rolling window — never a canned phrase.
"""

import asyncio
import io
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

ASSETS = Path(__file__).resolve().parent.parent / "assets"
FALLBACK_CLIP = ASSETS / "done.wav"

# Voices, per the project spec. Hinglish is spoken with the Hindi voice.
VOICES = {
    "hi": "hi-IN-SwaraNeural",
    "hinglish": "hi-IN-SwaraNeural",
    "mr": "mr-IN-AarohiNeural",
    "en": "en-IN-NeerjaNeural",
}
DEFAULT_VOICE = VOICES["en"]

# The laptop speaker has the bandwidth the ESP32 amp lacks, so it gets the expressive
# English voice. Other languages have no expressive variant and keep their spec voice.
LAPTOP_VOICES = {**VOICES, "en": "en-IN-NeerjaExpressiveNeural"}

# Generation must not hold up the confirmation. If Microsoft is slow, fall back.
GENERATION_TIMEOUT_SECONDS = 3.0
# The laptop plays in the background and holds up nothing, and the expressive voice is
# slower to generate, so it can afford to wait longer before settling for the fallback.
LAPTOP_GENERATION_TIMEOUT_SECONDS = 8.0

# The ESP32 plays back raw PCM, so everything is delivered in this format.
PLAYBACK_SAMPLE_RATE = 16_000


# --- CONFIRMATION TEXT --------------------------------------------------------

# A short spoken acknowledgement before the details, so the reply opens the way a person
# would answer. Not used for "unknown": nothing was done.
_OPENERS = {"en": "Done! ", "hi": "हो गया! ", "mr": "झालं! "}
_OPENERS["hinglish"] = _OPENERS["hi"]

# Templates are per-language so the confirmation is spoken in the language the command
# was given in. Each takes `value` (percent) and `savings` (percent).
_TEMPLATES = {
    "en": {
        "set": "Brightness set to {value:.0f} percent, using about {savings:.0f} "
               "percent less power than full brightness.",
        "up": "Brightness increased to {value:.0f} percent, using about {savings:.0f} "
              "percent less power than full brightness.",
        "down": "Brightness reduced to {value:.0f} percent, using about {savings:.0f} "
                "percent less power than full brightness.",
        "mode": "{mode} mode on, at {value:.0f} percent, using about {savings:.0f} "
                "percent less power than full brightness.",
        "auto": "Automatic mode on. Lighting will follow the sensors, "
                "currently saving about {savings:.0f} percent.",
        "unknown": "Sorry, I did not understand that command.",
    },
    "hi": {
        "set": "रोशनी {value:.0f} प्रतिशत पर सेट कर दी है, "
               "पूरी रोशनी से लगभग {savings:.0f} प्रतिशत कम बिजली लग रही है।",
        "up": "रोशनी बढ़ाकर {value:.0f} प्रतिशत कर दी है, "
              "पूरी रोशनी से लगभग {savings:.0f} प्रतिशत कम बिजली लग रही है।",
        "down": "रोशनी घटाकर {value:.0f} प्रतिशत कर दी है, "
                "पूरी रोशनी से लगभग {savings:.0f} प्रतिशत कम बिजली लग रही है।",
        "mode": "{mode} मोड चालू है, {value:.0f} प्रतिशत पर, "
                "पूरी रोशनी से लगभग {savings:.0f} प्रतिशत कम बिजली लग रही है।",
        "auto": "ऑटोमैटिक मोड चालू है। रोशनी सेंसर के हिसाब से चलेगी, "
                "अभी लगभग {savings:.0f} प्रतिशत बचत हो रही है।",
        "unknown": "माफ़ कीजिए, मैं यह कमांड समझ नहीं पाई।",
    },
    "mr": {
        "set": "उजेड {value:.0f} टक्क्यांवर सेट केला आहे, "
               "पूर्ण उजेडापेक्षा सुमारे {savings:.0f} टक्के कमी वीज लागते आहे.",
        "up": "उजेड वाढवून {value:.0f} टक्के केला आहे, "
              "पूर्ण उजेडापेक्षा सुमारे {savings:.0f} टक्के कमी वीज लागते आहे.",
        "down": "उजेड कमी करून {value:.0f} टक्के केला आहे, "
                "पूर्ण उजेडापेक्षा सुमारे {savings:.0f} टक्के कमी वीज लागते आहे.",
        "mode": "{mode} मोड सुरू आहे, {value:.0f} टक्क्यांवर, "
                "पूर्ण उजेडापेक्षा सुमारे {savings:.0f} टक्के कमी वीज लागते आहे.",
        "auto": "ऑटोमॅटिक मोड सुरू आहे. उजेड सेन्सरनुसार बदलेल, "
                "सध्या सुमारे {savings:.0f} टक्के बचत होते आहे.",
        "unknown": "माफ करा, मला ही आज्ञा समजली नाही.",
    },
}
# Hinglish is spoken in Hindi.
_TEMPLATES["hinglish"] = _TEMPLATES["hi"]


def confirmation_text(action, brightness_percent, savings_percent,
                      language="en", mode_name=None):
    """Build the spoken confirmation.

    `action` is one of set / up / down / mode / auto / unknown.
    """
    templates = _TEMPLATES.get(language, _TEMPLATES["en"])
    template = templates.get(action, templates["unknown"])
    text = template.format(value=brightness_percent or 0.0,
                           savings=savings_percent or 0.0,
                           mode=mode_name or "")
    if action in templates and action != "unknown":
        text = _OPENERS.get(language, _OPENERS["en"]) + text
    return text


# --- AUDIO GENERATION ---------------------------------------------------------

async def _generate_mp3(text, voice):
    """Ask edge-tts for MP3 bytes."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    chunks = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.extend(chunk["data"])
    if not chunks:
        raise RuntimeError("edge-tts returned no audio")
    return bytes(chunks)


def _mp3_to_pcm16(mp3_bytes, sample_rate=PLAYBACK_SAMPLE_RATE):
    """Decode MP3 to raw mono 16-bit PCM at the playback rate.

    The ESP32 has no MP3 decoder, so it is fed PCM it can push straight at the DAC.
    Decoding goes through libsndfile (via soundfile), which handles MP3 natively — no
    ffmpeg or any other external binary is required, so the server has no system
    dependency beyond its Python packages.
    """
    audio, source_rate = sf.read(io.BytesIO(mp3_bytes), dtype="float32", always_2d=True)
    # edge-tts returns mono, but mix down defensively in case that ever changes.
    mono = audio.mean(axis=1)

    if source_rate != sample_rate:
        # Linear resample. The source is 24 kHz speech going to 16 kHz; there is no
        # audible benefit to a windowed-sinc filter for a spoken confirmation played
        # through a small ESP32 speaker.
        duration = len(mono) / float(source_rate)
        target_count = int(duration * sample_rate)
        if target_count <= 0:
            raise RuntimeError("decoded audio too short to resample")
        mono = np.interp(
            np.linspace(0.0, len(mono) - 1, target_count),
            np.arange(len(mono)),
            mono,
        )

    clipped = np.clip(mono, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _load_fallback_pcm():
    """Read the pre-recorded clip as raw PCM at the playback rate."""
    if not FALLBACK_CLIP.exists():
        return b""
    try:
        with wave.open(str(FALLBACK_CLIP), "rb") as handle:
            if (handle.getframerate() == PLAYBACK_SAMPLE_RATE
                    and handle.getnchannels() == 1
                    and handle.getsampwidth() == 2):
                return handle.readframes(handle.getnframes())
        # Wrong format on disk — let ffmpeg normalise it.
        return _mp3_to_pcm16(FALLBACK_CLIP.read_bytes())
    except Exception:
        return b""


async def synthesise(text, language="en"):
    """Return (pcm_bytes, source).

    `source` is "edge-tts" on success or "fallback" when the static clip was used, so
    the caller can log which path ran. Never raises and never returns empty audio
    unless the fallback clip is also missing.
    """
    voice = VOICES.get(language, DEFAULT_VOICE)
    try:
        mp3 = await asyncio.wait_for(_generate_mp3(text, voice),
                                     timeout=GENERATION_TIMEOUT_SECONDS)
        pcm = await asyncio.get_running_loop().run_in_executor(
            None, _mp3_to_pcm16, mp3)
        if pcm:
            return pcm, "edge-tts"
        raise RuntimeError("empty PCM after decode")
    except Exception as exc:
        print(f"[tts] edge-tts unavailable ({type(exc).__name__}: {exc}); "
              f"using fallback clip")
        return _load_fallback_pcm(), "fallback"


# --- LAPTOP SPEAKER -----------------------------------------------------------

# One reply at a time: two quick commands queue their confirmations instead of
# talking over each other.
_laptop_lock = asyncio.Lock()


def laptop_speaker_available():
    """macOS ships `afplay`; elsewhere there is no dependency-free player, so skip."""
    return sys.platform == "darwin" and shutil.which("afplay") is not None


async def speak_on_laptop(text, language="en"):
    """Play the confirmation through the laptop's own speakers.

    Uses the edge-tts MP3 as-is (24 kHz) rather than the 16 kHz PCM made for the
    ESP32, so it sounds as good as the voice allows. Falls back to the static clip
    when edge-tts is unreachable. Returns the source used, or None if nothing played.
    """
    if not laptop_speaker_available():
        return None

    voice = LAPTOP_VOICES.get(language, DEFAULT_VOICE)
    async with _laptop_lock:
        try:
            mp3 = await asyncio.wait_for(_generate_mp3(text, voice),
                                         timeout=LAPTOP_GENERATION_TIMEOUT_SECONDS)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
                handle.write(mp3)
                path, source = Path(handle.name), "edge-tts"
        except Exception as exc:
            print(f"[tts] laptop voice: edge-tts unavailable "
                  f"({type(exc).__name__}: {exc}); using fallback clip")
            if not FALLBACK_CLIP.exists():
                return None
            path, source = FALLBACK_CLIP, "fallback"

        try:
            process = await asyncio.create_subprocess_exec(
                "afplay", str(path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await process.wait()
        finally:
            if path != FALLBACK_CLIP:
                path.unlink(missing_ok=True)
    print(f"[tts] spoke on laptop via {source} ({voice})")
    return source


# --- FALLBACK CLIP GENERATION -------------------------------------------------

def ensure_fallback_clip():
    """Make sure a static 'done' clip exists on disk.

    Tries edge-tts once at setup time so the fallback is a real spoken word. If that is
    not possible (no internet at install time), synthesises a short two-tone chime
    instead — the point is that the user always hears *something*, and a chime carries
    the "command applied" signal perfectly well.
    """
    if FALLBACK_CLIP.exists() and FALLBACK_CLIP.stat().st_size > 0:
        return FALLBACK_CLIP

    ASSETS.mkdir(parents=True, exist_ok=True)
    try:
        mp3 = asyncio.run(_generate_mp3("Done.", VOICES["en"]))
        pcm = _mp3_to_pcm16(mp3)
        _write_wav(FALLBACK_CLIP, pcm)
        print(f"[tts] fallback clip generated at {FALLBACK_CLIP}")
    except Exception as exc:
        print(f"[tts] could not generate spoken fallback ({exc}); writing chime")
        _write_wav(FALLBACK_CLIP, _chime_pcm())
    return FALLBACK_CLIP


def _write_wav(path, pcm, sample_rate=PLAYBACK_SAMPLE_RATE):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


def _chime_pcm(sample_rate=PLAYBACK_SAMPLE_RATE):
    """A short rising two-tone chime, generated without any external dependency."""
    import math

    samples = bytearray()
    for frequency, duration in ((880.0, 0.12), (1318.5, 0.18)):
        count = int(sample_rate * duration)
        for i in range(count):
            # Fade in and out so the tone does not click at the boundaries.
            envelope = min(1.0, i / 200.0, (count - i) / 400.0)
            value = 0.35 * envelope * math.sin(2 * math.pi * frequency * i / sample_rate)
            samples += struct.pack("<h", int(value * 32767))
    return bytes(samples)
