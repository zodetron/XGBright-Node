# CONTEXT — Smart Adaptive Lighting System with a Vernacular Voice Agent

**Full technical context for group members.** Everything from the raw CSV files to the
GPIO pins, in the order the data actually flows. Written 2026-08-30.

If you read nothing else, read **§1 (what it is)**, **§2 (the map)** and **§16 (status)**.
Everything between those is the detail behind them.

---

## Document map — which file to trust

| File | What it is | Trust it for |
|---|---|---|
| **`CONTEXT.md`** (this file) | Full technical explanation, end to end | Understanding how anything works |
| `HANDOFF.md` | Session handoff: current state, what's left | What to do next |
| `docs/HARDWARE.md` | Measured pin map and hardware quirks | **Authoritative for pins.** Beats the firmware source if they disagree |
| `laptop-server/protocol.py` | The wire schema | **Authoritative for messages.** Read before touching any message |
| `data-pipeline/README.md` | Model selection writeup | The ML numbers |
| `PROGRESS.md` | Original task checklist + deviations | The deviation list and the "why we chose X" record |
| `TESTING.md` | 15 end-to-end manual checks | Verifying a build |

⚠️ **`PROGRESS.md`'s "Firmware" limitations section is stale.** It still says recording
blocks `loop()` and that peripherals are unverified. Both were fixed afterwards — audio
now runs on core-0 tasks and every peripheral has been exercised on the real board.
`HANDOFF.md` supersedes it.

---

## 1. What this system is

An office/room light panel that **dims itself to exactly the brightness needed**, using
how much daylight is already in the room, and that you can **talk to in English, Hindi,
Marathi or Hinglish**.

The problem it replaces: a light panel pinned at 100% all day regardless of the sun. On
our test data that naive incumbent has a mean absolute error of **0.643** against the
ideal brightness. Our model has **0.133**. Mean commanded output across the test set is
**~0.38 instead of 1.0** — that gap is the energy saving.

Three things happen continuously:

1. **Sense** — two BH1750 light sensors report indoor and outdoor lux, once a second.
2. **Decide** — an XGBoost model on the laptop maps (indoor lux, outdoor lux, time) to
   a panel output fraction 0.0–1.0.
3. **Act** — the ESP32 sets PWM duty on the LED panel and acknowledges.

And on demand: **you speak → Whisper transcribes → a rule parser extracts intent → the
state machine applies it → edge-tts speaks a confirmation back out of the panel's own
speaker**, including the live energy-savings figure.

---

## 2. The end-to-end map

```
 ┌──────────────────────── ESP32-WROOM-32 (DevKit v1) ─────────────────────────┐
 │                                                                              │
 │   BH1750 #A 0x23 ──┐                                                         │
 │   BH1750 #B 0x5C ──┴─ I2C (21/22) ─► sensorUpdate()  ── EMA α=0.25           │
 │                                                                              │
 │   INMP441 ── I2S0 (32/33/13) ─► micTask [core 0] ─► 8 KB FIFO                │
 │                                                                              │
 │   MAX98357A ◄ I2S1 (26/25/27) ◄─ audioTask [core 0] ◄─ 32 KB jitter buffer   │
 │                                                                              │
 │   LED panel ◄── PWM GPIO 23, LEDC ch0, 5 kHz, 8-bit ◄─ lightUpdate() ramp    │
 │                                                                              │
 │   Button ──► GPIO 19 (INPUT_PULLUP, active LOW) ─► single/double/long        │
 │                                                                              │
 │                        loop() on core 1 — never blocks                       │
 └────────────────────────────────┬─────────────────────────────────────────────┘
                                  │
                    WiFi 2.4 GHz station mode, ~−63 dBm
                                  │
                   WebSocket CLIENT ──► ws://<laptop>:8000/ws/device
                   JSON text frames = control | binary frames = raw PCM
                                  │
 ┌────────────────────────────────┴───── laptop (macOS) : one Python process ───┐
 │                                                                              │
 │  main.py ──── FastAPI + uvicorn, port 8000                                   │
 │    │                                                                         │
 │    ├─ DeviceSession ── frames in/out, ack futures, audio assembly            │
 │    │                                                                         │
 │    └─ Controller ─── the decision layer                                      │
 │         ├─ predictor.py  ─► XGBoost 8.5 MB, 10 features, 0.46 ms/predict     │
 │         ├─ state.py      ─► AUTO / MANUAL machine + 30-min revert timer      │
 │         ├─ rolling.py    ─► 300 s window + cumulative-joules energy model    │
 │         ├─ voice/stt.py  ─► MLX Whisper large-v3-turbo (Apple Neural Engine) │
 │         ├─ voice/nlu.py  ─► rule parser, 4 languages, no LLM                 │
 │         └─ voice/tts.py  ─► edge-tts → MP3 → PCM16 → back down the socket    │
 │                                                                              │
 │  ws://localhost:8000/ws/dashboard  +  REST /api/*                            │
 └────────────────────────────────┬─────────────────────────────────────────────┘
                                  │
                    React + Vite dashboard on :5180
        live chart · status · manual override · modes CRUD · settings · push-to-talk
```

**Offline, run once, not part of the live system:**

```
 raw/*.zip ──► combine.py ──► out/unified.parquet ──► features.py ──► out/dataset.parquet
                                                                            │
                                                        train.py ◄──────────┘
                                                            │
                                    laptop-server/model/brightness_model.joblib
```

---

# PART I — THE MACHINE LEARNING

## 3. The raw data

### 3.1 What we actually have

Minute-resolution **submetering from a Singapore office tower**, 2018 and 2019, sitting
in `raw/` as zip archives. Per floor, per zone, it records:

- lighting power (kW)
- plug-load power
- air-conditioning power
- and on **floors 2–7 only**, an environment sensor: temperature, humidity, and an
  illuminance channel named `zN_S1(lux)`

Site coordinates used downstream: **lat 1.2966, lon 103.7764, UTC+8, no DST**
(`data-pipeline/config.py`).

### 3.2 What we threw away, and why

| Dropped | Reason |
|---|---|
| `archive (3).zip` | **Byte-identical duplicate** of the already-extracted `archive (2)/`. `combine.py` de-duplicates on filename so each floor-year counts once. |
| **Floor 1** | No environment sensors fitted at all — no illuminance channel to learn from. |
| Data before Mar 2019 / Jul 2018 | The sensors only came online then. Earlier coverage is empty rows. |
| **ASHRAE db-II** | This is the one worth understanding. It is a **per-subject thermal-comfort questionnaire** — one row per human response, different buildings, different continents. It has **no illuminance column, no lighting power, and no join key** with the submetering data. Merging it produces a table that is mostly nulls and teaches the model nothing about light. Excluded deliberately, logged as a deviation in `PROGRESS.md`. |

### 3.3 The two facts that shape everything downstream

**(a) `zN_S1(lux)` is not lux.** It is an integer-valued index that tops out around 90.
A real office task plane sits at 300–500 lux. Its *shape* is correct — it rises at 06:00,
plateaus through the working day, collapses after 19:00 — so we treat it as a **linear
relative illuminance index** and calibrate it onto a physical scale:

```python
# data-pipeline/config.py
INDOOR_LUX_INDEX_FULL = 90.0    # index value that means...
INDOOR_LUX_FULL_SCALE = 700.0   # ...this many real lux
```

```python
indoor_lux = (lux_index / 90.0) * 700.0
```

**Every absolute lux figure in this project inherits that calibration assumption.** If
the physical system dims too aggressively once running, these two constants are the
first thing to re-check against real BH1750 readings.

**(b) Outdoor lux was never measured.** Nothing in `raw/` contains outdoor illuminance.
We did not invent it — we reconstructed it from real measurements through real solar
geometry. That is §4.4.

## 4. `features.py` — how the training table is built

This is the intellectual core of the project. Read it in order.

### 4.1 Cleaning

```python
df = df.dropna(subset=["lux_index", "light_kw"])   # both signals required
df = df[(df.light_kw >= 0) & (df.lux_index >= 0)]  # negatives are meter error
```

### 4.2 Zone-relative panel output

Zones differ in size, so raw kW is not comparable across them. Each zone's lighting power
becomes a **0–1 output fraction against its own full-power reference**:

```python
full_power = df.groupby("zone_id")["light_kw"].transform(lambda s: s.quantile(0.99))
df = df[full_power > 0.05]                                  # drop never-lit zones
df["brightness_frac"] = (df.light_kw / full_power).clip(0, 1)
```

