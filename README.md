# Smart Adaptive Lighting with a Vernacular Voice Agent

An ESP32 reads two light sensors and streams them to a laptop. A model on the laptop
decides how bright the room's panel should be, replacing a naive always-100% system.
A button on the device records a spoken command in English, Hindi, Marathi or Hinglish;
the laptop transcribes it, parses it without an LLM, applies it, and speaks back a
confirmation stating both what changed and what it saved. Every real session is
recorded to a local SQLite database, so lighting changes, sensor readings and energy
use can be reviewed and exported afterwards.

## Layout

| Path | What it is |
|---|---|
| `raw/` | Source datasets (not modified) |
| `data-pipeline/` | Offline, one-time: builds the training set, picks the model |
| `laptop-server/` | The single Python process — WebSocket, ML, STT, NLU, TTS, API |
| `dashboard/` | React console |
| `esp32-firmware/` | Single-file Arduino sketch |
| `config/` | Persisted settings: modes, settings |
| `laptop-server/data/history.db` | SQLite history of real-device sessions (created on first run, gitignored) |
| `PROGRESS.md` | Task status, deviations, and known limitations |
| `TESTING.md` | End-to-end manual test plan |

## Running it

```bash
python3.11 -m venv .venv
./.venv/bin/pip install -r laptop-server/requirements.txt
./.venv/bin/python laptop-server/main.py          # :8000

cd dashboard && npm install && npm run dev        # :5180
```

Open **http://localhost:5180** for the live view, or **http://localhost:5180/#history**
for everything recorded so far.

No hardware needed to try it — the server exposes simulation endpoints and there is a
mock device that speaks the real protocol. Neither is ever written to the history
database:

```bash
./.venv/bin/python laptop-server/tools/mock_device.py --seconds 60 --interval 1
./.venv/bin/python laptop-server/tools/mock_device.py --say "make it a lot dimmer"
```

To flash the ESP32, put your WiFi credentials and the laptop's IP in a local
`secrets.h` (gitignored, never committed), then compile and upload:

```bash
cp esp32-firmware/secrets.example.h esp32-firmware/secrets.h    # then edit it
arduino-cli compile --fqbn esp32:esp32:esp32 esp32-firmware/
arduino-cli upload -p /dev/cu.usbserial-0001 --fqbn esp32:esp32:esp32 esp32-firmware/
```

The board finds the laptop by its mDNS name first, so a changed DHCP address does not
need a reflash; the IP in `secrets.h` is the fallback. Pins are in `docs/HARDWARE.md`.

## How it fits together

```
  BH1750 x2 ──┐                        ┌── XGBoost  (AUTO brightness)
  microphone ─┤   WebSocket   ┌────────┼── Whisper  (speech → text)
  button ─────┤ ◄───────────► │ laptop ├── rule NLU (text → intent)
  LED panel ◄─┤               │ server ├── edge-tts (confirmation)
  speaker ◄───┘               └────────┼── mode state machine
                                       ├── rolling window ──► dashboard: Live
                                       └── SQLite history ──► dashboard: History
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

## Stored history (SQLite)

The live chart is an in-memory window that resets on restart. Alongside it, the server
keeps a permanent record in **`laptop-server/data/history.db`**. It's a single SQLite file,
created on first run, kept forever and gitignored, so it never leaves the machine.

**What's recorded**

| Table | One row per | Contains |
|---|---|---|
| `sessions` | ESP32 connection | device id, firmware, connected / disconnected time, why it ended |
| `samples` | 5-second window | avg / min / max indoor and outdoor lux, avg brightness, mode, energy used vs. a 100% panel |
| `light_changes` | brightness change | from → to, who caused it (AUTO model, voice, typed command, dashboard), reason, voice transcript |

Each 5-second row averages every reading in that window rather than keeping one, so a
short spike still shows in min/max. The interval is `history_interval_seconds` in
`config/settings.json`. AUTO adjustments under 2% are treated as model jitter and not
logged as changes; voice and dashboard changes always are. That comes to about 2 MB a day.

**Only real measurements go in.** Recording starts when a real ESP32 says `hello` and
stops when it disconnects. With no device connected, nothing is written, so a gap in the
data is a real gap. The mock device and `/api/simulate/sensor` are never stored. A failed
sensor is stored as empty with the device's reason, never as 0.

**Ways to read it**

- **Dashboard → History tab:** pick a range or a day. You get energy and savings
  totals, panel-output and lux charts, the light-change log and the session list.
- **CSV export:** buttons on the History tab, or
  `GET /api/history/export/samples.csv` and `/api/history/export/light_changes.csv`.
- **API:** `/api/history/summary`, `/samples`, `/changes`, `/sessions`. All take
  optional `start` / `end` in unix seconds.
- **Directly:**
  ```bash
  sqlite3 laptop-server/data/history.db \
    "SELECT datetime(t,'unixepoch','localtime'), from_brightness, to_brightness, source, transcript
     FROM light_changes ORDER BY t DESC LIMIT 20;"
  ```

The design and its rules are written up in `CONTEXT.md` §24.1.

## Documentation

- `data-pipeline/README.md` — what the raw data actually contains, where outdoor lux
  comes from, how the target is defined, and why XGBoost won.
- `laptop-server/protocol.py` — the full ESP32 ↔ laptop message schema.
- `CONTEXT.md` — full technical explanation end to end, including the history
  database (§24.1).
- `TESTING.md` — 15 end-to-end checks, each with what it proves.
- `PROGRESS.md` — deviations from the original spec and every known limitation.
