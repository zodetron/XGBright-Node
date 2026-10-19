# XGBright — Adaptive Lighting with a Vernacular Voice Agent

An ESP32 reads two light sensors and streams them to a laptop. An XGBoost model on the
laptop sets the room's LED panel, replacing an always-100% system. Spoken commands in
English, Hindi, Marathi or Hinglish are transcribed, parsed without an LLM, applied, and
confirmed aloud with the energy saved. Every real session is recorded to a local SQLite
database.

```
  BH1750 x2 ──┐                        ┌── XGBoost  (AUTO brightness)
  microphone ─┤   WebSocket   ┌────────┼── Whisper  (speech → text)
  button ─────┤ ◄───────────► │ laptop ├── rule NLU (text → intent)
  LED panel ◄─┤  (ESP32 is    │ server ├── edge-tts (confirmation)
  speaker ◄───┘   the client) └────────┼── AUTO/MANUAL state machine
                                       ├── rolling window ──► dashboard: Live
                                       └── SQLite history ──► dashboard: History
```

The ESP32 makes no decisions. It reads, streams, and obeys.

## Layout

| Path | What it is |
|---|---|
| `laptop-server/` | The single Python process — WebSocket, ML, STT, NLU, TTS, history, API |
| `dashboard/` | React console (Live / History / Setup) |
| `esp32-firmware/` | Single-file Arduino sketch + `secrets.example.h` |
| `data-pipeline/` | Offline, one-time: builds the training set, picks the model |
| `config/` | `modes.json`, `settings.json` — live-editable from the dashboard |
| `laptop-server/data/history.db` | SQLite history, created on first run (gitignored) |
| `raw/` | Source datasets (gitignored) |

## Running it

```bash
python3.11 -m venv .venv
./.venv/bin/pip install -r laptop-server/requirements.txt
./.venv/bin/python laptop-server/main.py          # server    :8000

cd dashboard && npm install && npm run dev        # dashboard :5180
```

Open **http://localhost:5180** (`#history` and `#setup` for the other tabs).

**Without hardware**, a mock device speaks the real protocol. The dashboard labels it
**SIMULATED DEVICE**, and it is never written to history:

```bash
./.venv/bin/python laptop-server/tools/mock_device.py --seconds 60 --interval 1
./.venv/bin/python laptop-server/tools/mock_device.py --say "make it a lot dimmer"
```

**Flashing the ESP32:**

```bash
cp esp32-firmware/secrets.example.h esp32-firmware/secrets.h    # WiFi + fallback IP
arduino-cli compile --fqbn esp32:esp32:esp32 esp32-firmware/
arduino-cli upload -p /dev/cu.usbserial-0001 --fqbn esp32:esp32:esp32 esp32-firmware/
```

The board finds the laptop by mDNS on every reconnect, so a new DHCP address needs no
reflash. Pins and hardware quirks: `docs/HARDWARE.md`.

## How it behaves

- **AUTO vs MANUAL.** AUTO follows the model once a second. Any voice or dashboard
  command holds a level in MANUAL until a timeout that every new command resets; "auto
  mode" cancels it immediately.
- **Modes are data.** `config/modes.json` is a plain list; a mode added in Setup is
  speakable at once.
- **No LLM in the parser.** Keyword tables and rules across four languages and two
  scripts. What "a little" and "a lot" mean lives in `config/settings.json`.
- **Confirmations wait for the device.** The reply is spoken only after the ESP32
  acknowledges the change. If `edge-tts` fails, a recorded clip plays instead.
- **No fake numbers.** A failed sensor reports why and shows `—`, never 0. Readings
  disappear the moment the device disconnects.

## Stored history

`laptop-server/data/history.db` keeps everything, forever, on this machine only.

| Table | One row per | Contains |
|---|---|---|
| `sessions` | ESP32 connection | device, firmware, connected / disconnected, why it ended |
| `samples` | 5-second window | avg / min / max indoor and outdoor lux, avg brightness, mode, energy vs. a 100% panel |
| `light_changes` | brightness change | from → to, cause (AUTO / voice / typed / dashboard), reason, transcript |

Recording starts at the real device's `hello` and stops at disconnect. The mock device
and `/api/simulate/sensor` are never stored. AUTO moves under 2% are not logged as
changes. About 2 MB a day; the interval is `history_interval_seconds`.

Read it from the **History** tab, as CSV (`/api/history/export/samples.csv`,
`/api/history/export/light_changes.csv`), via `/api/history/{summary,samples,changes,sessions}`
(optional `start` / `end` in unix seconds), or directly:

```bash
sqlite3 laptop-server/data/history.db \
  "SELECT datetime(t,'unixepoch','localtime'), from_brightness, to_brightness, source, transcript
   FROM light_changes ORDER BY t DESC LIMIT 20;"
```

## Documentation

- `PROGRESS.md` — the full handover: how it was built, data, model, hardware, firmware,
  protocol, server, voice, dashboard, decisions, limitations, troubleshooting.
- `docs/HARDWARE.md` — measured pin map and the hardware gotchas. Authoritative for pins.
- `data-pipeline/README.md` — the raw data, the reconstructed outdoor lux, and why
  XGBoost won.
- `laptop-server/protocol.py` — the ESP32 ↔ laptop message schema.
- `TESTING.md` — end-to-end checks, each with what it proves.