The reference is the **99th percentile, not the max** (`ZONE_POWER_FULL_QUANTILE = 0.99`)
because zone lighting power has rare spikes — compressor inrush, submeter glitches — far
above the real steady-state maximum. Using `max()` would make every normal reading look
dim.

### 4.3 Solar geometry

`solar_elevation()` implements the **NOAA solar-position algorithm** for the site, giving
sun elevation for every timestamp. `clear_sky_illuminance()` turns elevation into the
outdoor illuminance a clear sky would produce — **peaking near 115 klux** under
Singapore's near-overhead sun.

### 4.4 Reconstructing outdoor lux — the five steps

The chain that turns a measured indoor index into a physically defensible outdoor lux:

**Step 1 — Measure each zone's panel output. Do not assume it.**

At night there is no daylight term, so whatever the sensor reads *is* the luminaires
alone. Divide by the output fraction at that moment and you recover that zone's
full-output illuminance directly:

```python
is_night = df.clear_sky_lux <= 1.0
night    = df[is_night & (df.brightness_frac > 0.15)]     # only well-lit night samples
panel_lux = (night.indoor_lux / night.brightness_frac).groupby(night.zone_id).median()
```

Guards: at least **200 night samples** per zone (`PANEL_LUX_MIN_NIGHT_SAMPLES`) and a
median above **30 lux** (`PANEL_LUX_MIN`) — below that the zone's sensor is uninformative.
**Result: median zone panel capability ≈ 280 lux at the sensor; spread 30 → 1290 lux.**

**Step 2 — Separate daylight from artificial light.**

```python
daylight_indoor_lux = clip(indoor_lux - brightness_frac * panel_lux[zone], min=0)
```

**Step 3 — Estimate each zone's daylight factor.**

On the *clearest* daytime moments, the observed daylight/clear-sky ratio approaches the
zone's true DF. So take a high quantile of that ratio:

```python
daytime = df.clear_sky_lux > 5000.0
daylight_factor = (daylight_indoor_lux / clear_sky_lux).where(daytime).groupby(zone).quantile(0.95)
```

**Result: DFs run 0.0003 → 0.025.** That is a realistic physical range, from deep-plan
interior to well-glazed perimeter — a sanity check that the method produces sense.

**Step 4 — Derive sky clearness from the shortfall.**

```python
potential = daylight_factor * clear_sky_lux          # what this zone COULD see today
clearness = clip(daylight_indoor_lux / potential, 0, 1)   # how much it actually saw
```

**Step 5 — Outdoor lux.**

```python
outdoor_lux = clear_sky_lux * sky_clearness
```

So outdoor lux is **derived from real measurements through real physics**, not simulated.
This was approved before implementation and is logged as a deviation.

### 4.5 Occupancy and setpoint — the design choice

Everything above is derivation from data. **This part is a policy decision and you should
say so if anyone asks.**

```python
occupied = brightness_frac > 0.10          # OCCUPANCY_POWER_FRACTION
setpoint = 300.0 if occupied else 50.0     # lux
```

- Occupancy comes from **real metered lighting power**, so the occupied/unoccupied rhythm
  in the training set is the building's genuine one, not simulated.
- **Why 300 lux, not the EN 12464-1 figure of 500?** EN 12464-1's 500 lux is on the *task
  plane*. 300 lux is the surrounding-area figure, and — critically — it is what these
  zones' *ceiling* sensors actually see at full output (median panel capability ≈ 280 lux
  at the sensor). Setting 500 would saturate the target at 1.0 for most rows and turn the
  regression into a disguised binary flag.
- 50 lux unoccupied = enough to move around safely, no task lighting.

### 4.6 The target

```python
target_brightness = clip((setpoint - daylight_indoor_lux) / panel_lux[zone], 0.0, 1.0)
```

In words: **the panel output that would bring the sensor plane to its setpoint, given the
daylight already present.**

Health check on the resulting distribution — **mean 0.38, only 3% at the floor (0.0) and
10% at the ceiling (1.0)**. It is a genuinely continuous regression target, not a
disguised occupancy classifier.

### 4.7 Resampling

Raw is 1-minute across 14 files × up to 5 zones ≈ **36M zone-minutes**. Resampled to
**5 minutes** (`RESAMPLE = "5min"`), then subsampled to `MAX_TRAIN_ROWS = 600_000`.
`RANDOM_SEED = 42`.

## 5. The features

**The design rule: every feature must be computable at inference time from what the
deployed system actually has — two BH1750 readings and the system clock.** Nothing in
training is unavailable in the room. That is why the building's power meters, which are
all over the training data, appear in *no* feature.

| # | Feature | Definition | Why |
|---|---|---|---|
| 1 | `indoor_lux` | measured | the primary signal |
| 2 | `outdoor_lux` | reconstructed (§4.4) | how much daylight is available |
| 3 | `log_indoor_lux` | `log1p(indoor_lux)` | illuminance spans 4 orders of magnitude; the log keeps the dim end resolvable |
| 4 | `log_outdoor_lux` | `log1p(outdoor_lux)` | same |
| 5 | `lux_ratio` | `indoor / (outdoor + 1)` | how well the room is *capturing* the light outside — effectively a live daylight-factor proxy |
| 6 | `tod_minutes` | `hour*60 + minute` | raw time of day |
| 7 | `tod_sin` | `sin(2π·min/1440)` | cyclic encoding… |
| 8 | `tod_cos` | `cos(2π·min/1440)` | …so 23:59 and 00:00 are adjacent, not maximally distant |
| 9 | `day_of_week` | 0–6 | weekday occupancy rhythm |
| 10 | `is_weekend` | 0/1 | explicit weekend flag |

**Target:** `target_brightness`, continuous in [0, 1].

The exact list and its **order** live in `laptop-server/model/model_metadata.json` and the
server reads it from there rather than duplicating it — so the pipeline and the server can
never silently disagree about column order.

## 6. Training and evaluation

### 6.1 The split — chronological, not random

**This is the single most important methodological decision in the ML half.**

Consecutive 5-minute samples from the same zone are near-duplicates. A random split
would put a row's near-twin in training and report a beautiful, meaningless score.

```
Train : 2018-07-01 → 2019-10-11   480,000 rows
Test  : 2019-10-11 → 2019-12-31   120,000 rows   (last 20%, held out)
```

### 6.2 The candidates and their hyperparameters

From `data-pipeline/train.py`:

| Model | Configuration |
|---|---|
| Ridge regression | linear baseline on standardised features |
| Random Forest | `n_estimators=120, max_depth=18, min_samples_leaf=5` |
| **XGBoost** | `n_estimators=600, learning_rate=0.05, max_depth=8` |
| LightGBM | `n_estimators=600, learning_rate=0.05, num_leaves=63` |
| Neural net | `MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=60)` |
| Mean baseline | predicts the training mean — the floor for "learned anything" |
| **Always 100%** | the incumbent system, scored on the same test set |

Including the incumbent as a scored row is what makes the headline claim honest: it is
measured on identical data, not asserted.

### 6.3 Results

Verbatim from `data-pipeline/out/model_comparison.csv`:

| Model | MAE | RMSE | R² | Fit time |
|---|---|---|---|---|
| Random Forest | 0.1304 | 0.2059 | 0.6085 | 13.6 s |
| **XGBoost — selected** | **0.1328** | **0.2061** | **0.6079** | **3.0 s** |
| LightGBM | 0.1339 | 0.2064 | 0.6068 | 5.6 s |
| Neural net (MLP 64-32) | 0.1426 | 0.2134 | 0.5797 | 20.6 s |
| Ridge regression | 0.1958 | 0.2620 | 0.3664 | 0.0 s |
| Mean baseline | 0.2866 | 0.3303 | −0.0071 | — |
| *Always 100% (incumbent)* | *0.6432* | *0.7225* | *−3.8197* | — |

MAE is in target units, so **0.1328 means commanded brightness is off by ~13 percentage
points on average**.

### 6.4 Why XGBoost won when Random Forest scored better

Random Forest had the lowest raw MAE — by **1.8% relative**, which is about **one third of
one percentage point of actual brightness**. Invisible in a room. It serialises to
**181.7 MB**. XGBoost reaches statistically indistinguishable accuracy in **8.5 MB**.

So `train.py` selects with an explicit efficiency tie-break:

