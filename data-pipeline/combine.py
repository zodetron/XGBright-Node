"""
Stage 1 — Combine every usable dataset in `raw/` into one unified long-format table.

The raw CSVs are wide: one row per minute, with a variable set of per-zone columns
(`z3_Light(kW)`, `z3_S1(lux)`, `z3_S1(degC)`, ...) that differs from floor to floor.
Zones are the natural unit of observation for this problem — one zone is one room with
its own luminaires and its own light sensor — so we melt the wide per-floor files into
a single long table keyed by (timestamp, building_zone).

Output: data-pipeline/out/unified.parquet
        columns: timestamp, zone_id, source_file, light_kw, plug_kw, lux_index,
                 temp_c, rh_pct
"""

import re
import sys

import pandas as pd

from config import CSV_GLOBS, OUT_DIR, RAW_DIR, RESAMPLE

# Matches the per-zone column naming used across all floor files, e.g.
# "z1_Light(kW)", "z4_Plug(kW)", "z2_S1(lux)", "z5_S1(degC)", "z3_S1(RH%)".
ZONE_COL = re.compile(r"^z(\d+)_(Light\(kW\)|Plug\(kW\)|S1\((?:lux|degC|RH%)\))$")

CHANNEL_NAMES = {
    "Light(kW)": "light_kw",
    "Plug(kW)": "plug_kw",
    "S1(lux)": "lux_index",
    "S1(degC)": "temp_c",
    "S1(RH%)": "rh_pct",
}


def discover_files():
    """Return the deduplicated list of raw CSVs to ingest.

    `archive (3).zip` unzips to the same seven 2019 floor files already present in
    `archive (2)/`, so de-duplicating on basename keeps each floor-year exactly once.
    """
    found = {}
    for pattern in CSV_GLOBS:
        for path in sorted(RAW_DIR.glob(pattern)):
            found.setdefault(path.name, path)
    return [found[name] for name in sorted(found)]


def load_one(path):
    """Load a single wide floor CSV and melt it into long per-zone rows."""
    df = pd.read_csv(path, low_memory=False)
    # A few files carry isolated corrupt rows (e.g. a bare "2" in the Date column of
    # 2019Floor6). Coerce and drop them rather than failing the whole ingest.
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    n_bad = int(df["Date"].isna().sum())
    if n_bad:
        print(f"  {path.name:<20} dropping {n_bad} row(s) with unparseable timestamps")
        df = df.dropna(subset=["Date"])
    df = df.rename(columns={"Date": "timestamp"}).set_index("timestamp")

    # Sensor/meter columns occasionally read as object dtype when a corrupt row forced
    # pandas into a mixed-type column; coerce every non-index column to numeric.
    df = df.apply(pd.to_numeric, errors="coerce")

    # Group the wide columns by (zone number, channel) so each zone becomes its own
    # frame with uniform column names.
    per_zone = {}
    for col in df.columns:
        m = ZONE_COL.match(col)
        if not m:
            continue  # AC circuits and anything unrecognised are not used here
        zone_num, channel = m.group(1), m.group(2)
        per_zone.setdefault(zone_num, {})[CHANNEL_NAMES[channel]] = col

    floor = path.stem  # e.g. "2019Floor3"
    frames = []
    for zone_num, channels in sorted(per_zone.items(), key=lambda kv: int(kv[0])):
        # A zone is only useful for this project if it has BOTH a light sensor and a
        # lighting-power meter — those are the two real signals the target is built
        # from. Zones without a sensor (all of Floor 1, plus z3 on most floors) are
        # dropped rather than imputed.
        if "lux_index" not in channels or "light_kw" not in channels:
            continue

        zone = df[list(channels.values())].rename(
            columns={v: k for k, v in channels.items()}
        )
        # Downsample from 1-minute to the pipeline resolution. Mean is right for all
        # of these channels (power and illuminance are both averageable intensities).
        zone = zone.resample(RESAMPLE).mean()
        zone["zone_id"] = f"{floor}_z{zone_num}"
        frames.append(zone)

    if not frames:
        return None

    out = pd.concat(frames).reset_index()
    out["source_file"] = path.name
    return out


def main():
    files = discover_files()
    if not files:
        sys.exit(f"No raw CSVs found under {RAW_DIR}")

    print(f"Found {len(files)} raw CSV files after de-duplication.")
    frames = []
    for path in files:
        part = load_one(path)
        if part is None:
            print(f"  {path.name:<20} skipped (no zone has both a sensor and a meter)")
            continue
        print(f"  {path.name:<20} {len(part):>8,} rows  {part.zone_id.nunique()} zones")
        frames.append(part)

    unified = pd.concat(frames, ignore_index=True)
    # Ensure every expected channel exists even if some floor lacked it entirely.
    for col in CHANNEL_NAMES.values():
        if col not in unified.columns:
            unified[col] = pd.NA

    unified = unified[
        ["timestamp", "zone_id", "source_file",
         "light_kw", "plug_kw", "lux_index", "temp_c", "rh_pct"]
    ].sort_values(["zone_id", "timestamp"], ignore_index=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / "unified.parquet"
    unified.to_parquet(dest, index=False)

    print(f"\nUnified dataset: {len(unified):,} rows, "
          f"{unified.zone_id.nunique()} zones, "
          f"{unified.timestamp.min()} to {unified.timestamp.max()}")
    print(f"Written to {dest}")


if __name__ == "__main__":
    main()
