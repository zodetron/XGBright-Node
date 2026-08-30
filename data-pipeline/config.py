"""
Shared constants for the offline data pipeline.

This stage is entirely offline and one-time: it turns the raw building datasets in
`raw/` into a training table, picks the best regression model, and exports it for the
laptop server's AUTO-mode prediction path. Nothing here runs in the live system.
"""

from pathlib import Path

# --- PATHS ---
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
OUT_DIR = Path(__file__).resolve().parent / "out"
MODEL_DIR = ROOT / "laptop-server" / "model"

# --- SOURCE SELECTION ---
# The 2018/2019 archives are minute-resolution submetering from a Singapore office
# tower: per-zone lighting power, plug power, AC power, and (floors 2-7) a per-zone
# environment sensor reporting temperature, humidity and an illuminance channel.
# `archive (3).zip` is a byte-identical duplicate of the already-extracted
# `archive (2)` folder, so we glob extracted CSVs only and de-duplicate by filename.
#
# The ASHRAE db-II dataset in raw/ is deliberately NOT merged: it is a per-subject
# thermal-comfort *survey* (one row per questionnaire response, no illuminance and no
# lighting power), covering different buildings on different continents. It shares no
# join key and no target-relevant column with the submetering data. See README.md.
CSV_GLOBS = ["archive (2)/*.csv", "archive(1)/*.csv"]

# --- SITE GEOMETRY (for the solar / outdoor-illuminance model) ---
# The source building is in Singapore. Outdoor lux is not measured anywhere in the raw
# data, so it is reconstructed physically from solar position at this location.
SITE_LAT = 1.2966
SITE_LON = 103.7764
SITE_TZ_OFFSET_H = 8.0  # Singapore Standard Time, no DST

# --- ILLUMINANCE SCALING ---
# The raw `zN_S1(lux)` channel is an integer index in roughly 0..90, not absolute lux:
# it tracks daylight and occupancy correctly in shape, but an occupied daylit office
# desk plane sits at 300-500 lux, not 45. We therefore treat it as a linear relative
# illuminance index and calibrate it onto a physically plausible desk-plane range.
# INDOOR_LUX_INDEX_FULL is the index value mapped to INDOOR_LUX_FULL_SCALE lux.
INDOOR_LUX_INDEX_FULL = 90.0
INDOOR_LUX_FULL_SCALE = 700.0

# --- LUMINAIRE / PANEL MODEL ---
# Fraction of a zone's peak lighting power treated as "100% panel". Zone lighting
# power has rare spikes (compressor inrush, submeter glitches) far above the real
# steady-state maximum, so the per-zone full-power reference is a high quantile.
ZONE_POWER_FULL_QUANTILE = 0.99

# Each zone's full-output luminaire illuminance is not assumed — it is measured from
# the data itself, using night-time samples when the lights are on and daylight is
# provably zero, where indoor_lux / brightness_frac is exactly the panel's full-output
# contribution. These bounds reject zones whose night sample is too weak or degenerate
# to give a trustworthy estimate.
PANEL_LUX_NIGHT_MIN_BRIGHTNESS = 0.15  # only well-lit night samples
PANEL_LUX_MIN = 30.0                   # below this the zone sensor is uninformative
PANEL_LUX_MIN_NIGHT_SAMPLES = 200      # need enough night samples to trust the median

# --- SETPOINT POLICY (defines the regression target) ---
# Target illuminance for an occupied workspace. EN 12464-1 specifies 500 lux on the
# task plane itself; 300 lux is the surrounding-area figure and is what these zones'
# ceiling sensors actually see at full output (median zone panel capability is ~280
# lux at the sensor), so 300 keeps the target continuous instead of saturating at 1.
SETPOINT_OCCUPIED_LUX = 300.0
# Target when the zone is unoccupied: enough to move around safely, no task lighting.
SETPOINT_UNOCCUPIED_LUX = 50.0
# A zone counts as occupied when its measured lighting power exceeds this fraction of
# the zone's full-power reference. Derived from real metered behaviour, so occupancy
# timing in the training set is real, not simulated.
OCCUPANCY_POWER_FRACTION = 0.10

# --- RESAMPLING / SIZE ---
# Raw data is 1-minute resolution across 14 files x up to 5 zones (~36M zone-minutes).
# Resample to 5 minutes, then subsample, to keep training tractable.
RESAMPLE = "5min"
MAX_TRAIN_ROWS = 600_000
RANDOM_SEED = 42
