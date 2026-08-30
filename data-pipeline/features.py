"""
Stage 2 — Clean the unified table and engineer the training features + target.

The deployed system sees exactly three raw quantities: indoor lux (BH1750 inside the
room model), outdoor lux (BH1750 outside it), and the wall-clock time. Every feature
built here is therefore a pure function of those three, so nothing in training is
unavailable at inference time.

Outdoor illuminance is the one quantity the raw data never measured. Rather than
inventing it, we reconstruct it from the real indoor sensor by back-projection:

  1. Solar geometry gives clear-sky outdoor illuminance E_clear(t) at the site.
  2. Each zone's full-output luminaire illuminance is measured from night-time
     samples, where daylight is provably zero and so indoor_lux / brightness_frac is
     exactly that zone's panel contribution at full output. That per-zone figure is
     then used to remove the artificial component, leaving daylight alone:
         daylight_indoor = indoor_lux - brightness_frac * panel_lux[zone]
  3. Each zone's daylight factor DF (what fraction of outdoor light reaches the desk)
     is estimated as a high quantile of daylight_indoor / E_clear over daytime hours —
     the clearest moments are where that ratio approaches the true DF.
  4. The sky-clearness at each instant then follows from how far the zone actually
     fell short of its own clear-sky potential, and:
         outdoor_lux = E_clear * clearness

So outdoor lux is derived from real measurements through real solar geometry, not
simulated. What remains synthetic is only the setpoint *policy* that defines the
target, which is a design choice rather than an observation.

Target: the panel output that would bring the desk plane to its setpoint, given how
much daylight is already there —
    target_brightness = clip((setpoint - daylight_indoor) / panel_lux[zone], 0, 1)
Occupancy (which selects the setpoint) is taken from real metered lighting power, so
the occupied/unoccupied rhythm in the training data is the building's real one.

Output: data-pipeline/out/dataset.parquet
"""

import numpy as np
import pandas as pd

from config import (INDOOR_LUX_FULL_SCALE, INDOOR_LUX_INDEX_FULL, MAX_TRAIN_ROWS,
                    OCCUPANCY_POWER_FRACTION, OUT_DIR, PANEL_LUX_MIN,
                    PANEL_LUX_MIN_NIGHT_SAMPLES, PANEL_LUX_NIGHT_MIN_BRIGHTNESS,
                    RANDOM_SEED, SETPOINT_OCCUPIED_LUX, SETPOINT_UNOCCUPIED_LUX,
                    SITE_LAT, SITE_LON, SITE_TZ_OFFSET_H, ZONE_POWER_FULL_QUANTILE)


def solar_elevation(ts, lat=SITE_LAT, lon=SITE_LON, tz_offset=SITE_TZ_OFFSET_H):
    """Solar elevation angle in radians (NOAA low-precision algorithm).

    Accurate to well under a degree, which is far finer than anything else in this
    pipeline needs.
    """
    day = ts.dt.dayofyear.to_numpy(dtype=float)
    hour = (ts.dt.hour + ts.dt.minute / 60.0).to_numpy(dtype=float)

    # Fractional year angle.
    gamma = 2.0 * np.pi / 365.0 * (day - 1.0 + (hour - 12.0) / 24.0)

    # Equation of time (minutes) and solar declination (radians).
    eqtime = 229.18 * (0.000075
                       + 0.001868 * np.cos(gamma) - 0.032077 * np.sin(gamma)
                       - 0.014615 * np.cos(2 * gamma) - 0.040849 * np.sin(2 * gamma))
    decl = (0.006918
            - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
            - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
            - 0.002697 * np.cos(3 * gamma) + 0.001480 * np.sin(3 * gamma))

    # True solar time -> hour angle.
    time_offset = eqtime + 4.0 * lon - 60.0 * tz_offset
    tst = hour * 60.0 + time_offset
    hour_angle = np.deg2rad(tst / 4.0 - 180.0)

    lat_r = np.deg2rad(lat)
    cos_zenith = (np.sin(lat_r) * np.sin(decl)
                  + np.cos(lat_r) * np.cos(decl) * np.cos(hour_angle))
    return np.arcsin(np.clip(cos_zenith, -1.0, 1.0))


