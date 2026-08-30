# End-to-End Test Plan

Manual verification steps for the whole system. Each section says what to do, what
should happen, and what it proves.

## 0. Prerequisites

```bash
# One-time
python3.11 -m venv .venv
./.venv/bin/pip install -r laptop-server/requirements.txt
cd dashboard && npm install

# Every session
./.venv/bin/python laptop-server/main.py     # server on :8000
cd dashboard && npm run dev                  # dashboard on :5180
```

The first voice command downloads the Whisper model (~1.5 GB) unless the server has
already warmed it at startup. `edge-tts` needs internet; without it the system falls
back to the pre-recorded clip, which is itself a test case below.

Everything except the hardware sections can be exercised with the mock device:

```bash
./.venv/bin/python laptop-server/tools/mock_device.py --seconds 60 --interval 1
```

---

## 1. Sensor reading flow

**Do:** Start the server, then the mock device (or the real ESP32).

**Expect:** `[device] hello from …` in the server log. The dashboard's ESP32 dot turns
green. Indoor/outdoor lux tiles update once per second. The mock prints `panel -> NN%`
for every reading.

**Proves:** The WebSocket carries telemetry in and brightness commands out, and the
device acknowledges each command.

---

## 2. AUTO-mode prediction

**Do:** Inject readings across the range without hardware:

```bash
curl -sX POST localhost:8000/api/simulate/sensor -H 'content-type: application/json' \
     -d '{"indoor_lux":450,"outdoor_lux":80000}'   # bright daylight
curl -sX POST localhost:8000/api/simulate/sensor -H 'content-type: application/json' \
     -d '{"indoor_lux":5,"outdoor_lux":0}'         # dark room, night
```

**Expect:** Bright daylight returns a brightness at or near `0.0`. A dark room returns
a non-zero value that depends on the time of day — low in the small hours (the model
learned that nobody is there), higher during working hours.

**Proves:** The exported model loads, the feature vector is built in the right order,
and AUTO responds to both light level and time of day.

---

## 3. Voice command — mode switch

**Do:** Type `office mode` into the dashboard's Voice box, or say it into the mic.

**Expect:** The mode tile flips to `MANUAL` in violet, showing `Office` and a countdown.
The Voice card shows a `MODE` badge, the language, the transcript, and a spoken
confirmation naming the mode, the resulting percentage, and the savings figure.

**Proves:** Mode matching against the live config, the MANUAL transition, and the
Option-C confirmation template.

---

## 4. Voice command — relative adjustment

**Do:** Send `make it a little brighter`, then `thoda tez karo`, then `खूप कमी कर`.

**Expect:** Each moves brightness by the configured step — 10% for "a little"/"thoda",
45% for "खूप" — from the level currently in effect. The language badge reads `en`,
`hinglish`, `mr` respectively, and each confirmation is spoken in that language.

**Proves:** Direction and magnitude parsing across scripts, configurable step sizes,
and per-language voice selection.

---

## 5. Voice command — absolute values

**Do:** Send `set to 60 percent`, then `2000 lux`, then `set it to sixty`.

**Expect:** 60%, a brightness computed to reach 2000 lux given current daylight, and
60% again (a bare number is read as a percentage).

**Proves:** Quantity extraction, unit disambiguation, and spelled-out numbers.

---

## 6. Manual timeout and reset

**Do:** Set the manual timeout to 60 seconds in Advanced Settings. Send any command.
Wait 40 seconds, send another. Wait 70 seconds without sending anything.

**Expect:** The countdown restarts at 60 on the second command — it is not cumulative.
After the final 70-second wait the mode tile returns to `AUTO` on its own.

**Proves:** The timer resets on *any* command and the lazy expiry fires.

---

## 7. Explicit auto override

**Do:** From MANUAL, send `auto mode` (or `apne aap`, or press "Return to auto").

**Expect:** The mode flips to AUTO immediately, without waiting for the countdown, and
the active mode name clears.

**Proves:** The explicit-auto escape hatch bypasses the timer.

---

## 8. TTS success path

**Do:** With internet available, send any valid command.

**Expect:** The server logs `[controller] spoke N.Ns via edge-tts`. The mock device
saves the returned audio to `tools/last_playback.wav`; play it and confirm it states
both the new value and the savings percentage, in the command's language.

**Proves:** edge-tts generation, MP3→PCM decode, and streaming playback to the device.

---

## 9. TTS fallback path

**Do:** Disconnect the laptop from the internet (leave the ESP32's LAN reachable), then
send a command. Alternatively set `GENERATION_TIMEOUT_SECONDS` in `voice/tts.py` to
`0.01` to force a timeout.

**Expect:** The server logs `[tts] edge-tts unavailable (…); using fallback clip` and
the device still receives and plays audio — the pre-recorded `assets/done.wav`. The
brightness change still applies.

**Proves:** The user never gets silence, and the failure is contained to the audio.

---

## 10. Confirmation gating

**Do:** Stop the mock device mid-session, then send a voice command through the
dashboard.

**Expect:** The server logs `device did not acknowledge within 2.0s`, and the Voice
card shows "device did not acknowledge" in red. The confirmation is still spoken.

**Proves:** The confirmation waits on the device's `ack` rather than assuming success,
and degrades sensibly when there is nothing to wait for.

---

## 11. WiFi drop and recovery

**Do:** With the real ESP32 running at a visible brightness, power off the router or
disable the laptop's WiFi.

**Expect:** The panel *holds its last brightness* — it must not jump to full or go
dark. The serial monitor prints `[ws] disconnected — holding last brightness`. On
restoring the network the device reconnects within a few seconds, sends `hello`, and
resumes taking commands.

**Proves:** The fallback in `loop()` and the reconnect path.

---

## 12. Dashboard live updates

**Do:** Watch the dashboard with the device streaming.

**Expect:** The chart advances once per second. The amber area is actual draw, the teal
band above it is the saving against always-100%, and together they reach the dashed
ceiling. Hovering shows actual watts, 100% watts, and the saved percentage. Older
points fall off the left edge once the window fills.

**Proves:** The WebSocket fan-out, the rolling window eviction, and the power model.

---

## 13. Dashboard CRUD

**Do:** Add a mode ("Movie Night", 60 lux). Confirm it appears in both the mode chips
and the list. Send `switch to movie night` by voice. Edit its target, then delete it.

**Expect:** The new mode is voice-matchable immediately with no server restart. Editing
and deleting take effect at once, and `config/modes.json` on disk reflects each change.
Deleting the mode currently held returns the system to AUTO.

**Proves:** Live config reload, and that modes are ordinary data rather than special
cases in code.

---

## 14. Settings persistence

**Do:** Change "A little" to 5% and save. Send `a little brighter`. Restart the server
and check the value survived.

**Expect:** The step applied is 5%. `config/settings.json` holds the new value after
restart.

**Proves:** Settings are read live by the parser and written through to disk.

---

## 15. Unrecognised input

**Do:** Send `make me a sandwich`.

**Expect:** An `UNKNOWN` badge, no change to brightness or mode, and a spoken "sorry, I
did not understand" in the detected language.

**Proves:** The parser declines rather than guessing — an unparsed command must never
move the lights.