```python
MAE_TIE_TOLERANCE = 0.02
tied = ranked[ranked.mae <= leader_mae * 1.02]      # within 2% relative MAE = tied
best = tied.sort_values("size_mb").iloc[0]          # decide on serialised size
```

The justification: this artifact is loaded into a live server process **that also holds a
~1.5 GB Whisper model in memory**. 173 MB of extra resident set is a real cost and the
accuracy it buys is not.

**Measured inference: 0.46 ms per single-row prediction** — enormous headroom against the
1-second control loop.

### 6.5 Reading the headline number honestly

Against the system this replaces, MAE falls from **0.643 → 0.133**, and mean commanded
output across the test set is **~0.38 rather than 1.0**.

Treat that as an **upper bound on savings, not a promise.** It rests on two stated
assumptions: the setpoint policy in §4.5 being the right definition of "ideal", and the
illuminance calibration in §3.3.

---

# PART II — THE HARDWARE

## 7. Bill of materials

| Part | Role |
|---|---|
| ESP32-WROOM-32, DevKit v1 | the whole device |
| 2 × BH1750 | ambient light sensors, I2C, addressable |
| INMP441 | I2S MEMS microphone, 24-bit |
| MAX98357A | I2S class-D amplifier + speaker |
| LED panel + MOSFET driver | the light, PWM-dimmed |
| Momentary pushbutton | local push-to-talk |

Board facts: **no PSRAM**, ~320 KB DRAM, **135–165 KB free heap at runtime**. Arduino-ESP32
core **3.3.11**. Serial `/dev/cu.usbserial-0001` @ **115200**. Core 0 = audio tasks,
core 1 = `loop()`.

## 8. The exact pin map

Every value below was **measured on the physical board**, not copied from a datasheet.
`docs/HARDWARE.md` is the authority; where the firmware source disagrees with it, that
file is right.

| Peripheral | Signal | GPIO | Notes |
|---|---|---|---|
| **LED / light panel** | PWM | **23** | LEDC channel 0, 5 kHz, 8-bit → duty 0–255 |
| **Button** | input | **19** | `INPUT_PULLUP`, wired to GND, **active LOW** |
| **BH1750 #A (indoor)** | SDA / SCL | **21 / 22** | ADDR → GND ⇒ address **`0x23`** |
| **BH1750 #B (outdoor)** | SDA / SCL | **21 / 22** | ADDR → 3V3 ⇒ address **`0x5C`** |
| **INMP441 mic** | SCK / WS / SD | **32 / 33 / 13** | `I2S_NUM_0` |
| **MAX98357A amp** | BCLK / LRC / DIN | **26 / 25 / 27** | `I2S_NUM_1` |

**Both BH1750s share the one I2C bus at 100 kHz**, distinguished only by address. If your
breakouts cannot be addressed differently you need a second bus or an I2C multiplexer.

**GPIO budget:**
- Free: `2, 4, 5, 12, 14, 15, 16, 17, 18, 34, 35, 36, 39`
- **Never use `6–11`** — SPI flash, prevents boot
- `34–39` are **input only**
- `0, 2, 12, 15` are **strapping pins** — pulling them wrong changes boot mode

**Not a GPIO:** the always-on red LED is the **power indicator**, hardwired to 3V3. No pin
controls it. The controllable LED is on 23. (This confused us once.)

## 9. The three things that will break this board

Learned the hard way, each costing real debugging time. All three are documented in the
firmware header too.

### 9.1 No `analogRead()` anywhere

The legacy I2S driver registers ESP-IDF's **old** ADC driver; `analogRead()` registers the
**new** one. Mixing them aborts during init on core 3.x with:

```
ADC: CONFLICT! driver_ng is not allowed to be used with the legacy driver
```

The board boot-loops **before `setup()` prints a single character**, so it looks like a
dead board. **The light sensors are I2C precisely to avoid this.** Use I2C or a digital pin.

**And: a clean compile proves nothing here.** The original firmware included
`driver/i2s.h`, compiled perfectly, and only flashing caught it. Now on
`driver/i2s_std.h`.

### 9.2 MCLK must be explicitly unset on both I2S peripherals

A zero-initialised `i2s_pin_config_t` leaves `mck_io_num` at 0, so **both** peripherals try
to route their master clock onto GPIO 0, and whichever installs second fails with
`i2s_set_pin: mclk config failed`. Symptom: a completely silent speaker with no error
anywhere obvious. Cost a session.

### 9.3 `loop()` must never block

Audio lives on **core 0 tasks** so a long tone, a stalled stream or a dead laptop cannot
delay light control, sensors or the button.

**Useful diagnostic:** a board that answers `ping` but refuses a WebSocket connection has a
stuck `loop()`. ICMP is handled by the WiFi stack's own task, so that asymmetry is
meaningful.

## 10. Audio hardware facts (all measured)

### 10.1 Microphone — INMP441

- **32-bit I2S slots → int16 via `>> 15`.** Strictly correct is `>> 16`; shifting one less
  adds gain. Measured peak on this board: **15287 of 32767** — good headroom. Raise the
  shift if it clips, lower it if too quiet.
- **A DC-blocking high-pass is mandatory.** The INMP441 **idles near −22000, not 0**.
  Left in, that offset dominates RMS and pegs the level meter at 100% in a silent room.
  One-pole HPF, `R = 0.995` ⇒ ~13 Hz corner at 16 kHz.
  **Measured: idle RMS fell from ~20150 to ~1350.**
- **The L/R pin appears to float.** Which I2S slot carries data has been observed
  **changing between boots**, so the firmware auto-detects it (§13.4). *Tying L/R to GND
  would make this deterministic and is on the to-do list.*
- Measured idle RMS in a quiet room: **~900–1400**.

### 10.2 Speaker — MAX98357A

- **Wire format is 16 kHz / 16-bit / mono, but the DAC runs at 32 kHz** with 2× linear
  interpolation. At 16 kHz a 5 kHz tone gets only 3.2 samples per cycle and the staircase
  is **audible as buzz**.
- **APLL enabled** for a jitter-free clock.
- **The `SD` pin must float or be high.** Tied to GND the amp is in shutdown and silent no
  matter what the firmware does. Floating = mono, (L+R)/2.
- Playback volume 0.90.

### 10.3 Why recording must stream, with the arithmetic

```
16 kHz × 16-bit × mono = 32 KB/s
5-second clip           = 160 KB
Free heap               = ~135–165 KB
```

**A 5-second clip does not fit in RAM.** The device therefore never holds a whole clip:
at most an 8 KB FIFO, with the laptop assembling the file. This is not an optimisation,
it is a hard constraint.

## 11. Sensors and radio

- **BH1750 continuous high-res conversion takes ~120 ms.** Polling faster just re-reads the
  same value. We sample at 130 ms.
- **Lux → percent must be logarithmic** (1 lx = 0%, 1000 lx = 100%). Lux spans four orders
  of magnitude; a linear map pins every indoor reading near zero.
- Measured indoor readings: sensor A/B **8.3 lx / 0.8 lx**, up to **25 / 455 lx**.
- **WiFi is 2.4 GHz only** — the ESP32 cannot see 5 GHz SSIDs at all.
- **Link quality is the limiting factor for streaming audio:**

| Link | Throughput | Verdict |
|---|---|---|
| −62 dBm (strong) | 40 KB/s | fine (31.2 KB/s needed) |
| −79 dBm (weak) | 17.5 KB/s | **stutters** |

Our board sits at **~−63 dBm**. Light, sensors and recording are unaffected by link
quality; only sustained playback needs the bandwidth. **If the confirmation crackles, that
is why — not the audio code.**

---

# PART III — THE FIRMWARE

`esp32-firmware/adaptive_lighting.ino`, single file, **1147 lines**.

Libraries (Library Manager): `WebSockets` by Markus Sattler ≥ 2.4.1, `ArduinoJson` by
Benoit Blanchon ≥ 7.0.0, `BH1750` by Christopher Laws.

## 12. Design philosophy — a pure I/O peripheral

> *"This device is a pure I/O peripheral. It does no inference and makes no lighting
> decisions of its own: it reads its two light sensors, streams them to the laptop,
> records audio on request, and does what it is told."*

Whisper, the brightness model, the NLU and TTS **all live on the laptop**. The ESP32 has
no idea what a mode is, what AUTO means, or what it just said.

**Only Section 1 of the file should ever need editing.** It is the configuration block.

## 13. Structure and subsystems

### 13.1 Section 1 — configuration

