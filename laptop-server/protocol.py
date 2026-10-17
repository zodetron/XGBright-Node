"""
Message schema for the single persistent ESP32 <-> laptop WebSocket
(architectural decision #3).

Control messages are JSON text frames; audio travels as binary frames bracketed by a
begin/end pair. Audio is deliberately *not* base64-in-JSON: five seconds of 16 kHz
16-bit mono is 160 KB, which base64 inflates to 213 KB and forces the ESP32 to hold the
whole thing in RAM to build one giant string. Binary frames stream in fixed-size chunks
the device can fill from a small buffer.

--- ESP32 -> laptop ----------------------------------------------------------
  {"type": "hello",  "device_id": str, "fw": str}   # + "simulated": true from the mock
  {"type": "sensor", "indoor_lux": float, "outdoor_lux": float}
  {"type": "sensor_unavailable", "reason": str}   # no trustworthy reading exists
  {"type": "ack",    "command_id": str, "brightness": float}   # command APPLIED
  {"type": "audio_begin", "sample_rate": int, "format": "pcm_s16le"}
  <binary frames>                                              # raw PCM chunks
  {"type": "audio_end"}

--- laptop -> ESP32 ----------------------------------------------------------
  {"type": "brightness", "command_id": str, "value": float}    # 0.0 - 1.0
  {"type": "record", "duration_ms": int}                       # start a capture
  {"type": "sweep"}                                            # panel self-test
  {"type": "audio_begin", "sample_rate": int, "format": "pcm_s16le", "bytes": int}
  <binary frames>
  {"type": "audio_end"}
  {"type": "status", "message": str}

The `ack` is load-bearing: the spoken confirmation is gated on it (decision #13), so
the user is never told the light changed before it actually did.
"""

# Message type constants, so a typo is an ImportError rather than a silent no-op.
HELLO = "hello"
SENSOR = "sensor"
SENSOR_UNAVAILABLE = "sensor_unavailable"
ACK = "ack"
AUDIO_BEGIN = "audio_begin"
AUDIO_END = "audio_end"
BRIGHTNESS = "brightness"
RECORD = "record"
SWEEP = "sweep"
STATUS = "status"

AUDIO_SAMPLE_RATE = 16_000
AUDIO_FORMAT = "pcm_s16le"

# Chunk size for outbound audio. Small enough for a comfortable ESP32 receive buffer,
# large enough that a 10-second confirmation is not thousands of frames.
AUDIO_CHUNK_BYTES = 2048

# How long to wait for the device to confirm a brightness command before giving up on
# the gated-confirmation flow and speaking anyway.
ACK_TIMEOUT_SECONDS = 2.0

# A device that has sent nothing for this long is treated as disconnected.
DEVICE_TIMEOUT_SECONDS = 15.0

# Default push-to-talk capture length. Long enough for a full sentence in any of the
# supported languages, short enough that the confirmation still feels prompt.
RECORD_DURATION_MS = 5000
