"""
Persistent history of real device sessions, in a local SQLite file.

This is the one exception to "nothing is persisted" (architectural decision #8): the
rolling window still lives in memory and resets on restart, but alongside it every
session with a real ESP32 is written to `laptop-server/data/history.db`, kept forever.

Three tables:
  sessions       — one row per device connection, opened on the device's `hello`
  samples        — one row per `history_interval_seconds` bucket (default 5 s),
                   aggregating every 1 Hz reading that landed in it
  light_changes  — one row per brightness command that actually changed the light

What is never written (the data-integrity rule, §29):
  * anything while no session is open — no device, no rows, so gaps are real gaps
  * a mock device (`tools/mock_device.py`) or `/api/simulate/sensor` — simulated
    readings must never be indistinguishable from measured ones
  * a substituted value for a failed sensor — such a bucket stores NULL lux and the
    device's reason, never a 0

Writes go through a single-thread executor so the 1 Hz control loop never waits on
disk, and so rows land in the order they were produced.
"""

import asyncio
import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "history.db"

# An AUTO tick that moves the panel by less than this is model jitter, not a change
# anyone would want to look up. Voice and dashboard commands are always logged.
CHANGE_THRESHOLD = 0.02
ALWAYS_LOGGED_SOURCES = {"voice", "voice_typed", "dashboard"}

# Same guard as the rolling window: a gap longer than this between readings is a
# stall or a dropout, and integrating across it would invent energy.
MAX_ENERGY_GAP_SECONDS = 10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,
    firmware        TEXT,
    connected_at    REAL NOT NULL,
    disconnected_at REAL,
    end_reason      TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    t_start         REAL NOT NULL,
    t_end           REAL NOT NULL,
    readings        INTEGER NOT NULL,
    unavailable     INTEGER NOT NULL,
    indoor_lux_avg  REAL,
    indoor_lux_min  REAL,
    indoor_lux_max  REAL,
    outdoor_lux_avg REAL,
    outdoor_lux_min REAL,
    outdoor_lux_max REAL,
    brightness_avg  REAL,
    brightness_last REAL,
    mode            TEXT,
    mode_name       TEXT,
    actual_wh       REAL NOT NULL,
    full_wh         REAL NOT NULL,
    sensor_error    TEXT
);
CREATE INDEX IF NOT EXISTS samples_t ON samples(t_start);