| Group | Key values |
|---|---|
| 1.1 Network | `WIFI_SSID`, `WIFI_PASSWORD`, `SERVER_HOST`, `SERVER_PORT 8000`, `SERVER_PATH "/ws/device"`, `DEVICE_ID "esp32-room-1"`, `FIRMWARE_VERSION "2.0.0"` |
| 1.2 Pins | the table in §8 |
| 1.3 Panel | `LIGHT_PWM_FREQ_HZ 5000`, `LIGHT_PWM_RESOLUTION 8`, `LIGHT_INVERT_OUTPUT 0`, `LIGHT_RAMP_STEP 3`, `LIGHT_RAMP_INTERVAL_MS 8` (⇒ full sweep ~680 ms) |
| 1.4 Sensors | `SENSOR_SAMPLE_INTERVAL_MS 130`, `SENSOR_EMA_ALPHA 0.25`, `TELEMETRY_INTERVAL_MS 1000` |
| 1.5 Mic | `MIC_SAMPLE_RATE 16000`, `MIC_SAMPLE_SHIFT 15`, `MIC_DC_BLOCK_R 0.995`, `MIC_CHANNEL_LEFT 0` |
| 1.6 Recording | `RECORDING_DURATION_MS 5000`, `RECORDING_MAX_MS 15000`, `MIC_STREAM_BUFFER_BYTES 8192`, `MIC_WS_CHUNK_BYTES 1024` |
| 1.7 Speaker | `SPK_STREAM_RATE 16000`, `SPK_I2S_RATE 32000`, `PLAYBACK_BUFFER_BYTES 32768` (~1.0 s), `PLAYBACK_PREROLL_BYTES 10240` (~320 ms), `PLAYBACK_REFILL_BYTES 6144` (~190 ms) |
| 1.8 Button | `BUTTON_DEBOUNCE_MS 35`, `BUTTON_DOUBLECLICK_MS 350`, `BUTTON_LONGPRESS_MS 900` |
| 1.9 System | `SERIAL_BAUD 115200`, `JSON_RX_MAX_BYTES 2048` |

⚠️ **`WIFI_SSID`, `WIFI_PASSWORD` and `SERVER_HOST` are compile-time constants.** Changing
network *or* the laptop getting a new DHCP lease **requires a reflash**. The credentials
are in the source — keep that in mind before pushing the firmware anywhere public.

### 13.2 Light panel

Never jumps. `setLightBrightness(duty)` sets a **target**; `lightUpdate()` ramps the actual
duty toward it at 3 units per 8 ms.

```c
static int g_lightTarget;   // where we want to be
static int g_lightActual;   // what the pin outputs right now
```

**`LIGHT_INVERT_OUTPUT`** exists because MOSFET driver boards are commonly active-LOW. If
the serial log shows duty changing and the panel stays dark, **flip this to 1 before
suspecting anything else.**

Note also: `ledcSetup()` / `ledcAttachPin()` **do not exist on core 3.x**. They were
replaced by `ledcAttach(pin, freq, res)` and `ledcWrite(pin, duty)`. There is a version
shim in the firmware so it builds on both.

### 13.3 Sensors

Each BH1750 is smoothed with an EMA at α = 0.25. A failed or absent sensor returns
**`-1.0f`**, never 0:

```c
float sensorLuxA() { return g_liveA ? g_luxA : -1.0f; }
bool  sensorIsHealthy() { return g_liveA && g_liveB; }
```

`-1.0f` is what triggers `sensor_unavailable` on the wire (§16.2 / §21). **A clamped zero
would appear on the dashboard as a measurement nothing ever took.**

### 13.4 Microphone — the auto-detect

`micTaskFn` runs **pinned to core 0** at priority 3, capturing continuously into an
8 KB FreeRTOS `StreamBuffer`.

At boot, `micAutoDetectSlot()` installs on the configured slot, measures peak-to-peak
spread, and if the signal is flat **uninstalls and reinstalls on the other slot**. The
config default in the file is `MIC_CHANNEL_LEFT 0` (RIGHT) — but **on this board it picks
LEFT every single boot**, the opposite of the default. Without this auto-detect, every
recording would have been silent and we would have blamed Whisper.

Pipeline per block: 32-bit raw → `>> 15` → one-pole DC-blocking HPF → int16 → FIFO.

### 13.5 Speaker — the jitter buffer

`audioTaskFn`, also on **core 0**, priority 2.

Playback does not start the instant bytes arrive. It **banks a 320 ms preroll first**,
because draining immediately means every network hiccup empties the buffer and the DMA
clocks out zeros. On underrun it goes **quiet and refills 190 ms** rather than
machine-gunning fragments. *One short pause beats continuous distortion.*

Upsampling 16 kHz → 32 kHz is 2× linear interpolation, done on the fly.

### 13.6 Button

A small state machine with 35 ms debounce, producing `BTN_SINGLE_CLICK`,
`BTN_DOUBLE_CLICK`, `BTN_LONG_PRESS` (900 ms).

### 13.7 The recorder

`startCapture()` → sends `audio_begin` → starts the mic stream → `recorderUpdate()` drains
the FIFO to the socket in 1 KB chunks every `loop()` iteration → after the duration,
`finishCapture()` sends `audio_end`.

**Time-bounded in `loop()`, not a blocking wait** — telemetry and light control keep
running throughout the capture.

## 14. Boot sequence

Exactly what you should see on the serial monitor, in order:

```
1. Banner: firmware version, core version, free heap
2. lightInit()    — LEDC attach
3. sensorInit()   — I2C scan:  [i2c] scanning: 0x23 0x5C [2 device(s)]
4. buttonInit()
5. micInit()      — I2S0 install + slot auto-detect
6. spkInit()      — I2S1 install (MCLK explicitly unset)
7. micLaunchTask() / spkLaunchTask()   → core 0
8. WiFi.mode(WIFI_STA)
9. wifiScanAndReport()   — lists what IS in range, says if the target was found
10. wifiBegin()          — join
11. g_ws.begin(...)      — WebSocket client dials the laptop
12. runBrightnessSweep() — 0 → 255 → 0, visible proof PWM reaches the pin
13. "[setup] ready | free heap N bytes"
```

**Every init failure is survivable.** A failed mic prints
`[mic] I2S init FAILED — voice disabled, rest of system OK` and the light still works.

**Why the WiFi scan exists:** hours were lost to "it won't connect" that turned out to be
the network simply not being there — the SSID was misspelled. The scan **separates "wrong
password" from "that network is not here"** before we start retrying blindly.

⚠️ **The scan must run from an idle radio.** Scanning during an in-flight association
returns zero networks and looks exactly like a dead antenna.

## 15. `loop()` — the priority order

```c
void loop() {
  // Priority 1: the light. First, every iteration, always.
  lightUpdate();
  sensorUpdate();
  ButtonEvent e = buttonUpdate();
  if (e != BTN_NONE) handleButtonEvent(e);

  // Priority 2: network + audio streaming
  netUpdate();
  recorderUpdate();

  if (g_sweepRequested) { g_sweepRequested = false; runBrightnessSweep(); }
  if (isRecording() && millis() - g_recStart >= g_recDuration) finishCapture();
}
```

**WiFi-drop fallback:** `g_lightTarget` is never cleared on disconnect, and the LEDC
peripheral holds its duty **in hardware**. So if the laptop dies, **the room stays exactly
as the user left it** — it does not snap to full or to dark.

**The one deliberate blocking call:** `runBrightnessSweep()` blocks for ~2.4 s. It is a
human-triggered diagnostic, and a sweep sharing the loop with everything else is not
visually unambiguous — which is the entire point of it. It is also **deferred out of the
socket callback** into `loop()` via `g_sweepRequested`, because running it inside the
callback would stall the connection for its whole duration.

---

# PART IV — THE TRANSPORT

## 16. The WebSocket

### 16.1 Direction — the thing that confuses everyone

**The ESP32 is a WebSocket _client_. It dials out to the laptop at
`ws://<laptop-ip>:8000/ws/device`.**

Consequences, all of which caused confusion at least once:

- The board **never creates an access point** → it will **never appear in a WiFi list**.
- There is **no page to browse at the ESP32's IP**.
- If the laptop server is down, the board just retries; light and sensors keep working.

⚠️ **This is the opposite of the `CheckForESP_Firmware` project**, where the ESP32 *hosts*
a server on port 81 and a browser connects directly to it. `docs/HARDWARE.md` was written
against that firmware — **its hardware facts all still apply, its transport does not.**

