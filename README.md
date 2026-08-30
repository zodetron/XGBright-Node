# Smart Adaptive Lighting with a Vernacular Voice Agent

An ESP32 reads two light sensors and streams them to a laptop. A model on the laptop
decides how bright the room's panel should be, replacing a naive always-100% system.
A button on the device records a spoken command in English, Hindi, Marathi or Hinglish;
the laptop transcribes it, parses it without an LLM, applies it, and speaks back a
confirmation stating both what changed and what it saved.

## Layout

| Path | What it is |
|---|---|
| `raw/` | Source datasets (not modified) |
| `data-pipeline/` | Offline, one-time: builds the training set, picks the model |
| `laptop-server/` | The single Python process — WebSocket, ML, STT, NLU, TTS, API |
| `dashboard/` | React console |
| `esp32-firmware/` | Single-file Arduino sketch |
| `config/` | The only persisted state: modes, settings |
| `PROGRESS.md` | Task status, deviations, and known limitations |
| `TESTING.md` | End-to-end manual test plan |

## Running it

```bash
python3.11 -m venv .venv
./.venv/bin/pip install -r laptop-server/requirements.txt
./.venv/bin/python laptop-server/main.py          # :8000

cd dashboard && npm install && npm run dev        # :5180
```

No hardware needed to try it — the server exposes simulation endpoints and there is a
mock device that speaks the real protocol:

```bash
./.venv/bin/python laptop-server/tools/mock_device.py --seconds 60 --interval 1
./.venv/bin/python laptop-server/tools/mock_device.py --say "make it a lot dimmer"
```

To flash the ESP32, fill in the pin numbers and WiFi credentials at the top of
`esp32-firmware/adaptive_lighting.ino` — every pin is a named `-1` placeholder and the
firmware refuses to boot until they are set, naming the one that is missing.

## How it fits together

```
  BH1750 x2 ──┐                        ┌── XGBoost  (AUTO brightness)
  microphone ─┤   WebSocket   ┌────────┼── Whisper  (speech → text)
  button ─────┤ ◄───────────► │ laptop ├── rule NLU (text → intent)
  LED panel ◄─┤               │ server ├── edge-tts (confirmation)
  speaker ◄───┘               └────────┼── mode state machine
                                       └── rolling window ──► React dashboard
```

The ESP32 makes no decisions. It reads, streams, and obeys.

## The parts worth knowing about

**AUTO vs MANUAL.** AUTO follows the model. Any voice command or dashboard override
holds a fixed level in MANUAL, reverting after a configurable timeout that *every* new
command resets. Saying "auto mode" cancels the hold immediately.

**Modes are data, not code.** `config/modes.json` is a plain list; the built-in entries
are not special-cased. A mode added from the dashboard is speakable a second later.

**The NLU has no LLM in it.** It is keyword tables plus rules, in four languages and
two scripts, covering mode names, directions, magnitudes and explicit quantities. What
"a little" and "a lot" mean in percent lives in `config/settings.json`.

**Confirmations are gated on the device.** The spoken reply waits for the ESP32 to
acknowledge that it actually applied the change, so the user is never told the light
moved before it did. If `edge-tts` stalls or fails, a pre-recorded clip plays instead —
never silence.

**Nothing is persisted but config.** Live readings, chart history and energy totals are
an in-memory rolling window. There is no database anywhere in this project.

## Documentation

- `data-pipeline/README.md` — what the raw data actually contains, where outdoor lux
  comes from, how the target is defined, and why XGBoost won.
- `laptop-server/protocol.py` — the full ESP32 ↔ laptop message schema.
- `TESTING.md` — 15 end-to-end checks, each with what it proves.
- `PROGRESS.md` — deviations from the original spec and every known limitation.
