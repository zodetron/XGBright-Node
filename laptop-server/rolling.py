"""
In-memory rolling window of live telemetry, plus the linear power model.

Nothing here is persisted (architectural decision #8). The window holds only the last
`rolling_window_seconds` of samples; anything older is discarded on insert. Restarting
the server starts the graph over, which is the intended behaviour.

Power model (architectural decision #9): the panel at X% brightness draws X% of its
rated wattage. Crude, but it is the model the project specifies and it is close enough
to true for a dimmable LED panel driven by PWM.
"""

import threading
import time
from collections import deque


class RollingWindow:
    """Fixed-duration ring of samples with running energy totals.

    Two totals are tracked and never expire: cumulative actual energy and cumulative
    energy the incumbent always-100% system would have used. The savings figure quoted
    in voice confirmations comes from these, so it reflects the whole session rather
    than just the visible graph.
    """

    def __init__(self, config):
        self._config = config
        self._lock = threading.RLock()
        self._samples = deque()

        # Session-cumulative joules. Kept outside the window so they survive eviction.
        self._actual_joules = 0.0
        self._full_joules = 0.0
        self._last_sample_time = None

    def add(self, indoor_lux, outdoor_lux, brightness, mode, mode_name, timestamp=None):
        """Record one telemetry sample and integrate energy since the previous one.

        `brightness` is the panel output fraction, 0.0-1.0.
        """
        now = timestamp if timestamp is not None else time.time()
        rated_watts = float(self._config.get("panel_rated_watts", 18.0))
        window_seconds = float(self._config.get("rolling_window_seconds", 300))

        brightness = max(0.0, min(1.0, float(brightness)))
        actual_watts = rated_watts * brightness

        with self._lock:
            # Integrate power over the gap since the last sample. The first sample of a
            # session has no gap and so contributes no energy, only a data point.
            if self._last_sample_time is not None:
                dt = now - self._last_sample_time
                # Guard against clock jumps and long stalls (e.g. WiFi dropout) which
                # would otherwise inject a huge fictional energy block.
                if 0 < dt <= 10.0:
                    self._actual_joules += actual_watts * dt
                    self._full_joules += rated_watts * dt
            self._last_sample_time = now

            self._samples.append({
                "t": now,
                "indoor_lux": float(indoor_lux),
                "outdoor_lux": float(outdoor_lux),
                "brightness": brightness,
                "actual_watts": actual_watts,
                "full_watts": rated_watts,
                "mode": mode,
                "mode_name": mode_name,
            })

            cutoff = now - window_seconds
            while self._samples and self._samples[0]["t"] < cutoff:
                self._samples.popleft()

    def series(self):
        """The full window, oldest first — what the dashboard chart plots."""
        with self._lock:
            return list(self._samples)

    def latest(self):
        with self._lock:
            return dict(self._samples[-1]) if self._samples else None

    def savings_percent(self):
        """How much less energy the system used than an always-100% panel would have.

        Returns a percentage in 0-100. Falls back to the instantaneous figure before
        enough time has passed to integrate anything, so a confirmation spoken in the
        first seconds of a session still quotes something meaningful rather than zero.
        """
        with self._lock:
            if self._full_joules > 1e-6:
                saved = 1.0 - (self._actual_joules / self._full_joules)
                return max(0.0, min(100.0, saved * 100.0))
            if self._samples:
                return max(0.0, min(100.0,
                                    (1.0 - self._samples[-1]["brightness"]) * 100.0))
            return 0.0

    def energy_summary(self):
        with self._lock:
            return {
                "actual_wh": self._actual_joules / 3600.0,
                "full_wh": self._full_joules / 3600.0,
                "saved_wh": (self._full_joules - self._actual_joules) / 3600.0,
                "savings_percent": self.savings_percent(),
                "samples": len(self._samples),
            }

    def reset(self):
        with self._lock:
            self._samples.clear()
            self._actual_joules = 0.0
            self._full_joules = 0.0
            self._last_sample_time = None