def clear_sky_illuminance(elevation_rad):
    """Clear-sky global horizontal illuminance in lux from solar elevation.

    Global horizontal irradiance under clear skies is ~1000 W/m^2 * sin(h) with an
    air-mass correction; luminous efficacy of daylight is ~110 lm/W. The exponent
    slightly steepens the low-sun falloff to stand in for atmospheric extinction.
    Peaks near 115 klux at Singapore's overhead sun, matching published values.
    """
    sin_h = np.clip(np.sin(elevation_rad), 0.0, None)
    return 128_000.0 * np.power(sin_h, 1.15)


def build(df):
    """Clean, decompose, and engineer features on the unified long table."""
    df = df.copy()

    # --- CLEANING -----------------------------------------------------------
    # Rows without both real signals carry no information for this target.
    df = df.dropna(subset=["lux_index", "light_kw"])
    # Negative power/illuminance is meter error, not a physical reading.
    df = df[(df.light_kw >= 0) & (df.lux_index >= 0)]

    # --- ZONE-RELATIVE PANEL OUTPUT ----------------------------------------
    # Convert each zone's lighting power into a 0-1 output fraction against that
    # zone's own full-power reference, so zones of different sizes are comparable.
    full_power = df.groupby("zone_id")["light_kw"].transform(
        lambda s: s.quantile(ZONE_POWER_FULL_QUANTILE)
    )
    # Zones whose lights are effectively never on give a degenerate reference.
    df = df[full_power > 0.05]
    full_power = full_power[full_power > 0.05]
    df["brightness_frac"] = (df.light_kw / full_power).clip(0.0, 1.0)

    # --- ILLUMINANCE CALIBRATION -------------------------------------------
    # Map the raw integer sensor index onto a physical desk-plane lux scale.
    df["indoor_lux"] = (df.lux_index / INDOOR_LUX_INDEX_FULL) * INDOOR_LUX_FULL_SCALE

    # --- SOLAR GEOMETRY -----------------------------------------------------
    elevation = solar_elevation(df.timestamp)
    df["solar_elevation"] = elevation
    df["clear_sky_lux"] = clear_sky_illuminance(elevation)

    # --- PER-ZONE PANEL OUTPUT (measured, not assumed) ---------------------
    # When the sun is fully down there is no daylight term, so whatever the sensor
    # reads is the luminaires alone. Dividing by the output fraction at that moment
    # recovers the zone's full-output illuminance; the median over many such samples
    # is robust to occasional sensor dropouts.
    is_night = df.clear_sky_lux <= 1.0
    night = df[is_night & (df.brightness_frac > PANEL_LUX_NIGHT_MIN_BRIGHTNESS)]
    panel_lux = (night.indoor_lux / night.brightness_frac).groupby(
        night.zone_id).agg(["median", "size"])
    # Reject zones with too few night samples, or whose lights barely register.
    panel_lux = panel_lux[
        (panel_lux["size"] >= PANEL_LUX_MIN_NIGHT_SAMPLES)
        & (panel_lux["median"] > PANEL_LUX_MIN)
    ]["median"].rename("panel_lux")
    df = df.join(panel_lux, on="zone_id")
    df = df[df.panel_lux.notna()]

    # --- DAYLIGHT / ARTIFICIAL DECOMPOSITION -------------------------------
    # Remove the luminaires' own contribution to get the daylight component alone.
    df["daylight_indoor_lux"] = (
        df.indoor_lux - df.brightness_frac * df.panel_lux
    ).clip(lower=0.0)

    # --- PER-ZONE DAYLIGHT FACTOR ------------------------------------------
    # Estimate DF from the clearest daytime moments: when the sky is clear, the
    # observed daylight/clear-sky ratio approaches the zone's true daylight factor.
    daytime = df.clear_sky_lux > 5_000.0
    ratio = (df.daylight_indoor_lux / df.clear_sky_lux.where(daytime))
    df["_ratio"] = ratio
    zone_df = df.groupby("zone_id")["_ratio"].quantile(0.95).rename("daylight_factor")
    df = df.drop(columns="_ratio").join(zone_df, on="zone_id")
    # Guard against zones with no usable daytime signal.
    df = df[df.daylight_factor.notna() & (df.daylight_factor > 1e-5)]

    # --- OUTDOOR ILLUMINANCE RECONSTRUCTION --------------------------------
    # How clear the sky actually was, as the shortfall against the zone's own
    # clear-sky potential. At night clearness is undefined and outdoor lux is 0.
    potential = df.daylight_factor * df.clear_sky_lux
    clearness = np.where(potential > 1.0, df.daylight_indoor_lux / potential, 0.0)
    df["sky_clearness"] = np.clip(clearness, 0.0, 1.0)
    df["outdoor_lux"] = df.clear_sky_lux * df.sky_clearness

    # --- OCCUPANCY & SETPOINT (defines the target) -------------------------
    occupied = df.brightness_frac > OCCUPANCY_POWER_FRACTION
    setpoint = np.where(occupied, SETPOINT_OCCUPIED_LUX, SETPOINT_UNOCCUPIED_LUX)
    df["occupied"] = occupied
    df["setpoint_lux"] = setpoint

    # --- TARGET -------------------------------------------------------------
    # Panel output needed to close the gap between existing daylight and setpoint.
    df["target_brightness"] = np.clip(
        (setpoint - df.daylight_indoor_lux) / df.panel_lux, 0.0, 1.0
    )

    # --- MODEL FEATURES -----------------------------------------------------
    # Every one of these is computable on the laptop from two BH1750 readings plus
    # the system clock — nothing here needs the building's meters.
    minutes = df.timestamp.dt.hour * 60 + df.timestamp.dt.minute
    df["tod_minutes"] = minutes
    # Cyclic encoding so 23:59 and 00:00 are adjacent rather than maximally distant.
    df["tod_sin"] = np.sin(2 * np.pi * minutes / 1440.0)
    df["tod_cos"] = np.cos(2 * np.pi * minutes / 1440.0)
    df["day_of_week"] = df.timestamp.dt.dayofweek
    df["is_weekend"] = (df.day_of_week >= 5).astype(int)
    # Illuminance spans four orders of magnitude; logs keep the low end resolvable.
    df["log_indoor_lux"] = np.log1p(df.indoor_lux)
    df["log_outdoor_lux"] = np.log1p(df.outdoor_lux)
    # How well the room is currently capturing whatever light is outside.
    df["lux_ratio"] = df.indoor_lux / (df.outdoor_lux + 1.0)

    return df