CREATE TABLE IF NOT EXISTS light_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    t               REAL NOT NULL,
    from_brightness REAL,
    to_brightness   REAL NOT NULL,
    source          TEXT NOT NULL,
    reason          TEXT,
    mode            TEXT,
    mode_name       TEXT,
    transcript      TEXT
);
CREATE INDEX IF NOT EXISTS light_changes_t ON light_changes(t);
"""


def is_simulated(device_id, hello):
    """Whether a `hello` comes from the mock device rather than real hardware."""
    return bool(hello.get("simulated")) or str(device_id).startswith("mock")


class _Bucket:
    """Accumulates the readings that fall in one interval."""

    def __init__(self, index, interval):
        self.index = index
        self.interval = interval
        self.t_start = index * interval
        self.t_end = self.t_start + interval
        self.readings = 0
        self.unavailable = 0
        self.indoor = []
        self.outdoor = []
        self.brightness = []
        self.mode = None
        self.mode_name = None
        self.actual_joules = 0.0
        self.full_joules = 0.0
        self.sensor_error = None

    def row(self, session_id):
        def stats(values):
            if not values:
                return None, None, None
            return sum(values) / len(values), min(values), max(values)

        indoor = stats(self.indoor)
        outdoor = stats(self.outdoor)
        return (
            session_id, self.t_start, self.t_end, self.readings, self.unavailable,
            *indoor, *outdoor,
            (sum(self.brightness) / len(self.brightness)) if self.brightness else None,
            self.brightness[-1] if self.brightness else None,
            self.mode, self.mode_name,
            self.actual_joules / 3600.0, self.full_joules / 3600.0,
            self.sensor_error,
        )


class HistoryStore:
    def __init__(self, config, path=DB_PATH):
        self._config = config
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db_lock = threading.Lock()
        with self._db_lock:
            # WAL lets the dashboard's reads run while a write is in progress.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._close_orphaned_sessions()
            self._db.commit()

        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history")

        # Live recording state — touched only from the event loop.
        self.session_id = None
        self._bucket = None
        self._last_reading_at = None
        self._last_logged_brightness = None

    # --- disk ---------------------------------------------------------------

    def _close_orphaned_sessions(self):
        """Close sessions left open by a server that died without shutting down.

        The end time is the last moment we have evidence the device was there, not
        the time of this restart — the hours in between were not a connection.
        """
        self._db.execute("""
            UPDATE sessions SET
              disconnected_at = MAX(connected_at,
                COALESCE((SELECT MAX(t_end) FROM samples
                          WHERE session_id = sessions.id), 0),
                COALESCE((SELECT MAX(t) FROM light_changes
                          WHERE session_id = sessions.id), 0)),
              end_reason = 'unclean_shutdown'
            WHERE disconnected_at IS NULL
        """)

    def _execute(self, sql, params=()):
        with self._db_lock:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            return cursor.lastrowid

    def _submit(self, sql, params):
        """Queue a write without waiting for it. Failures are logged, not raised."""
        future = self._writer.submit(self._execute, sql, params)
        future.add_done_callback(_log_write_failure)

    def _query(self, sql, params=()):
        with self._db_lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    # --- session lifecycle --------------------------------------------------

    @property
    def recording(self):
        return self.session_id is not None

    async def start_session(self, device_id, firmware):
        if self.recording:
            self.end_session("superseded")
        now = time.time()
        loop = asyncio.get_running_loop()
        self.session_id = await loop.run_in_executor(
            self._writer, self._execute,
            "INSERT INTO sessions (device_id, firmware, connected_at) VALUES (?, ?, ?)",
            (device_id, firmware, now))
        self._bucket = None
        self._last_reading_at = None
        self._last_logged_brightness = None
        print(f"[history] recording session {self.session_id} ({device_id})")

    def end_session(self, reason="disconnect"):
        # Queued rather than awaited: this runs in socket cleanup, where the task may
        # already be cancelled, and an awaited write would then never happen.
        if not self.recording:
            return
        self._flush_bucket()
        session_id, self.session_id = self.session_id, None
        self._submit(
            "UPDATE sessions SET disconnected_at = ?, end_reason = ? WHERE id = ?",
            (time.time(), reason, session_id))
        print(f"[history] closed session {session_id} ({reason})")

    def close(self):
        self._writer.shutdown(wait=True)
        with self._db_lock:
            self._db.close()

    # --- recording ----------------------------------------------------------

    def _interval(self):
        try:
            return max(1.0, float(self._config.get("history_interval_seconds", 5)))
        except (TypeError, ValueError):
            return 5.0

    def _bucket_for(self, now):
        """The open bucket for `now`, closing the previous one if it has ended."""
        interval = self._interval()
        index = math.floor(now / interval)
        if self._bucket is not None and (self._bucket.index != index
                                         or self._bucket.interval != interval):
            self._flush_bucket()
        if self._bucket is None:
            self._bucket = _Bucket(index, interval)
        return self._bucket

    def _flush_bucket(self):
        bucket, self._bucket = self._bucket, None
        if bucket is None or not self.recording:
            return
        if bucket.readings == 0 and bucket.unavailable == 0:
            return
        self._submit(
            "INSERT INTO samples (session_id, t_start, t_end, readings, unavailable,"
            " indoor_lux_avg, indoor_lux_min, indoor_lux_max,"
            " outdoor_lux_avg, outdoor_lux_min, outdoor_lux_max,"
            " brightness_avg, brightness_last, mode, mode_name,"
            " actual_wh, full_wh, sensor_error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            bucket.row(self.session_id))

    def on_reading(self, indoor_lux, outdoor_lux, brightness, mode, mode_name):
        if not self.recording:
            return
        now = time.time()
        bucket = self._bucket_for(now)
        bucket.readings += 1
        bucket.indoor.append(float(indoor_lux))
        bucket.outdoor.append(float(outdoor_lux))
        bucket.brightness.append(float(brightness))
        bucket.mode = mode
        bucket.mode_name = mode_name

        # Energy over the gap since the previous reading, credited to this bucket.
        if self._last_reading_at is not None:
            dt = now - self._last_reading_at
            if 0 < dt <= MAX_ENERGY_GAP_SECONDS:
                rated = float(self._config.get("panel_rated_watts", 18.0))
                bucket.actual_joules += rated * float(brightness) * dt
                bucket.full_joules += rated * dt
        self._last_reading_at = now

    def on_unavailable(self, reason):
        if not self.recording:
            return
        bucket = self._bucket_for(time.time())
        bucket.unavailable += 1
        bucket.sensor_error = reason
        # No reading means no basis for integrating energy across the gap.
        self._last_reading_at = None

    def on_command(self, brightness, source, reason, mode, mode_name, transcript=None):
        """Log a brightness command if it is a change worth keeping."""
        if not self.recording:
            return
        brightness = float(brightness)
        previous = self._last_logged_brightness
        if (source not in ALWAYS_LOGGED_SOURCES and previous is not None
                and abs(brightness - previous) < CHANGE_THRESHOLD):
            return
        self._last_logged_brightness = brightness
        self._submit(
            "INSERT INTO light_changes (session_id, t, from_brightness, to_brightness,"
            " source, reason, mode, mode_name, transcript)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.session_id, time.time(), previous, brightness, source, reason,
             mode, mode_name, transcript))

    # --- queries (called from FastAPI's threadpool) --------------------------

    def sessions(self, start=None, end=None, limit=200):
        start, end = _range(start, end)
        return self._query(
            "SELECT * FROM sessions WHERE connected_at <= ?"
            " AND COALESCE(disconnected_at, ?) >= ?"
            " ORDER BY connected_at DESC LIMIT ?",
            (end, time.time(), start, int(limit)))

    def samples(self, start=None, end=None, max_points=1500):
        """Samples in [start, end], downsampled server-side if there are too many.

        A week at 5 s is ~120k rows; the chart only needs about one point per pixel.
        Downsampled rows are readings-weighted averages, so a bucket with one reading
        does not count as much as one with five.
        """
        start, end = _range(start, end)
        interval = self._interval()
        step = interval
        extent = self._query("SELECT COUNT(*) AS n, MIN(t_start) AS lo, MAX(t_start)"
                             " AS hi FROM samples WHERE t_start >= ? AND t_start <= ?",
                             (start, end))[0]
        if max_points and extent["n"] > max_points:
            # Size the step from the data actually present, not the requested range —
            # an open-ended "all time" query would otherwise be measured from 1970.
            span = max(interval, extent["hi"] - extent["lo"])
            step = math.ceil(span / max_points / interval) * interval

        if step <= interval:
            rows = self._query(
                "SELECT * FROM samples WHERE t_start >= ? AND t_start <= ?"
                " ORDER BY t_start", (start, end))
            return {"step_seconds": interval, "downsampled": False, "points": rows}

        rows = self._query(f"""
            SELECT
              CAST(t_start / {step} AS INTEGER) * {step} AS t_start,
              MAX(t_end) AS t_end,
              SUM(readings) AS readings,
              SUM(unavailable) AS unavailable,
              SUM(indoor_lux_avg * readings) / NULLIF(SUM(CASE WHEN indoor_lux_avg
                IS NOT NULL THEN readings END), 0) AS indoor_lux_avg,
              MIN(indoor_lux_min) AS indoor_lux_min,
              MAX(indoor_lux_max) AS indoor_lux_max,
              SUM(outdoor_lux_avg * readings) / NULLIF(SUM(CASE WHEN outdoor_lux_avg
                IS NOT NULL THEN readings END), 0) AS outdoor_lux_avg,
              MIN(outdoor_lux_min) AS outdoor_lux_min,
              MAX(outdoor_lux_max) AS outdoor_lux_max,
              SUM(brightness_avg * readings) / NULLIF(SUM(CASE WHEN brightness_avg
                IS NOT NULL THEN readings END), 0) AS brightness_avg,
              SUM(actual_wh) AS actual_wh,
              SUM(full_wh) AS full_wh
            FROM samples
            WHERE t_start >= ? AND t_start <= ?
            GROUP BY CAST(t_start / {step} AS INTEGER)
            ORDER BY t_start
        """, (start, end))
        return {"step_seconds": step, "downsampled": True, "points": rows}

    def changes(self, start=None, end=None, limit=500):
        start, end = _range(start, end)
        return self._query(
            "SELECT * FROM light_changes WHERE t >= ? AND t <= ?"
            " ORDER BY t DESC LIMIT ?", (start, end, int(limit)))

    def summary(self, start=None, end=None):
        start, end = _range(start, end)
        totals = self._query(
            "SELECT COUNT(*) AS buckets, COALESCE(SUM(readings), 0) AS readings,"
            " COALESCE(SUM(unavailable), 0) AS unavailable,"
            " COALESCE(SUM(actual_wh), 0) AS actual_wh,"
            " COALESCE(SUM(full_wh), 0) AS full_wh,"
            " AVG(indoor_lux_avg) AS indoor_lux_avg,"
            " AVG(outdoor_lux_avg) AS outdoor_lux_avg,"
            " MIN(t_start) AS first_t, MAX(t_end) AS last_t"
            " FROM samples WHERE t_start >= ? AND t_start <= ?", (start, end))[0]
        by_source = self._query(
            "SELECT source, COUNT(*) AS n FROM light_changes"
            " WHERE t >= ? AND t <= ? GROUP BY source", (start, end))
        # Connected time is each session's overlap with the requested range.
        connected = self._query(
            "SELECT COALESCE(SUM(MIN(COALESCE(disconnected_at, ?), ?)"
            " - MAX(connected_at, ?)), 0) AS seconds FROM sessions"
            " WHERE connected_at <= ? AND COALESCE(disconnected_at, ?) >= ?",
            (time.time(), end, start, end, time.time(), start))[0]["seconds"]

        full = totals["full_wh"]
        return {
            **totals,
            "saved_wh": full - totals["actual_wh"],
            "savings_percent": ((1 - totals["actual_wh"] / full) * 100) if full > 0
                               else None,
            "changes": sum(row["n"] for row in by_source),
            "changes_by_source": {row["source"]: row["n"] for row in by_source},
            "connected_seconds": max(0.0, connected),
            "recording": self.recording,
        }

    def export_rows(self, table, start=None, end=None):
        """Every row of `samples` or `light_changes` in the range, oldest first."""
        start, end = _range(start, end)
        column = {"samples": "t_start", "light_changes": "t"}[table]
        with self._db_lock:
            cursor = self._db.execute(
                f"SELECT * FROM {table} WHERE {column} >= ? AND {column} <= ?"
                f" ORDER BY {column}", (start, end))
            header = [d[0] for d in cursor.description]
            return header, cursor.fetchall()


def _range(start, end):
    return (float(start) if start is not None else 0.0,
            float(end) if end is not None else time.time() + 60)


def _log_write_failure(future):
    if (exc := future.exception()) is not None:
        print(f"[history] write failed: {exc}")
