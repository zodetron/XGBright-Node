# Handoff — Adaptive Lighting + Vernacular Voice Agent

Written 2026-08-25. Read this first when picking the project back up, then
`PROGRESS.md` for the task checklist and `docs/HARDWARE.md` for anything touching the
board.

---

## 1. Where things actually stand

**All six phases of the build spec are implemented, and the whole system has run end
to end on real hardware.** What remains is verification of two physical things, not
unwritten code.

### Confirmed working on the real board

| | Evidence |
|---|---|
| Both BH1750s | `[i2c] scanning: 0x23 0x5C [2 device(s)]`, streaming real lux at 1 Hz |
| INMP441 mic | Captures signal; slot auto-detect picks LEFT every boot |
| WiFi | Joins the configured 2.4 GHz network, gets a DHCP lease, ~−63 dBm |
| WebSocket | `[device] hello from esp32-room-1 (fw 2.0.0)` |
| AUTO mode | XGBoost drives panel from live sensor readings |
| Voice loop | `received 5.0s of audio → transcript → spoke 3.9s via edge-tts` |
| PWM path | `[light] commanded 100% -> duty 255/255 on GPIO 23` |
| Dashboard | Live chart, modes CRUD, settings, push-to-talk |

### Not yet verified — do these first

1. **Does the LED physically light?** The panel was disconnected during testing, so
   "off" was correct behaviour, not a fault. The software path is proven down to the
   duty cycle. Reconnect it and press **Test panel** in the dashboard (or
   `POST /api/light/sweep`) — it ramps 0→255→0 over ~2.5 s. If it stays dark while the
   serial log shows duty changing, it is wiring; since it goes through a MOSFET, first
   suspect is an active-LOW driver, fixed by `#define LIGHT_INVERT_OUTPUT 1`.
2. **Whisper on your actual voice.** Every voice test so far captured silence (I
   triggered recordings remotely with nobody speaking — it correctly heard nothing and
   said "sorry, I did not understand"). Accuracy on your voice, accent and the
   Hindi/Marathi/Hinglish commands is still unproven.

---

## 2. Running it

```bash
cd /Users/hardik/Projects/MOODSENSE

./.venv/bin/python laptop-server/main.py     # server  :8000
cd dashboard && npm run dev                  # console :5180
```

Dashboard: **http://localhost:5180**. Port 5180 is deliberate — 5173/5174 are usually
taken by your BolBiz project.

No hardware needed to work on the software:

```bash
./.venv/bin/python laptop-server/tools/mock_device.py --seconds 60 --interval 1
./.venv/bin/python laptop-server/tools/mock_device.py --sweep 4      # a day in 4 min
./.venv/bin/python laptop-server/tools/mock_device.py --say "make it a lot dimmer"
```

Flashing:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 <sketch-dir>
arduino-cli upload -p /dev/cu.usbserial-0001 --fqbn esp32:esp32:esp32 <sketch-dir>
```

`arduino-cli`, the ESP32 core 3.3.11, the three libraries and Rosetta are all already
installed.

---

## 3. Architecture, and why

The ESP32 is a **pure I/O peripheral**. It reads sensors, streams them out, records
audio on request, and does what it is told. Whisper, the brightness model, the parser
and TTS all live on the laptop.

```
  BH1750 x2 ──I2C──┐                     ┌── XGBoost  (AUTO brightness)
  INMP441 ──I2S0──►│  ESP32    WebSocket │── Whisper  (speech → text)
  MAX98357A ◄I2S1──│  (client) ◄────────►│── rule NLU (text → intent)
  LED ◄──PWM 23────│                     │── edge-tts (confirmation)
  Button ──19─────►└─────────────────────┴── React dashboard