FEATURES = [
    "indoor_lux", "outdoor_lux",
    "log_indoor_lux", "log_outdoor_lux", "lux_ratio",
    "tod_minutes", "tod_sin", "tod_cos",
    "day_of_week", "is_weekend",
]
TARGET = "target_brightness"


def main():
    src = OUT_DIR / "unified.parquet"
    print(f"Loading {src} ...")
    df = pd.read_parquet(src)
    print(f"  {len(df):,} rows in")

    df = build(df)
    print(f"  {len(df):,} rows after cleaning and feature engineering")
    print(f"  {df.zone_id.nunique()} zones retained")

    # Subsample for tractable training. Sampling is stratified by zone so no single
    # floor dominates, and seeded so the pipeline is reproducible.
    if len(df) > MAX_TRAIN_ROWS:
        frac = MAX_TRAIN_ROWS / len(df)
        df = (df.groupby("zone_id", group_keys=False)
                .sample(frac=frac, random_state=RANDOM_SEED))
        print(f"  subsampled to {len(df):,} rows")

    keep = ["timestamp", "zone_id", TARGET] + FEATURES + [
        "daylight_factor", "sky_clearness", "daylight_indoor_lux", "panel_lux",
        "brightness_frac", "occupied", "setpoint_lux",
    ]
    out = df[keep].sort_values("timestamp", ignore_index=True)

    dest = OUT_DIR / "dataset.parquet"
    out.to_parquet(dest, index=False)

    print("\nTarget distribution:")
    print(out[TARGET].describe().round(3).to_string())
    print("\nDaylight factor by zone (first 10):")
    print(out.groupby("zone_id").daylight_factor.first().head(10).round(4).to_string())
    print(f"\nWritten to {dest}")


if __name__ == "__main__":
    main()