Why this direction: the laptop is where Whisper, the model and TTS run, and the ESP32 is
specified as a pure I/O device.

Connection management: `setReconnectInterval(3000)` and
`enableHeartbeat(15000, 3000, 2)` — the heartbeat is what detects **half-open TCP
connections**, where the socket looks alive but nothing is getting through.

### 16.2 The complete wire schema

Authoritative source: `laptop-server/protocol.py`. **Read it before touching any message.**

**ESP32 → laptop**

```jsonc
{"type": "hello",  "device_id": "esp32-room-1", "fw": "2.0.0"}
{"type": "sensor", "indoor_lux": 8.3, "outdoor_lux": 455.0}
{"type": "sensor_unavailable", "reason": "indoor BH1750 (0x23) not responding"}
{"type": "ack",    "command_id": "a3f9...", "brightness": 0.62}   // command APPLIED
{"type": "audio_begin", "sample_rate": 16000, "format": "pcm_s16le"}
<binary frames>                                                   // raw PCM chunks
{"type": "audio_end"}
```

**Laptop → ESP32**

```jsonc
{"type": "brightness", "command_id": "a3f9...", "value": 0.62}    // 0.0 - 1.0
{"type": "record", "duration_ms": 5000}
{"type": "sweep"}                                                 // panel self-test
{"type": "audio_begin", "sample_rate": 16000, "format": "pcm_s16le", "bytes": 124800}
<binary frames>
{"type": "audio_end"}
{"type": "status", "message": "..."}
```

Constants:

```python
AUDIO_SAMPLE_RATE       = 16_000
AUDIO_FORMAT            = "pcm_s16le"
AUDIO_CHUNK_BYTES       = 2048    # outbound chunk size
ACK_TIMEOUT_SECONDS     = 2.0
DEVICE_TIMEOUT_SECONDS  = 15.0    # silence longer than this = disconnected
RECORD_DURATION_MS      = 5000
```

The type names are Python constants (`protocol.BRIGHTNESS`, etc.) **so a typo is an
`ImportError` rather than a silent no-op.**

### 16.3 Why audio is binary frames, not base64 in JSON

Five seconds of 16 kHz 16-bit mono is 160 KB. **Base64 inflates that to 213 KB** *and*
forces the ESP32 to hold the whole thing in RAM to build one giant string — which, per
§10.3, it cannot do. Binary frames stream in fixed-size chunks the device fills from a
small FIFO.

### 16.4 The `ack` is load-bearing

```c
// firmware: acknowledge only AFTER the panel has been commanded
setLightBrightness(duty);
...
ack["type"] = "ack"; ack["command_id"] = doc["command_id"]; sendJson(ack);
```

```python
# server: gate the spoken confirmation on it
ack = await self.send_brightness(target, await_ack=True)
applied = ack is not None
```

**The user is never told the light changed before it actually did.** If the ack does not
arrive within 2.0 s the server logs it and speaks anyway, marking `applied: false`.

### 16.5 One device at a time

```python
previous = controller.device
if previous is not None:
    await previous.socket.close(code=1000, reason="replaced by a new device")
```

This exists because a bug: connecting a test client **silently overwrote
`controller.device`**, leaving the original board streaming telemetry while the server
believed nothing was attached and could no longer command it. Now the old socket is closed
properly so state always matches reality.

---

# PART V — THE LAPTOP SERVER

One Python process. `./.venv/bin/python laptop-server/main.py`, FastAPI + uvicorn on
**port 8000**.

## 17. File responsibilities

| File | Responsibility |
|---|---|
| `main.py` | Transport only — WebSockets, REST, lifecycle, `DeviceSession` |
| `controller.py` | **The decision layer.** Everything between "a reading arrived" and "a command goes out", and between "a transcript arrived" and "a confirmation is spoken" |
| `state.py` | The AUTO/MANUAL machine and the revert timer |
| `predictor.py` | Loads the model, builds the feature vector, predicts |
| `rolling.py` | 300 s telemetry window + the energy/savings model |
| `history.py` | SQLite log of real-device sessions — 5 s buckets + light changes (§24.1) |
| `config_store.py` | Loads and live-updates the three config JSONs |
| `protocol.py` | The wire schema |
| `voice/stt.py` | MLX Whisper |
| `voice/nlu.py` | Rule-based intent parser |
| `voice/tts.py` | edge-tts + confirmation templates + fallback |
| `tools/mock_device.py` | A fake ESP32 speaking the real protocol |

The split between `main.py` and `controller.py` is deliberate: **the control logic can be
exercised with no WebSocket and no device attached.**

## 18. The 1 Hz control loop, step by step

Every second, `{"type":"sensor"}` arrives and `Controller.on_sensor()` runs:

```python
async def on_sensor(self, indoor_lux, outdoor_lux):
    self.sensor_error = None
    self.last_indoor_lux, self.last_outdoor_lux = float(indoor_lux), float(outdoor_lux)
    self.last_sensor_at = time.time()

    # 1. Predict — ALWAYS, even while MANUAL holds
    predicted = self.predictor.predict(indoor_lux, outdoor_lux, datetime.now())

    # 2. Resolve against the state machine (this is also what expires MANUAL)
    brightness = self.state.resolve_brightness(predicted)
    self.last_brightness = brightness

    # 3. Record for the chart and the energy totals
    snapshot = self.state.snapshot()
    self.window.add(indoor_lux, outdoor_lux, brightness, snapshot["mode"], snapshot["active_mode_name"])

    # 4. Command the device
    await self.send_brightness(brightness)

    # 5. Tell every dashboard
    await self.broadcast_state(predicted=predicted)
```

**Why predict even during MANUAL:** so the dashboard can show what AUTO *would* be doing,
and so the value is fresh the instant the MANUAL timer expires.

## 19. `predictor.py`

```python
self._features = self._meta["features"]   # read from the artifact, never duplicated
```

Feature construction **mirrors `data-pipeline/features.py` exactly** (§5). Then:

```python
frame = pd.DataFrame([[row[f] for f in self._features]], columns=self._features)
value = float(self._model.predict(frame)[0])
return max(0.0, min(1.0, value))          # server MUST clip
```

A **DataFrame with named columns, not a bare array** — the model was fitted with feature
names and warns otherwise.

**Graceful degradation:** if the model cannot load, it logs loudly and falls back to a
heuristic, `(300 - indoor_lux) / 500`, rather than exiting. MANUAL mode and the entire
voice pipeline still work without the model.

## 20. `state.py` — AUTO / MANUAL

```
AUTO    — the default. Brightness = whatever the model predicts.
MANUAL  — entered by ANY voice command or dashboard override that sets a specific level.
```

Rules:

- **Timer:** `manual_timeout_seconds` (default **1800 s = 30 min**) reverts MANUAL → AUTO.
- **Every new command resets the timer**, so a user who keeps adjusting is never yanked
  back to AUTO mid-conversation.
- **Saying "auto mode" cancels MANUAL immediately**, bypassing the timer entirely. It is an
  escape hatch that must always work.
- **Expiry is lazy** — checked inside `resolve_brightness()`, which runs once per telemetry
  sample. No background timer thread.

The relative-adjust baseline is subtle and worth knowing:

```python
baseline = self._target_brightness if self._mode == MANUAL else float(current_brightness)
```

In AUTO, "a bit brighter" nudges from **what the model last commanded** — what the user can
actually see — not from a stale MANUAL target they never set.

## 21. Lux ↔ brightness

Modes are defined in **target lux**, but the panel takes an output **fraction**. The
conversion mirrors the target definition the model was trained on:

```python
def brightness_for_target_lux(self, target_lux):
    panel_full = self.config.get("panel_full_output_lux", 500)
    ambient = max(0.0, self.last_indoor_lux - self.last_brightness * panel_full)
    return clip((target_lux - ambient) / panel_full, 0, 1)
```

Subtract the panel's own current contribution from the measured reading to get the
**ambient** component, then fill the remaining gap. Same shape as §4.6.

## 22. `rolling.py` — the energy model

**The power model:** the panel at X% brightness draws X% of its rated wattage
(`panel_rated_watts`, default **18.0 W**). Crude, but close enough for a PWM-dimmed LED
panel.

Two cumulative totals are integrated on every sample and **never expire**, even as the
300 s display window slides:

```python
if 0 < dt <= 10.0:                          # guard against clock jumps / WiFi stalls
    self._actual_joules += rated_watts * brightness * dt
    self._full_joules   += rated_watts * dt
savings = 1.0 - (actual_joules / full_joules)
```