```

**The ESP32 is a WebSocket _client_ that dials out to the laptop.** This is the single
most important thing to remember, because your other project
(`CheckForESP_Firmware`) is the opposite: there the ESP32 *hosts* a server on port 81
and a browser connects to it directly.

Consequences of this project's direction, all of which caused confusion at least once:
- The board **never creates an access point**, so it will never appear in a WiFi list.
- There is **no page to browse at the ESP32's IP**.
- If the laptop server is down, the board just retries; light and sensors keep working.

Wire schema: `laptop-server/protocol.py`.

---

## 4. Hardware essentials

Full detail in `docs/HARDWARE.md`. The parts that bite:

| Peripheral | GPIO |
|---|---|
| LED panel (PWM, 5 kHz, 8-bit) | 23 |
| Button (`INPUT_PULLUP`, active LOW) | 19 |
| I2C SDA / SCL | 21 / 22 |
| BH1750 indoor / outdoor | `0x23` / `0x5C` |
| INMP441 SCK / WS / SD | 32 / 33 / 13 |
| MAX98357A BCLK / LRC / DIN | 26 / 25 / 27 |

**Three things that break the board:**

1. **No `analogRead()` anywhere.** The I2S driver and Arduino's ADC register
   conflicting ESP-IDF drivers; the chip aborts during init and boot-loops before
   `setup()` prints a thing. This actually happened — see §5.
2. **MCLK must be explicitly unset** on both I2S peripherals or they fight over GPIO 0.
3. **`loop()` must never block.** Audio runs in tasks on core 0.

No PSRAM: ~148 KB free heap at runtime, so audio streams and is never buffered whole.

---

## 5. What went wrong, and what fixed it

Kept because most of these are non-obvious and will otherwise be rediscovered.

### Boot loop on first flash
`driver/i2s.h` (the legacy API) compiles fine on core 3.x but links the old ADC driver,
which the new driver stack refuses to coexist with. The board aborted during init and
rebooted forever, printing nothing of ours. **A clean compile proved nothing** — only
flashing caught it. Fixed by migrating to `driver/i2s_std.h`.

### `ledcSetup` / `ledcAttachPin` do not exist on core 3.x
Replaced by `ledcAttach(pin, freq, res)` and `ledcWrite(pin, duty)`. There is a version
shim in the firmware so it builds on both.

### Microphone would have been silent
The firmware originally hardcoded the LEFT I2S slot. Your test firmware documents the
INMP441's L/R pin as floating, with the active slot changing between boots. Ported your
auto-detect: it measures peak-to-peak spread and reinstalls on the other slot if flat.
**On this board it picks LEFT every time — the opposite of your test firmware's
default.** Without this, every recording would have been silent.

### The target network did not exist
Hours of "it won't connect" traced to the network simply not being there. Added a
boot-time scan that lists what *is* in range and says whether the target was found —
this separates "wrong password" from "that network is not here". The board is now on
a network that exists.

Note the scan must run from an **idle radio**: scanning during an in-flight association
returns zero networks and looks exactly like a dead antenna.

### The laptop's IP moved mid-session
The DHCP lease jumped to a different subnet. `SERVER_HOST` is a compile-time constant, so every
address change needs a reflash. **Check `ipconfig getifaddr en0` matches `SERVER_HOST`
before flashing.** Switching to mDNS (`<your-laptop>.local`) is an open
improvement.

### A second WebSocket client orphaned the ESP32
Connecting a test client silently overwrote `controller.device`, leaving the board
streaming while the server believed nothing was attached. Now a new connection closes
the previous one properly.

### Fabricated data — two instances, both fixed
1. I left a **mock device** running and showed you a dashboard full of simulated lux
   as if it were real. The mock is now opt-in only and never started without saying so.
2. Failed or absent sensors were **clamped to 0 and sent as real readings**. Now they
   send `sensor_unavailable` with a reason; the server refuses to run AUTO without a
   real reading, and the dashboard shows `—` rather than a stale value.

Related: the dashboard used to keep showing the last readings after a disconnect. It
now distinguishes *"a device is attached"* from *"it is supplying readings"*.

### Push-to-talk nearly used the wrong microphone
The dashboard button originally fell back to the laptop mic when the ESP32 was offline.
Now it **only ever** uses the device mic; the laptop mic is behind a separate, clearly
labelled control that only appears when no device is attached, and its results are
tagged **via laptop mic** in red.

---

## 6. The ML pipeline

Offline and one-time. `combine.py` → `features.py` → `train.py`.

The raw archives are Singapore office submetering. **Floors 2–7 have real per-zone light
sensors** (`zN_S1(lux)`) alongside lighting-power meters — I initially said they didn't
and was wrong. `archive (3).zip` duplicates `archive (2)`. ASHRAE db-II is excluded: a
thermal-comfort survey with no illuminance and no join key.

Two things to know before trusting the numbers:
- **`zN_S1(lux)` is a relative index**, integer-valued and capped near 90, not absolute
  lux. Calibrated onto a physical scale by constants in `data-pipeline/config.py`.
- **Outdoor lux was never measured.** It is reconstructed from the real indoor sensor
  back-projected through a NOAA clear-sky solar model for Singapore.

Results on a chronological split (random would leak — adjacent 5-min samples are
near-duplicates):

| Model | MAE | R² |
|---|---|---|
| Random Forest | 0.1304 | 0.609 |
| **XGBoost (selected)** | **0.1328** | **0.608** |
| LightGBM | 0.1339 | 0.607 |
| MLP 64-32 | 0.1426 | 0.580 |
| Ridge | 0.1958 | 0.366 |
| *Always 100% (incumbent)* | *0.6432* | *−3.820* |

Random Forest won on raw MAE but serialises to 181.7 MB against XGBoost's 8.5 MB, so
`train.py` treats models within 2% relative MAE as tied and picks the smallest.

---

## 7. Open items

**Should do next**
- Verify the LED physically ramps (§1).
- Speak real commands and check Whisper's accuracy across all four languages.
- Tie the INMP441's L/R pin to GND so the slot stops needing auto-detection.

**Worth doing**
- Switch `SERVER_HOST` to mDNS so DHCP changes stop requiring a reflash.
- The panel sweep blocks `loop()` for ~2.4 s. Deliberate for a human-triggered
  diagnostic, but it is the one remaining blocking call.
- Link margin is ~−63 dBm. Your notes record streaming working at −62 and stuttering at
  −79. Only the spoken confirmation needs sustained 31.2 KB/s; if it crackles, that is
  why, not the audio code.

**Known limitations** (full list at the bottom of `PROGRESS.md`)
- Model MAE 0.133 means commanded brightness is off by ~13 percentage points on
  average. Useful against an always-100% incumbent, not a precise controller. No
  feedback loop — it maps a reading to a command without checking the result.
- Trained on deep-plan office zones (daylight factors 0.0003–0.025). A cardboard-box
  room with a large opening may be outside that range.
- `edge-tts` needs internet. The pre-recorded fallback covers failure.
- No auth on the API or either WebSocket. Fine on a trusted LAN.
- The live chart and session energy totals are in-memory and reset on restart. Real
  device sessions are logged to `laptop-server/data/history.db` (added 2026-10-17, see
  `CONTEXT.md` §24.1) and shown in the dashboard's History tab.

---

## 8. File map

| Path | What |
|---|---|
| `esp32-firmware/adaptive_lighting.ino` | Single-file firmware. Section 1 is the only part to edit. |
| `laptop-server/main.py` | Entrypoint — WebSocket, REST, lifecycle |
| `laptop-server/controller.py` | Decision layer: telemetry → command, transcript → action |
| `laptop-server/state.py` | AUTO/MANUAL machine, revert timer |
| `laptop-server/protocol.py` | **The wire schema. Read this before touching messages.** |
| `laptop-server/voice/nlu.py` | Rule-based parser, four languages, no LLM |
| `laptop-server/tools/mock_device.py` | Fake ESP32 speaking the real protocol |
| `dashboard/src/components/VoicePanel.tsx` | Push-to-talk + intent display |
| `config/*.json` | Persisted modes and settings |
| `laptop-server/history.py` | SQLite history: 5 s buckets, light changes, sessions |
| `laptop-server/data/history.db` | The history itself — gitignored, kept forever |
| `dashboard/src/components/HistoryView.tsx` | The History tab |
| `docs/HARDWARE.md` | Authoritative for pins and quirks |
| `PROGRESS.md` | Task checklist, deviations, limitations |
| `TESTING.md` | 15 end-to-end checks |

`WIFI_SSID`, `WIFI_PASSWORD` and `SERVER_HOST` in firmware Section 1.1 are
**placeholders in the repo** — fill in your own before flashing. Never commit real ones.

---

## 9. Session checklist

1. `ipconfig getifaddr en0` — does it match `SERVER_HOST` in the firmware? If not,
   update and reflash.
2. Start server and dashboard.
3. Power the ESP32; watch the serial monitor at 115200 for the boot report.
4. Confirm `device_connected: true` and both lux values moving.
5. If anything is odd, the serial log names the fault — the boot sequence reports the
   I2C scan, mic slot, WiFi scan, panel sweep and free heap in order.
