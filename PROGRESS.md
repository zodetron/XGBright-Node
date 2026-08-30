# PROGRESS

Master task list for the Smart Adaptive Lighting System. Update immediately on task
completion — never batch. New sessions: read this file first, find the first unchecked
task, re-read relevant existing files, continue from there.

---

## Phase 0 — Project Scaffolding
- [x] Create project root structure: `raw/`, `data-pipeline/`, `esp32-firmware/`, `laptop-server/`, `dashboard/`, `config/`, `PROGRESS.md`
- [x] Initialize `PROGRESS.md` with this full task list, all unchecked

## Phase 1 — Data Pipeline & Model Selection
- [x] Script to load and combine all datasets from `raw/` into one unified dataset
- [x] Preprocessing/cleaning/feature engineering script
- [x] Train/test split
- [x] Train candidate models: baseline regression, Random Forest, gradient boosting (XGBoost or LightGBM), optional small neural net
- [x] Evaluate all candidates (MAE, RMSE, R² at minimum), log results to a comparison table/file
- [x] Auto-select best model based on evaluation metrics
- [x] Export/serialize winning model to `laptop-server/model/` in a format loadable at server startup
- [x] Write a short `data-pipeline/README.md` documenting which model won and why (metrics)

## Phase 2 — Laptop Server Core
- [x] Set up single Python process entrypoint
- [x] WebSocket server — message schema for: sensor readings (in), audio blob (in), command confirmation (in), brightness command (out), audio playback (out)
- [x] Load trained ML model at startup, implement AUTO-mode prediction call
- [x] In-memory rolling window structure: recent readings, current panel %, computed watts, timestamps
- [x] Linear power model: config constant for rated wattage, actual vs hypothetical-100% watts
- [x] Mode state machine: AUTO/MANUAL, active mode name, target, configurable timer w/ reset-on-command, auto-revert, explicit "auto mode" override
- [x] Config files (JSON): modes list, magnitude step sizes, MANUAL timeout — load at startup, live-updatable

## Phase 3 — Voice Pipeline
- [x] Integrate MLX Whisper (`mlx-community/whisper-large-v3-turbo`) for STT
- [x] Rule-based NLU parser:
  - [x] Mode-name matching against live modes config
  - [x] Direction-word parsing
  - [x] Magnitude-word parsing mapped to configurable step sizes
  - [x] Explicit number/percentage parsing
  - [x] Resolve parsed intent into concrete action
- [x] Wire parsed intent into mode state machine
- [x] TTS: `edge-tts` with the four specified voices + language selection
- [x] TTS confirmation text generator (Option C: action + new value + live savings %)
- [x] TTS fallback: pre-recorded static clip on failure/timeout, gated on ESP32 applied-confirmation

## Phase 4 — React Dashboard
- [x] Project scaffold + deliberate design system
- [x] Live 1-second energy chart: actual vs hypothetical-100%, rolling window
- [x] Current status panel: mode, indoor/outdoor lux, panel %
- [x] Manual override controls
- [x] Custom modes manager: add/edit/delete
- [x] Advanced settings panel: magnitude steps, MANUAL timeout
- [x] Connect dashboard to laptop server backend

## Phase 5 — ESP32 Firmware (single file)
- [x] Single file with: pin declarations block (placeholders), WiFi/WebSocket setup, sensor loop, button-triggered recording, command handler, audio playback handler, WiFi-drop fallback
- [x] Inline comments throughout

## Phase 6 — Integration & End-to-End Test Plan
- [x] Document manual test steps
- [x] Note known limitations/assumptions

---

## Deviations

- **ASHRAE db-II excluded from the combined dataset.** It is a per-subject thermal-comfort
  survey with no illuminance and no lighting power, sharing no join key with the
  submetering data. Merging it would add only nulls. (`data-pipeline/README.md`)
- **Outdoor lux is reconstructed, not measured.** No dataset in `raw/` contains outdoor
  illuminance. It is derived from real indoor sensor readings back-projected through a
  NOAA solar-position clear-sky model. Approved by the user before implementation.
- **Model selection uses an efficiency tie-break.** Random Forest had the lowest raw MAE
  but serialises to 181.7 MB; XGBoost is within 1.8% relative MAE at 8.5 MB. `train.py`
  treats models within 2% relative MAE as tied and decides on serialised size, because
  the artifact is co-resident with Whisper in the live server.
- **TTS decodes MP3 via `soundfile`, not ffmpeg.** The spec did not name a decoder; using
  libsndfile (already a dependency) keeps the server free of any external binary
  dependency, so it runs from a clean `pip install` with nothing else to set up.
- **`zN_S1(lux)` treated as a relative index, not absolute lux.** The channel is integer
  valued and caps near 90; it is calibrated onto a physical scale via constants in
  `data-pipeline/config.py`. Absolute lux figures downstream inherit that assumption.

---

## Known Limitations & Assumptions

### Data and model