The savings figure spoken in voice confirmations comes from these, so it reflects the
**whole session**, not just the visible graph.

The `dt <= 10.0` guard matters: without it, a WiFi dropout injects a huge fictional block
of energy into the total. **Consequence:** long outages under-report actual *and*
hypothetical usage equally, so the savings **percentage** stays correct.

Nothing here is persisted. Restarting the server starts the graph over — intended
behaviour. The long-term record is the separate SQLite history (§24.1).

## 23. The API surface

**WebSockets**

| Path | Who | What |
|---|---|---|
| `/ws/device` | the ESP32 | the protocol in §16.2 |
| `/ws/dashboard` | browser tabs | receives full state on connect, then a live event stream |

Each dashboard gets **its own `asyncio.Queue` (maxsize 32)** so a slow tab cannot block the
others or the device loop. Overflowing queues **drop** rather than stall — a dashboard that
far behind resyncs from the next full state message.

**REST**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | full status snapshot |
| GET | `/api/series` | the rolling window for the chart |
| GET/POST | `/api/modes` | list / create a mode |
| PUT/DELETE | `/api/modes/{id}` | edit / delete |
| GET/PUT | `/api/settings` | magnitude steps, timeout, wattage |
| POST | `/api/override` | manual control: `{auto}` \| `{mode_id}` \| `{brightness}` |
| POST | `/api/light/sweep` | **panel self-test — the LED verification** |
| POST | `/api/voice/record` | ask the device to capture |
| POST | `/api/voice/audio` | upload audio (laptop-mic fallback path) |
| POST | `/api/simulate/sensor` | ⚠️ **opt-in simulation** |
| POST | `/api/simulate/voice` | ⚠️ **opt-in simulation** |
| GET | `/api/history/summary` | energy, savings, change counts, connected time for a range |
| GET | `/api/history/samples` | stored 5 s buckets; downsampled server-side past `max_points` |
| GET | `/api/history/changes` | the light-change log, newest first |
| GET | `/api/history/sessions` | device connect/disconnect sessions |
| GET | `/api/history/export/{samples,light_changes}.csv` | raw rows as CSV |

Every history endpoint takes optional `start` / `end` in unix seconds.

CORS is enabled because the dashboard runs on a different port in development.

**The `status()` payload distinguishes two facts that are not the same thing:**

```python
"device_connected": self.device is not None,     # a board is on the socket
"readings_live":    time.time() - self.last_sensor_at < 15.0,   # it's supplying data
"sensor_error":     self.sensor_error,           # and why not, if not
```

A board with no light sensors fitted is *connected* but has nothing to report. See §26.

## 24. Persisted state — config and history

**`config/settings.json`**

```json
{
  "magnitude_steps": { "slight": 10, "normal": 25, "large": 45 },
  "manual_timeout_seconds": 1800,
  "panel_rated_watts": 18.0,
  "rolling_window_seconds": 300,
  "sensor_target_lux_default": 500,
  "panel_full_output_lux": 500,
  "history_interval_seconds": 5
}
```

**`config/modes.json`**

| id | name | target lux |
|---|---|---|
| `office` | Office | 500 |
| `reading` | Reading | 400 |
| `relax` | Relax | 150 |
| `dark` | Dark | 20 |

Both are **live-updatable** — a mode added from the dashboard is matchable by voice
immediately, with no restart, because `nlu.parse()` is handed the live list.

### 24.1 History — `laptop-server/data/history.db`

Added 2026-10-17. A SQLite file, kept forever, gitignored. Three tables:

| Table | One row per | Holds |
|---|---|---|
| `sessions` | device connection | device_id, firmware, connected_at, disconnected_at, end_reason |
| `samples` | `history_interval_seconds` bucket (5 s) | readings count, avg/min/max indoor + outdoor lux, avg + last brightness, mode, actual Wh, Wh at 100%, sensor_error |
| `light_changes` | brightness command that changed the light | from → to, source (`auto` / `hold` / `voice` / `voice_typed` / `dashboard`), reason, mode, voice transcript |

**When it records.** A session opens on the device's `hello` and closes on disconnect
(`end_reason` = `disconnect`, `replaced`, `server_stopped`). A server that died without
shutting down has its open session closed on next start at the last recorded moment,
marked `unclean_shutdown` — never at the restart time.

**What it never records — the data-integrity rule (§29) applied to disk:**
- anything outside a session — no device, no rows, so a gap in the data is a real gap
- the mock device: its `hello` carries `"simulated": true` (any `mock*` device_id is also
  treated as simulated)
- `/api/simulate/sensor` injections, even while a real board is connected — along with
  the brightness command they produce
- a value for a failed sensor: a bucket with only `sensor_unavailable` stores NULL lux
  and the reason

**Buckets average, they do not sample.** All ~5 readings in a bucket are averaged, with
min/max kept, so a short spike survives. Energy is integrated per reading with the same
`dt <= 10 s` guard as `rolling.py`.

**Change log threshold.** AUTO moves under 2% are model jitter and skipped. Voice, typed
(`/api/simulate/voice` — real commands to the real panel) and dashboard changes are
always logged.

**Performance.** Writes go through a one-thread executor: the control loop never waits on
disk and rows land in order. WAL mode lets the dashboard read during a write. ~17k rows
a day at 5 s, roughly 2 MB/day.

---

# PART VI — THE VOICE PIPELINE

## 25. The full journey of one spoken command

Trigger: the physical button on GPIO 19, or push-to-talk in the dashboard.

```
 1. Button press / POST /api/voice/record
 2. Server → device:  {"type":"record","duration_ms":5000}
 3. Device → server:  {"type":"audio_begin","sample_rate":16000,"format":"pcm_s16le"}
 4. Device streams ~5 s of PCM as binary frames, 1 KB at a time, from the 8 KB FIFO
 5. Device → server:  {"type":"audio_end"}
 6. Server assembles the bytes → pcm16_to_float32() → float32 [-1,1] @ 16 kHz
 7. MLX Whisper large-v3-turbo transcribes on the Neural Engine  →  text
 8. nlu.parse(text, live_modes, live_settings)  →  Intent
 9. Controller._apply(intent)  →  the state machine
10. send_brightness(..., await_ack=True)  →  WAIT for the device's ack   ◄── the gate
11. tts.confirmation_text(action, brightness%, savings%, language, mode)
12. edge-tts → MP3 → soundfile decode → PCM16 @ 16 kHz
13. Server → device: audio_begin, binary frames (2048 B each), audio_end
14. Device banks 320 ms preroll, then plays through the MAX98357A
```

Measured on the real board end to end: *"received 5.0s of audio → transcript → spoke 3.9s
via edge-tts"*.

## 26. Speech-to-text

**Model: `mlx-community/whisper-large-v3-turbo`**, running locally on the Mac's Neural
Engine / GPU via MLX. ~1.5 GB.

- **Loaded once, lazily**, on a background warmup at server startup. A one-sample
  transcribe forces the weights to materialise so the first real command doesn't pay the
  load cost. The server stays usable while it warms.
- **Transcription is blocking**, so it runs in a thread executor — the asyncio event loop
  stays free for WebSocket and dashboard traffic.
- The firmware records at **exactly 16 kHz**, which is what Whisper resamples to
  internally, so nothing has to be resampled on the way in.
- Whisper returns its own language guess, but **the NLU does its own detection from
  vocabulary**, which is more reliable on short Hinglish commands.

## 27. Natural-language understanding — `voice/nlu.py`

