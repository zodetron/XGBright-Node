"""
The AUTO / MANUAL mode state machine (architectural decision #5).

  AUTO    — the default. Brightness is whatever the ML model predicts from the current
            sensor readings, recomputed on every telemetry sample.
  MANUAL  — entered by any voice command or dashboard override that sets a specific
            mode or target. Brightness is held fixed and sensor readings are ignored.

A configurable timer (default 30 min) reverts MANUAL back to AUTO on expiry. Every new
command resets that timer, whether it switches mode or just nudges brightness — so a
user who keeps adjusting never gets yanked back to AUTO mid-conversation. Saying "auto
mode" cancels MANUAL immediately, bypassing the timer entirely.

The timer is checked lazily rather than run on a background thread: expiry only matters
at the moment someone asks what the state is, and every telemetry sample asks.
"""

import threading
import time

AUTO = "AUTO"
MANUAL = "MANUAL"


class ModeState:
    def __init__(self, config):
        self._config = config
        self._lock = threading.RLock()

        self._mode = AUTO
        self._active_mode_id = None      # which named mode, when one was selected
        self._active_mode_name = None
        self._target_brightness = 0.0    # panel output fraction held in MANUAL
        self._manual_since = None
        self._last_command_at = None
        self._last_reason = "startup"

    # --- queries ------------------------------------------------------------

    def snapshot(self):
        """Current state, after applying any pending timeout expiry."""
        with self._lock:
            self._expire_if_due()
            remaining = None
            if self._mode == MANUAL and self._last_command_at is not None:
                timeout = float(self._config.get("manual_timeout_seconds", 1800))
                remaining = max(0.0, timeout - (time.time() - self._last_command_at))
            return {
                "mode": self._mode,
                "active_mode_id": self._active_mode_id,
                "active_mode_name": self._active_mode_name,
                "target_brightness": self._target_brightness,
                "manual_seconds_remaining": remaining,
                "last_reason": self._last_reason,
            }

    @property
    def mode(self):
        with self._lock:
            self._expire_if_due()
            return self._mode

    def _expire_if_due(self):
        """Revert MANUAL -> AUTO once the timeout since the last command has elapsed."""
        if self._mode != MANUAL or self._last_command_at is None:
            return
        timeout = float(self._config.get("manual_timeout_seconds", 1800))
        if time.time() - self._last_command_at >= timeout:
            self._mode = AUTO
            self._active_mode_id = None
            self._active_mode_name = None
            self._manual_since = None
            self._last_command_at = None
            self._last_reason = f"manual timeout ({timeout:.0f}s) expired"

    # --- transitions --------------------------------------------------------

    def _enter_manual(self, reason):
        """Shared bookkeeping for every MANUAL-entering command.

        Crucially this always refreshes `_last_command_at`, which is what resets the
        revert timer — so a relative nudge extends the hold exactly as a mode switch
        does.
        """
        if self._mode != MANUAL:
            self._manual_since = time.time()
        self._mode = MANUAL
        self._last_command_at = time.time()
        self._last_reason = reason

    def set_auto(self, reason="explicit auto"):
        """Drop straight back to AUTO, cancelling any MANUAL hold immediately."""
        with self._lock:
            self._mode = AUTO
            self._active_mode_id = None
            self._active_mode_name = None
            self._manual_since = None
            self._last_command_at = None
            self._last_reason = reason
            return self.snapshot()

    def set_mode(self, mode, brightness, reason="mode selected"):
        """Switch to a named mode from the modes config, holding it in MANUAL."""
        with self._lock:
            self._enter_manual(reason)
            self._active_mode_id = mode["id"]
            self._active_mode_name = mode["name"]
            self._target_brightness = max(0.0, min(1.0, float(brightness)))
            return self.snapshot()

    def set_brightness(self, brightness, reason="absolute set"):
        """Pin an explicit output fraction, clearing any named-mode association.

        The named mode is dropped because once the user nudges the level the state no
        longer *is* "Office" — reporting it as such on the dashboard would be a lie.
        """
        with self._lock:
            self._enter_manual(reason)
            self._active_mode_id = None
            self._active_mode_name = None
            self._target_brightness = max(0.0, min(1.0, float(brightness)))
            return self.snapshot()

    def adjust_brightness(self, delta, current_brightness, reason="relative adjust"):
        """Apply a relative change to the level currently in effect.

        In AUTO the baseline is whatever the model last commanded, so "a bit brighter"
        nudges from what the user can actually see rather than from a stale MANUAL
        target they never set.
        """
        with self._lock:
            baseline = (self._target_brightness if self._mode == MANUAL
                        else float(current_brightness))
            self._enter_manual(reason)
            self._active_mode_id = None
            self._active_mode_name = None
            self._target_brightness = max(0.0, min(1.0, baseline + float(delta)))
            return self.snapshot()

    def resolve_brightness(self, predicted):
        """The output fraction to actually command right now.

        In MANUAL this is the held target; in AUTO it is the model's prediction. Called
        once per telemetry sample, which is also what drives lazy timeout expiry.
        """
        with self._lock:
            self._expire_if_due()
            if self._mode == MANUAL:
                return self._target_brightness
            self._target_brightness = max(0.0, min(1.0, float(predicted)))
            return self._target_brightness