- **The training illuminance is a relative index, not absolute lux.** The raw
  `zN_S1(lux)` channel is integer-valued and caps near 90, while a real office task
  plane sits at 300–500 lux. It is calibrated onto a physical scale by constants in
  `data-pipeline/config.py`. Every absolute lux figure downstream inherits that
  assumption — it is the first thing to re-check against real BH1750 readings if the
  physical system dims too aggressively.
- **Outdoor lux was never measured.** It is reconstructed from the real indoor sensor
  back-projected through a NOAA clear-sky solar model for the source building's
  location (Singapore). The method is documented in `data-pipeline/README.md`.
- **The setpoint policy is a design choice, not an observation.** 300 lux occupied /
  50 lux unoccupied defines the regression target. A different policy would produce a
  different model; nothing in the data dictates these numbers.
- **R² is 0.61, MAE 0.133.** The commanded brightness is off by roughly 13 percentage
  points on average. That is genuinely useful against an always-100% incumbent (MAE
  0.643) but it is not a precise controller, and the model has no feedback loop — it
  maps a reading to a command without checking whether the room reached the setpoint.
- **The model was trained on deep-plan office zones**, whose daylight factors run
  0.0003–0.025. A cardboard-box room model with a large opening may sit outside that
  range, in which case the model is extrapolating.

### Voice

- **`edge-tts` requires internet** even though it runs as a local library. Accepted
  tradeoff per the project spec; the pre-recorded fallback covers the failure.
- **Confirmations are long** — 7–10 seconds in Hindi and Marathi, because they state
  the action, the new value and the savings figure. Shorter templates are a one-line
  change in `voice/tts.py` if that proves tiresome in use.
- **Whisper is ~1.5 GB and loads once at startup.** First-run download is slow; the
  server warms it in the background and stays usable meanwhile.
- **The NLU vocabulary is hand-built.** It covers the direction, magnitude, auto and
  number words listed in `voice/nlu.py` across English, Hindi, Marathi and romanised
  Hinglish. Dialect words outside those tables will not match — adding one is a
  one-line data change, not a code change.
- **Language detection is heuristic**, based on script plus vocabulary. A command that
  is entirely English words spoken by a Hindi speaker will be answered in English.

### System

- **No persistence beyond the three config files.** Telemetry, chart history and energy
  totals are in memory only; restarting the server starts them over. This is
  architectural decision #8, not an oversight.
- **The energy model is linear** — X% brightness draws X% of rated wattage. Real LED
  drivers are not perfectly linear, especially at the bottom of the range.
- **Energy totals ignore gaps longer than 10 seconds**, so a WiFi dropout does not
  inject a fictional block of energy into the running total. Long outages therefore
  under-report both actual and hypothetical usage equally; the savings *percentage*
  stays correct.
- **One device at a time.** The server tracks a single `controller.device`; a second
  ESP32 connecting would displace the first.
- **No authentication on the API or either WebSocket.** Fine on a trusted LAN, not
  fine on an untrusted network.

### Firmware

- **Boots on real hardware; peripherals still unverified.** Flashed to the board and
  confirmed to reach `setup()` and start WiFi. The sensors, mic, speaker, panel and
  button have not yet been exercised on hardware — only the boot path has.
- **Fixed after first flash:** the original build used the legacy `driver/i2s.h`, which
  links ESP-IDF's old ADC driver and aborts at boot on core 3.x
  (`ADC: CONFLICT! driver_ng is not allowed to be used with the legacy driver`). It
  compiled clean, so only flashing caught it. Now on `driver/i2s_std.h`.
- **Pins and audio constants now come from measurement**, not assumption — see
  `docs/HARDWARE.md`. Corrected against the first draft: mic shift is `>> 15` (was
  `>> 11`), a DC-blocking high-pass is required (the INMP441 idles near -22000), the
  speaker DAC runs at 32 kHz with 2x interpolation and APLL, and panel PWM is 8-bit on
  GPIO 23.
- **`HAVE_*` flags** allow bringing peripherals up one at a time; a disabled peripheral
  is never initialised and never has data invented for it.
- **Recording still blocks `loop()`** for its duration. `docs/HARDWARE.md` is emphatic
  that audio belongs on core 0 tasks so a stalled stream cannot delay light control,
  sensors or the button. Not yet restructured — the single largest remaining gap
  between this firmware and the documented hardware design.
- **A sensor that fails or is absent reports `sensor_unavailable`,** never a substituted
  zero. Earlier drafts clamped a failed BH1750 read to 0 and sent it as a real reading.
- **Both BH1750s share one I2C bus**, distinguished by address (0x23 / 0x5C). If your
  breakouts cannot be addressed differently you will need a second bus or an I2C
  multiplexer.
- **Microphone and speaker assume I2S parts** (INMP441-class mic, MAX98357A-class amp).
  Analog alternatives would need the capture and playback sections rewritten.
- **Recording is blocking.** The 4.5-second capture runs inside `loop()`, so telemetry
  pauses for its duration. Acceptable for a push-to-talk button; it would need a task
  split if readings had to stay uninterrupted.
