"""
AUTO-mode brightness prediction.

Loads the model chosen by the offline pipeline (`data-pipeline/train.py`) and turns a
live sensor reading into a panel output fraction. The feature vector is built from the
exact column list recorded in model_metadata.json, in that order — the pipeline and the
server must agree on it, so it is read from the artifact rather than duplicated here.
"""

import json
import math
from pathlib import Path

import joblib
import pandas as pd

MODEL_DIR = Path(__file__).resolve().parent / "model"


class BrightnessPredictor:
    """Wraps the exported regressor. Degrades to a heuristic if it cannot be loaded."""

    def __init__(self, model_dir=MODEL_DIR):
        self._model = None
        self._meta = {}
        model_path = Path(model_dir) / "brightness_model.joblib"
        meta_path = Path(model_dir) / "model_metadata.json"
        try:
            self._model = joblib.load(model_path)
            self._meta = json.loads(meta_path.read_text())
            self._features = self._meta["features"]
            print(f"[predictor] loaded {self._meta.get('model')} "
                  f"(MAE {self._meta.get('metrics', {}).get('mae', float('nan')):.4f}) "
                  f"from {model_path.name}")
        except Exception as exc:
            # The server is still useful without the model — MANUAL mode and the whole
            # voice pipeline work fine — so log loudly and fall back rather than exit.
            print(f"[predictor] WARNING: could not load model ({exc}). "
                  f"AUTO mode will use the daylight-offset fallback.")
            self._features = []

    @property
    def info(self):
        return {
            "loaded": self._model is not None,
            "model": self._meta.get("model", "fallback heuristic"),
            "metrics": self._meta.get("metrics", {}),
        }

    def _feature_row(self, indoor_lux, outdoor_lux, when):
        """Build the feature dict. Must mirror data-pipeline/features.py exactly."""
        minutes = when.hour * 60 + when.minute
        return {
            "indoor_lux": indoor_lux,
            "outdoor_lux": outdoor_lux,
            "log_indoor_lux": math.log1p(max(0.0, indoor_lux)),
            "log_outdoor_lux": math.log1p(max(0.0, outdoor_lux)),
            "lux_ratio": indoor_lux / (outdoor_lux + 1.0),
            "tod_minutes": minutes,
            "tod_sin": math.sin(2 * math.pi * minutes / 1440.0),
            "tod_cos": math.cos(2 * math.pi * minutes / 1440.0),
            "day_of_week": when.weekday(),
            "is_weekend": 1 if when.weekday() >= 5 else 0,
        }

    def predict(self, indoor_lux, outdoor_lux, when):
        """Return the panel output fraction for these conditions, clipped to [0, 1]."""
        indoor_lux = max(0.0, float(indoor_lux))
        outdoor_lux = max(0.0, float(outdoor_lux))

        if self._model is None:
            return self._fallback(indoor_lux)

        row = self._feature_row(indoor_lux, outdoor_lux, when)
        # A DataFrame with named columns, not a bare array — the model was fitted with
        # feature names and warns otherwise.
        frame = pd.DataFrame([[row[f] for f in self._features]],
                             columns=self._features)
        value = float(self._model.predict(frame)[0])
        return max(0.0, min(1.0, value))

    @staticmethod
    def _fallback(indoor_lux, setpoint=300.0, panel_lux=500.0):
        """Fill the gap between measured light and setpoint. Used only if load failed."""
        return max(0.0, min(1.0, (setpoint - indoor_lux) / panel_lux))
