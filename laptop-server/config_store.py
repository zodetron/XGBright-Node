"""
Live-reloadable JSON configuration.

Deliberately not a database (architectural decision #8): these are small, rarely
changing settings files that a human should be able to read and edit by hand. The
dashboard writes through this module, and every write lands on disk immediately so the
running process and the file never disagree.

Two files, both under `config/`:
  modes.json     — the user's list of lighting modes {id, name, target_lux}
  settings.json  — magnitude step sizes, MANUAL timeout, panel wattage, window size

Built-in modes like "office" and "dark" are ordinary entries in modes.json, not special
cases in code (architectural decision #6) — they can be renamed, retargeted or deleted
like any other.
"""

import json
import re
import threading
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MODES_FILE = CONFIG_DIR / "modes.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Fallbacks used only if a config file is missing or unreadable at startup, so the
# server always comes up rather than refusing to start over a deleted file.
DEFAULT_MODES = {
    "modes": [
        {"id": "office", "name": "Office", "target_lux": 500},
        {"id": "reading", "name": "Reading", "target_lux": 400},
        {"id": "relax", "name": "Relax", "target_lux": 150},
        {"id": "dark", "name": "Dark", "target_lux": 20},
    ]
}
DEFAULT_SETTINGS = {
    "magnitude_steps": {"slight": 10, "normal": 25, "large": 45},
    "manual_timeout_seconds": 1800,
    "panel_rated_watts": 18.0,
    "rolling_window_seconds": 300,
    "sensor_target_lux_default": 500,
    "panel_full_output_lux": 500,
}

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name):
    """Turn a display name into a stable id. Falls back to 'mode' if nothing survives."""
    return _SLUG.sub("-", name.strip().lower()).strip("-") or "mode"


class ConfigStore:
    """Thread-safe read/write access to the JSON config files.

    The server touches config from several places at once — the sensor loop reads
    settings, the dashboard writes modes, the voice pipeline reads step sizes — so
    every access takes the lock and every mutation writes through to disk.
    """

    def __init__(self, config_dir=CONFIG_DIR):
        self._dir = Path(config_dir)
        self._lock = threading.RLock()
        self._modes_path = self._dir / "modes.json"
        self._settings_path = self._dir / "settings.json"
        self._modes = self._load(self._modes_path, DEFAULT_MODES)
        self._settings = self._load(self._settings_path, DEFAULT_SETTINGS)
        # A settings file written by an older build may lack newly added keys; fill
        # them in rather than crashing on a missing lookup later.
        for key, value in DEFAULT_SETTINGS.items():
            self._settings.setdefault(key, value)

    # --- disk ---------------------------------------------------------------

    @staticmethod
    def _load(path, fallback):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(fallback))  # deep copy

    def _write(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file and replace, so a crash mid-write cannot leave
        # a truncated config that fails to parse on next startup.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)

    # --- settings -----------------------------------------------------------

    @property
    def settings(self):
        with self._lock:
            return json.loads(json.dumps(self._settings))

    def get(self, key, default=None):
        with self._lock:
            return self._settings.get(key, default)

    def update_settings(self, patch):
        """Shallow-merge a patch into settings and persist. Returns the new settings."""
        with self._lock:
            for key, value in patch.items():
                if key not in DEFAULT_SETTINGS:
                    continue  # ignore unknown keys rather than accumulating junk
                if key == "magnitude_steps" and isinstance(value, dict):
                    # Merge nested so a partial update doesn't wipe the other steps.
                    merged = dict(self._settings.get("magnitude_steps", {}))
                    merged.update({k: v for k, v in value.items()
                                   if k in DEFAULT_SETTINGS["magnitude_steps"]})
                    self._settings[key] = merged
                else:
                    self._settings[key] = value
            self._write(self._settings_path, self._settings)
            return json.loads(json.dumps(self._settings))

    # --- modes --------------------------------------------------------------

    @property
    def modes(self):
        with self._lock:
            return json.loads(json.dumps(self._modes.get("modes", [])))

    def find_mode(self, mode_id):
        with self._lock:
            for mode in self._modes.get("modes", []):
                if mode["id"] == mode_id:
                    return dict(mode)
            return None

    def add_mode(self, name, target_lux):
        with self._lock:
            modes = self._modes.setdefault("modes", [])
            mode_id = slugify(name)
            # Ids must stay unique — the NLU and the dashboard both key on them.
            existing = {m["id"] for m in modes}
            if mode_id in existing:
                suffix = 2
                while f"{mode_id}-{suffix}" in existing:
                    suffix += 1
                mode_id = f"{mode_id}-{suffix}"
            mode = {"id": mode_id, "name": name.strip(),
                    "target_lux": int(target_lux)}
            modes.append(mode)
            self._write(self._modes_path, self._modes)
            return dict(mode)

    def update_mode(self, mode_id, name=None, target_lux=None):
        with self._lock:
            for mode in self._modes.get("modes", []):
                if mode["id"] != mode_id:
                    continue
                if name is not None:
                    mode["name"] = name.strip()
                if target_lux is not None:
                    mode["target_lux"] = int(target_lux)
                self._write(self._modes_path, self._modes)
                return dict(mode)
            return None

    def delete_mode(self, mode_id):
        with self._lock:
            modes = self._modes.get("modes", [])
            remaining = [m for m in modes if m["id"] != mode_id]
            if len(remaining) == len(modes):
                return False
            self._modes["modes"] = remaining
            self._write(self._modes_path, self._modes)
            return True