**No LLM anywhere in this path** (architectural decision #7). It is a rule parser over
vocabulary tables. Adding a dialect word is a **one-line data change, not a code change**.

### 27.1 Priority order — and why it is that order

```
1. AUTO       — "auto mode", "automatic", "अपने आप", "apne aap"
2. ABSOLUTE   — "set to 60 percent", "2000 lux", "saath pratishat"
3. MODE       — matched against the LIVE modes config
4. RELATIVE   — direction word + optional magnitude word
5. UNKNOWN    — change nothing
```

- **AUTO outranks everything** because it is an escape hatch the user must always be able
  to reach.
- **ABSOLUTE before MODE** because *"set office mode to 60%"* contains both, and the
  explicit number is the more specific instruction.
- **MODE before RELATIVE** because a mode name is unambiguous.

### 27.2 The vocabulary

Four languages, in **both Devanagari and Latin script**, covering the romanised Hinglish
people actually speak.

| Table | Examples |
|---|---|
| `AUTO_WORDS` | auto, automatic, ऑटो, स्वचालित, अपने आप, आपोआप, apne aap, khud |
| `BRIGHTER_WORDS` | brighter, increase, up, raise, boost · badhao, tez, zyada, roshni, ujala, chamkao · vadhav, jasta, prakash · बढ़ाओ, तेज, रोशनी, वाढव, जास्त, प्रकाश |
| `DIMMER_WORDS` | dimmer, dim, darker, reduce, lower · kam karo, ghatao, halka, dhima, andhera · kami kar, halke, khali · कम, घटाओ, हल्का, अंधेरा, कमी, हळू |
| `SLIGHT_WORDS` | a little, a bit, slightly · thoda, zara, kinchit · थोड़ा, ज़रा, थोडं |
| `LARGE_WORDS` | a lot, much, very, max · bahut, kaafi · khup · बहुत, काफ़ी, खूप |
| `NUMBER_WORDS` | ten…hundred, half, full, quarter · das, bees, pachas, saath, sau, aadha, poora · दस, पचास, साठ, सौ, आधा, शंभर, अर्धा |
| `PERCENT_WORDS` | percent, %, pratishat, प्रतिशत, टक्के, takke |
| `LUX_WORDS` | lux, lx, लक्स |

### 27.3 Magnitude → step size

Magnitude words map to the **configurable** steps in `settings.json`:

| Word bucket | Default step |
|---|---|
| slight | **10%** |
| normal (no magnitude word) | **25%** |
| large | **45%** |

A bare number alongside a direction word becomes the step itself: *"brighter by 15"* ⇒
+15%.

### 27.4 Worked examples

| You say | Parsed | Result |
|---|---|---|
| "auto mode" | AUTO | MANUAL cancelled immediately |
| "set to 60 percent" | ABSOLUTE, percent=60 | brightness → 0.60, MANUAL |
| "2000 lux" | ABSOLUTE, lux=2000 | `brightness_for_target_lux(2000)` |
| "set it to sixty" | ABSOLUTE, percent=60 | bare number with no unit and no direction reads as percent |
| "reading mode" | MODE `reading` | target 400 lux → fraction, MANUAL |
| "thoda tez karo" | RELATIVE, slight, +1 | **+10%**, language = `hi` |
| "khup kami kar" | RELATIVE, large, −1 | **−45%**, language = `mr` |
| "brighter by 15" | RELATIVE, explicit | **+15%** |
| "make it brighter but dimmer" | RELATIVE, −1 | **the later word in the sentence wins** |
| "banana" | UNKNOWN | **changes nothing at all** |

That last row is a deliberate rule:

> *"Unrecognised: change nothing at all. Silently guessing at a command the user did not
> give is worse than saying 'I did not understand'."*

### 27.5 Language detection

Heuristic: Devanagari script detection plus a `MARATHI_MARKERS` table of words that are
distinctively Marathi rather than Hindi (वाढव, खूप, जास्त, आहे, टक्के, vadhav, khup, aahe…).
Hinglish is detected as its own tag but **spoken back in Hindi**.

**Known limit:** a command made entirely of English words, spoken by a Hindi speaker, is
answered in English.

## 28. Text-to-speech

**Voices** (from the project spec):

| Language | Voice |
|---|---|
| English | `en-IN-NeerjaNeural` |
| Hindi | `hi-IN-SwaraNeural` |
| **Hinglish** | `hi-IN-SwaraNeural` (Hindi voice) |
| Marathi | `mr-IN-AarohiNeural` |

**Confirmation content — action + new value + live savings.** Example templates:

> **EN:** "Brightness set to 60 percent, using about 42 percent less power than full
> brightness."
> **HI:** "रोशनी 60 प्रतिशत पर सेट कर दी है, पूरी रोशनी से लगभग 42 प्रतिशत कम बिजली लग रही है।"
> **MR:** "उजेड 60 टक्क्यांवर सेट केला आहे, पूर्ण उजेडापेक्षा सुमारे 42 टक्के कमी वीज लागते आहे."
> **Unknown:** "Sorry, I did not understand that command." / "माफ़ कीजिए, मैं यह कमांड समझ नहीं पाई।"

Six action tags: `set`, `up`, `down`, `mode`, `auto`, `unknown`.

**Pipeline:** edge-tts → MP3 → **`soundfile` (libsndfile)** → PCM16 mono @ 16 kHz → down
the socket.

**Why `soundfile` and not ffmpeg:** the spec did not name a decoder. libsndfile is already
a dependency, so the server runs from a clean `pip install` **with no external binary to
install**. Logged as a deviation.

**Fallback chain**, because edge-tts calls Microsoft's servers and therefore needs internet:

```
edge-tts (3.0 s timeout)  →  assets/done.wav ("Done.")  →  a synthesised chime
```

`ensure_fallback_clip()` tries once at setup to make the fallback a real spoken word;
if that fails it writes a generated chime. The result tags `source` as `"edge-tts"` or
`"fallback"` so the dashboard can show which was used.

**Speaking is fire-and-forget by default** (`asyncio.create_task`) — the network round trip
to Microsoft must not delay the dashboard learning what happened.

---

# PART VII — THE DASHBOARD

React + TypeScript + Vite, `dashboard/`, dev server on **port 5180** (5173/5174 are usually
taken by the BolBiz project).

| Component | Shows |
|---|---|
| `StatusPanel.tsx` | mode, indoor/outdoor lux, panel %, connection state |
| `EnergyChart.tsx` | live 1 s chart: actual watts vs hypothetical 100% |
| `HistoryView.tsx` | **History tab** — stored range: energy/savings tiles, panel output + lux charts with real gaps, change log, sessions, CSV export |
| `Controls.tsx` | manual override — auto / mode / brightness slider / **Test panel** |
| `ModesManager.tsx` | add / edit / delete modes |
| `SettingsPanel.tsx` | magnitude steps, MANUAL timeout, rated wattage |
| `VoicePanel.tsx` | push-to-talk, transcript, parsed intent, confirmation |
| `api.ts` / `recorder.ts` | REST + WS client, browser audio capture |

**Push-to-talk uses the ESP32 microphone only.** It originally fell back to the laptop mic
when the board was offline, which meant you could not tell which mic you had just tested.
Now the laptop mic is behind a **separate, clearly labelled control that only appears when
no device is attached**, and its results are tagged **"via laptop mic" in red**.

---

# PART VIII — PRINCIPLES AND DECISIONS

## 29. The data-integrity rule

**No simulated, substituted or stale value is ever displayed as a real measurement.**

This was violated twice and fixed twice. It is now enforced at every layer:

| Layer | Enforcement |
|---|---|
| Firmware | A failed BH1750 returns `-1.0f`, never 0. `sendTelemetry()` sends `sensor_unavailable` with a specific reason — *"indoor BH1750 (0x23) not responding"* — instead of a reading. |
| Protocol | `sensor_unavailable` is a first-class message type with a mandatory `reason`. |
| Server | `on_sensor_unavailable()` **leaves the panel exactly where it is**. AUTO refuses to run without a real reading rather than driving from a guess. |
| Dashboard | Shows `—` and the reason, not the last-known value. `device_connected` and `readings_live` are **separate flags**. |
| Simulation | `mock_device.py` and `/api/simulate/*` are **opt-in only** and never started without saying so, prominently and unprompted. |

The two incidents, kept because they explain the rule:

1. A **mock device was left running** and the dashboard showed a screen full of simulated
   lux as if it were real.
2. Failed sensors were **clamped to 0 and sent as genuine readings** — a plausible-looking
   number that could not be distinguished from a true one.

> **A fabricated reading that looks plausible is worse than no reading.** This is an
> energy-measurement project; its credibility rests entirely on the numbers being real.

## 30. Architectural decisions

The numbering comes from the original build spec. These are the ones cited in the code:

| # | Decision |
|---|---|
| **#1** | The ESP32 is a **pure I/O peripheral** with no local decision-making |
| **#3** | **One persistent WebSocket** carries everything; the ESP32 is the client |
| **#5** | The **AUTO/MANUAL state machine** with a reset-on-command revert timer |
| **#7** | **Rule-based NLU, no LLM**, with configurable magnitude steps |
| **#8** | **No persistence** beyond the config JSONs — *amended 2026-10-17:* real-device history is logged to SQLite (§24.1); the live window is still memory-only |
| **#9** | **Linear power model** — X% brightness draws X% of rated wattage |
| **#13** | The spoken confirmation is **gated on the device's applied-ack** |

Plus the recorded deviations from the spec (`PROGRESS.md`):

- ASHRAE db-II excluded from the combined dataset
- Outdoor lux reconstructed, not measured *(approved before implementation)*
- Model selection uses a size tie-break within 2% relative MAE
- TTS decodes MP3 via `soundfile`, not ffmpeg
- `zN_S1(lux)` treated as a relative index, not absolute lux

## 31. Bugs we hit, and what fixed them

Kept because most are non-obvious and would otherwise be rediscovered.

| Symptom | Cause | Fix |
|---|---|---|
| Boot loop, nothing printed | `driver/i2s.h` links the legacy ADC driver; conflicts on core 3.x. **Compiled clean** — only flashing caught it | migrate to `driver/i2s_std.h` |
| `ledcSetup` undefined | Removed in core 3.x | `ledcAttach(pin, freq, res)` + a version shim |
| Speaker silent | Both I2S peripherals routed MCLK to GPIO 0 | explicitly unset `mck_io_num` on both |
| Mic would have been silent | Firmware hardcoded LEFT slot; INMP441 L/R floats and the slot changes between boots | peak-to-peak auto-detect + reinstall |
| Level meter pegged at 100% in silence | INMP441 idles near **−22000** | one-pole DC-blocking HPF, R = 0.995 |
| "WiFi won't connect" for hours | The network **did not exist** | boot-time scan that reports what *is* in range |
| Scan returned zero networks | Scanning during an in-flight association | scan from an **idle radio**, before the first join |
| Board streaming but server said "no device" | A second WS client silently overwrote `controller.device` | close the previous socket properly |
| Dashboard full of fake lux | A mock device left running | mock is opt-in, announced |
| Stale readings after disconnect | Dashboard kept last values | separate `device_connected` from `readings_live` |
| **Server unreachable after moving network** | `SERVER_HOST` is a compile-time constant and the laptop's DHCP lease changed | **check `ipconfig getifaddr en0` before every flash** |

## 32. Known limitations

### Model
- **MAE 0.133** — commanded brightness is off by ~13 percentage points on average. Genuinely
  useful against an always-100% incumbent; **not a precise controller**.
- **No feedback loop.** It maps a reading to a command without ever checking whether the
  room reached the setpoint. This is the single biggest architectural gap.
- Trained on **deep-plan office zones** (DF 0.0003–0.025). A small room with a large opening
  may sit outside that range, where the model is extrapolating.
- The **illuminance calibration** (§3.3) and the **setpoint policy** (§4.5) are assumptions,
  not observations.

### Voice
- **edge-tts requires internet** despite running as a local library.
- **Confirmations are long** — 7–10 s in Hindi and Marathi, since they state action, value
  and savings. Shortening them is a one-line change in `voice/tts.py`.
- The NLU vocabulary is **hand-built**; dialect words outside the tables will not match.
- Language detection is **heuristic**.

### System
- **The live window resets on restart** — the live chart and session energy totals are
  in memory. The long-term record is the SQLite history (§24.1).
- **The energy model is linear.** Real LED drivers are not, especially at the bottom.
- **One device at a time.**
- **No authentication** on the API or either WebSocket. Fine on a trusted LAN, not fine
  anywhere else.
- WiFi credentials and the server IP are **compile-time constants** — every network change
  needs a reflash.

---

# PART IX — OPERATIONS

## 33. Current status

**All six phases of the build spec are implemented, and the whole system has run end to
end on real hardware.**

### Confirmed working on the physical board

| | Evidence |
|---|---|
| Both BH1750s | `[i2c] scanning: 0x23 0x5C [2 device(s)]`, streaming real lux at 1 Hz |
| INMP441 mic | Captures signal; slot auto-detect picks LEFT every boot |
| WiFi | Joins, gets a DHCP lease, ~−63 dBm |
| WebSocket | `[device] hello from esp32-room-1 (fw 2.0.0)` |
| AUTO mode | XGBoost drives the panel from live sensor readings |
| Voice loop | `received 5.0s of audio → transcript → spoke 3.9s via edge-tts` |
| PWM path | `[light] commanded 100% -> duty 255/255 on GPIO 23` |
| Dashboard | Live chart, modes CRUD, settings, push-to-talk |

### ⚠️ Two things NOT yet verified — neither is missing code

1. **Does the LED physically light?** The panel was **disconnected** during all testing, so
   "off" was correct behaviour, not a fault. The software path is proven down to the duty
   cycle. Reconnect it and press **Test panel** (or `POST /api/light/sweep`) — it ramps
   0→255→0 over ~2.5 s. **If it stays dark while the serial log shows duty changing, it is
   wiring — first suspect is an active-LOW driver, fixed with
   `#define LIGHT_INVERT_OUTPUT 1`.**

2. **Whisper on a real human voice.** Every voice test so far captured **silence** —
   recordings were triggered remotely with nobody speaking, and the system correctly heard
   nothing and said "sorry, I did not understand". The plumbing is proven; **accuracy on a
   real accent and on the Hindi/Marathi/Hinglish command set is unproven.**

### Open improvements
- Tie the INMP441's L/R pin to GND so slot auto-detection stops being needed.
- Switch `SERVER_HOST` to **mDNS** (`<your-laptop>.local`) so DHCP changes stop
  requiring a reflash.
- The panel sweep blocks `loop()` for ~2.4 s — deliberate, but the last blocking call.

## 34. Running it

```bash
cd /Users/hardik/Projects/MOODSENSE

./.venv/bin/python laptop-server/main.py     # server    :8000
cd dashboard && npm run dev                  # dashboard :5180
```

Then open **http://localhost:5180**.

**No hardware needed to work on the software** — but note these are simulations and must be
announced as such:

```bash
./.venv/bin/python laptop-server/tools/mock_device.py --seconds 60 --interval 1
./.venv/bin/python laptop-server/tools/mock_device.py --sweep 4        # a day in 4 min
./.venv/bin/python laptop-server/tools/mock_device.py --say "make it a lot dimmer"
```

**Flashing:**

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 esp32-firmware/
arduino-cli upload -p /dev/cu.usbserial-0001 --fqbn esp32:esp32:esp32 esp32-firmware/
```

`arduino-cli`, ESP32 core 3.3.11, the three libraries and Rosetta are already installed on
the dev machine.

**Regenerating the model** (offline, one-time — you should not normally need this):

```bash
cd data-pipeline
python combine.py     # raw/ -> out/unified.parquet
python features.py    # -> out/dataset.parquet
python train.py       # -> laptop-server/model/ + out/model_comparison.csv
```

## 35. Session checklist

1. **`ipconfig getifaddr en0`** — does it match `SERVER_HOST` in the firmware
   (`adaptive_lighting.ino` line ~74)? **If not, update and reflash.** The ESP32 must be on
   the same subnet: if the laptop is `192.168.1.x` and the board comes up `192.168.0.x`,
   they cannot see each other.
2. Start the server, then the dashboard.
3. Power the ESP32; watch the serial monitor at **115200** for the boot report.
4. Confirm `device_connected: true` **and** both lux values moving.
5. If anything is odd, **the serial log names the fault.** The boot sequence reports the
   I2C scan, mic slot, WiFi scan, panel sweep and free heap, in that order.

## 36. Troubleshooting quick reference

| Symptom | Look here first |
|---|---|
| Nothing on serial, board resets forever | An `analogRead()` crept in (§9.1) |
| Speaker silent | MCLK on both I2S peripherals (§9.2); or the MAX98357A `SD` pin tied to GND (§10.2) |
| Every recording silent | Mic slot auto-detect; check the boot log's slot decision (§13.4) |
| Level meter at 100% in a quiet room | DC-blocking filter missing (§10.1) |
| Panel dark but log shows duty changing | **Wiring.** Try `LIGHT_INVERT_OUTPUT 1` (§13.2) |
| Board pings but WebSocket refuses | Stuck `loop()` (§9.3) |
| "WiFi won't connect" | Read the boot scan — is the SSID even in range? 5 GHz is invisible to the ESP32 |
| Server says no device, but board is streaming | A second client displaced it (§16.5) |
| Confirmation audio crackles | Link margin, not the audio code (§11) |
| Dashboard shows `—` for lux | Correct behaviour — check `sensor_error` for the reason (§29) |
| Board can't reach the server at all | `SERVER_HOST` vs `ipconfig getifaddr en0` (§35) |
