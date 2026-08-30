# Data Pipeline & Model Selection

Offline, one-time stage. It turns the raw building datasets in `raw/` into a training
table, compares candidate regressors, and exports the winner to `laptop-server/model/`
for the server's AUTO-mode prediction path. Nothing here runs in the live system.

## Running it

```bash
python combine.py     # raw/ -> out/unified.parquet
python features.py    # -> out/dataset.parquet
python train.py       # -> laptop-server/model/ + out/model_comparison.csv
```

## What the raw data actually contains

The 2018 and 2019 archives are minute-resolution submetering from a Singapore office
tower — per-zone lighting power, plug power, AC power, and on floors 2–7 a per-zone
environment sensor reporting temperature, humidity and an illuminance channel.

Two things are worth knowing before trusting any of it:

- **`archive (3).zip` is a byte-identical duplicate** of the already-extracted
  `archive (2)/`. `combine.py` de-duplicates on filename so each floor-year is counted
  once.
- **The `zN_S1(lux)` channel is an index, not absolute lux.** It is integer-valued and
  tops out around 90, whereas a real office task plane sits at 300–500 lux. Its *shape*
  is right — it rises at 06:00, plateaus through the working day, and collapses after
  19:00 — so it is treated as a linear relative illuminance index and calibrated onto a
  physical scale (`INDOOR_LUX_INDEX_FULL` → `INDOOR_LUX_FULL_SCALE` in `config.py`).
  The absolute lux figures downstream inherit that calibration assumption.

Floor 1 has no environment sensors at all and is dropped. Sensors only came online in
March 2019 and July 2018 respectively, so coverage before those dates is empty.

**ASHRAE db-II is deliberately not merged.** It is a per-subject thermal-comfort
questionnaire — one row per response, no illuminance, no lighting power, different
buildings on different continents. It shares no join key and no target-relevant column
with the submetering data. Combining them would produce a table that is mostly nulls
and teaches the model nothing about lighting.

## Where outdoor lux comes from

Outdoor illuminance is the one quantity nothing in `raw/` measured. Rather than
inventing it, it is reconstructed from the real indoor sensor through real solar
geometry:

1. **Solar position** at the site (NOAA algorithm) gives clear-sky outdoor illuminance
   `E_clear(t)`, peaking near 115 klux under Singapore's overhead sun.
2. **Each zone's panel output is measured, not assumed.** At night there is no daylight
   term, so whatever the sensor reads is the luminaires alone — `indoor_lux /
   brightness_frac` on night samples recovers that zone's full-output illuminance
   directly. Median across zones is ~280 lux; the spread runs from 30 to 1290.
3. **Daylight is separated from artificial light**:
   `daylight_indoor = indoor_lux − brightness_frac × panel_lux[zone]`.
4. **Each zone's daylight factor** is the 95th percentile of
   `daylight_indoor / E_clear` over daytime hours — on the clearest moments that ratio
   approaches the zone's true DF. Values run 0.0003 to 0.025, a realistic range from
   deep-plan interior to well-glazed perimeter.
5. **Sky clearness** then follows from how far the zone fell short of its own clear-sky
   potential, and `outdoor_lux = E_clear × clearness`.

So outdoor lux is derived from real measurements, not simulated. What remains a design
choice rather than an observation is the **setpoint policy** that defines the target.

## The target

The panel output that would bring the sensor plane to its setpoint given the daylight
already present:

```
target_brightness = clip((setpoint − daylight_indoor) / panel_lux[zone], 0, 1)
```

Occupancy selects the setpoint (300 lux occupied, 50 lux unoccupied) and is taken from
real metered lighting power, so the occupied/unoccupied rhythm is the building's real
one. The resulting target is continuous — mean 0.38, only 3% at the floor and 10% at
the ceiling — rather than a disguised binary occupancy flag.

## Features

Every feature is a pure function of the three quantities the deployed system actually
has: indoor lux, outdoor lux, and the wall clock. Nothing in training is unavailable at
inference time.

`indoor_lux`, `outdoor_lux`, `log_indoor_lux`, `log_outdoor_lux`, `lux_ratio`,
`tod_minutes`, `tod_sin`, `tod_cos`, `day_of_week`, `is_weekend`

The log transforms keep the low end resolvable across four orders of magnitude, and the
cyclic time encoding stops 23:59 and 00:00 from being maximally distant.

## Evaluation

**Split is chronological, not random.** Consecutive 5-minute samples from the same zone
are near-duplicates, so a random split would leak almost every test row's answer into
training and report an inflated score. Training covers 2018-07-01 → 2019-10-11
(480,000 rows); the held-out test set is 2019-10-11 → 2019-12-31 (120,000 rows).

| Model | MAE | RMSE | R² |
|---|---|---|---|
| Random Forest | 0.1304 | 0.2059 | 0.609 |
| **XGBoost (selected)** | **0.1328** | **0.2061** | **0.608** |
| LightGBM | 0.1339 | 0.2064 | 0.607 |
| Neural net (MLP 64-32) | 0.1426 | 0.2134 | 0.580 |
| Ridge regression | 0.1958 | 0.2620 | 0.366 |
| Mean baseline | 0.2866 | 0.3303 | −0.007 |
| *Always 100% (incumbent)* | *0.6432* | *0.7225* | *−3.820* |

MAE is in target units, so 0.1328 means the commanded brightness is off by about 13
percentage points on average.

## Why XGBoost won

Random Forest had the lowest raw MAE, by 1.8% relative — about a third of one percent
of actual brightness, which is invisible in a room. It serialises to **181.7 MB**.
XGBoost reaches statistically indistinguishable accuracy in **8.5 MB**.

`train.py` therefore selects on lowest MAE with an explicit efficiency tie-break:
models within 2% relative MAE of the leader are treated as tied and decided on
serialised size. This artifact is loaded into a live server process that also holds a
Whisper model in memory, so 173 MB of resident set is a real cost and the accuracy it
buys is not.

Inference measured at **0.46 ms** per single-row prediction — ample headroom for the
1-second control loop.

## Reading the headline number

Against the naive system this replaces — a panel pinned at 100% — the model cuts MAE
from 0.643 to 0.133. That gap is the energy-saving opportunity: across the test set the
mean commanded output is ~0.38 rather than 1.0.

Treat that as an upper bound on savings, not a promise. It rests on the setpoint policy
above being the right definition of "ideal", and on the illuminance calibration in
`config.py`. Both are stated assumptions, and both are worth re-checking against the
real BH1750 readings once the hardware is running — the calibration constants are the
first thing to revisit if the physical system dims too aggressively.
