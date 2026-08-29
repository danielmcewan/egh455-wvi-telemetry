"""Timestamped persistence - REQ-F-08.

SQLite because it is one file, needs no server process, and survives a Pi
losing power mid-flight. Every row carries BOTH timestamps so you can query
after the fact how long ingest actually took.

This module also answers the two evidence questions the requirements ask:
`latency_stats()` for REQ-M-19 (4 second ceiling) and `mission_stats()` for
REQ-M-15 (ten minutes of logged operation). Both are computed from the stored
rows rather than from counters in memory, so a server restart does not erase
the proof.
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "telemetry.db"

BUDGET_MS = 4000            # REQ-M-19: capture to available, in milliseconds
REQUIRED_LOG_SECONDS = 600  # REQ-M-15: ten minutes of logged operation
DRILL_THRESHOLD_BAR = 2.0   # REQ-F-09

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    seq               INTEGER NOT NULL,
    source            TEXT    NOT NULL,
    t_capture         TEXT    NOT NULL,
    t_ingest          TEXT    NOT NULL,
    ingest_latency_ms REAL    NOT NULL,
    temperature_c     REAL, humidity_pct      REAL,
    pressure_hpa      REAL, light_lux         REAL,
    gas_oxidising_ohm REAL, gas_reducing_ohm  REAL,
    gas_nh3_ohm       REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_capture ON readings(t_capture);
CREATE INDEX IF NOT EXISTS idx_readings_latency ON readings(ingest_latency_ms);

CREATE TABLE IF NOT EXISTS detections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    seq               INTEGER NOT NULL,
    source            TEXT    NOT NULL,
    t_capture         TEXT    NOT NULL,
    t_ingest          TEXT    NOT NULL,
    ingest_latency_ms REAL    NOT NULL,
    class             TEXT    NOT NULL,
    confidence        REAL    NOT NULL,
    bbox              TEXT    NOT NULL,
    aruco_id          INTEGER,
    gauge_value_bar   REAL,
    -- HLO-M-3 requires localisation coordinates to be stored with a timestamp,
    -- not just shown live. Metres, relative to the ArUco marker.
    pose_x_m          REAL, pose_y_m REAL, pose_z_m REAL,
    image_ref         TEXT
);
CREATE INDEX IF NOT EXISTS idx_detections_capture ON detections(t_capture);
CREATE INDEX IF NOT EXISTS idx_detections_class   ON detections(class);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init() -> None:
    global _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    # WAL keeps reads (the history page) from blocking writes (incoming telemetry).
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(_SCHEMA)
    _migrate()
    _conn.commit()


def _migrate() -> None:
    """Add columns that a database created by an earlier version is missing.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    the localisation columns would never appear on a log that predates them -
    and the existing log is the evidence for REQ-M-15 and REQ-M-19, so dropping
    it to pick up a schema change is not an option.
    """
    have = {r["name"] for r in _conn.execute("PRAGMA table_info(detections)")}
    for col in ("pose_x_m", "pose_y_m", "pose_z_m"):
        if col not in have:
            _conn.execute(f"ALTER TABLE detections ADD COLUMN {col} REAL")


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def write_event(event: Dict[str, Any]) -> None:
    """Writes are ~1/s, so a lock is plenty. Do not over-engineer this."""
    if _conn is None:
        return
    d = event["data"]
    with _lock:
        if event["type"] == "air_reading":
            _conn.execute(
                """INSERT INTO readings (seq, source, t_capture, t_ingest, ingest_latency_ms,
                       temperature_c, humidity_pct, pressure_hpa, light_lux,
                       gas_oxidising_ohm, gas_reducing_ohm, gas_nh3_ohm)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event["seq"], event["source"], event["t_capture"], event["t_ingest"],
                 event["ingest_latency_ms"], d.get("temperature_c"), d.get("humidity_pct"),
                 d.get("pressure_hpa"), d.get("light_lux"), d.get("gas_oxidising_ohm"),
                 d.get("gas_reducing_ohm"), d.get("gas_nh3_ohm")),
            )
        else:
            _conn.execute(
                """INSERT INTO detections (seq, source, t_capture, t_ingest, ingest_latency_ms,
                       class, confidence, bbox, aruco_id, gauge_value_bar,
                       pose_x_m, pose_y_m, pose_z_m, image_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event["seq"], event["source"], event["t_capture"], event["t_ingest"],
                 event["ingest_latency_ms"], d.get("class") or d.get("cls"),
                 d.get("confidence"), json.dumps(d.get("bbox")), d.get("aruco_id"),
                 d.get("gauge_value_bar"), d.get("pose_x_m"), d.get("pose_y_m"),
                 d.get("pose_z_m"), d.get("image_ref")),
            )
        _conn.commit()


# --------------------------------------------------------------- querying
# Column names get pasted straight into SQL, so they are whitelisted rather
# than derived from whatever the caller sent. A search box is an input from
# outside the system, and inputs from outside the system get a whitelist.
_SEARCHABLE = {
    "readings": ["t_capture", "source", "seq"],
    "detections": ["t_capture", "source", "seq", "class", "aruco_id", "image_ref"],
}

_SORTABLE = {
    "readings": {"t_capture", "seq", "source", "ingest_latency_ms", "temperature_c",
                 "humidity_pct", "pressure_hpa", "light_lux", "gas_oxidising_ohm",
                 "gas_reducing_ohm", "gas_nh3_ohm"},
    "detections": {"t_capture", "seq", "source", "ingest_latency_ms", "class",
                   "confidence", "gauge_value_bar", "aruco_id",
                   "pose_x_m", "pose_y_m", "pose_z_m"},
}


def _table(kind: str) -> str:
    return "readings" if kind == "readings" else "detections"


def _filters(kind: str, q: Optional[str], since: Optional[str], until: Optional[str],
             source: Optional[str], cls: Optional[str],
             min_confidence: Optional[float]) -> Tuple[str, List[Any]]:
    """Build the WHERE clause shared by the table, its row count and the CSV.

    One function for all three so the count under the table can never disagree
    with the rows in it, or with what a download actually contains.
    """
    table = _table(kind)
    sql, params = " WHERE 1=1", []
    if since:
        sql += " AND t_capture >= ?"; params.append(since)
    if until:
        sql += " AND t_capture <= ?"; params.append(until)
    if source:
        sql += " AND source = ?"; params.append(source)
    if cls and table == "detections":
        sql += " AND class = ?"; params.append(cls)
    if min_confidence is not None and table == "detections":
        sql += " AND confidence >= ?"; params.append(min_confidence)
    if q:
        # Match against any searchable column. CAST because seq and aruco_id
        # are integers and people still type digits at a search box.
        cols = _SEARCHABLE[table]
        sql += " AND (" + " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in cols) + ")"
        params.extend([f"%{q}%"] * len(cols))
    return sql, params


def history(kind: str = "readings", since: Optional[str] = None,
            until: Optional[str] = None, limit: int = 500,
            q: Optional[str] = None, source: Optional[str] = None,
            cls: Optional[str] = None, min_confidence: Optional[float] = None,
            offset: int = 0, sort: str = "t_capture",
            order: str = "desc") -> Dict[str, Any]:
    """Scroll back through the flight - the other half of REQ-F-08.

    Returns the page of rows *and* the total the filter matched, so the page
    can honestly say "50 of 3,214" rather than implying the log ends wherever
    the fetch happened to stop.
    """
    if _conn is None:
        return {"rows": [], "total": 0, "offset": 0, "limit": limit}
    table = _table(kind)
    where, params = _filters(kind, q, since, until, source, cls, min_confidence)

    sort = sort if sort in _SORTABLE[table] else "t_capture"
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    limit = max(1, min(limit, 5000))
    offset = max(0, offset)

    with _lock:
        total = _conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0]
        rows = _conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY {sort} {direction}, id {direction}"
            f" LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "offset": offset,
            "limit": limit, "sort": sort, "order": direction.lower()}


def iter_rows(kind: str, q: Optional[str] = None, since: Optional[str] = None,
              until: Optional[str] = None, source: Optional[str] = None,
              cls: Optional[str] = None, min_confidence: Optional[float] = None,
              limit: int = 100000) -> List[Dict[str, Any]]:
    """Every matching row, oldest first - the CSV export path.

    Oldest first because an exported log gets read forwards, unlike the
    on-screen table which shows the newest thing at the top.
    """
    if _conn is None:
        return []
    table = _table(kind)
    where, params = _filters(kind, q, since, until, source, cls, min_confidence)
    with _lock:
        rows = _conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY t_capture ASC, id ASC LIMIT ?",
            [*params, max(1, min(limit, 200000))]).fetchall()
    return [dict(r) for r in rows]


def recent_detections(limit: int = 30) -> List[Dict[str, Any]]:
    """The newest detections, shaped exactly like a live hub event.

    Used to refill the gallery at startup. Without this, restarting the server
    mid-flight leaves the target panel empty until the next detection arrives -
    which reads as "the dashboard lost the targets" when in fact every one of
    them is still in the log. REQ-F-07 asks for the images to be displayed, and
    a restart is not an excuse to stop displaying them.
    """
    if _conn is None:
        return []
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM detections ORDER BY t_capture DESC, id DESC LIMIT ?",
            (max(1, min(limit, 200)),)).fetchall()
    out = []
    for r in rows:
        try:
            bbox = json.loads(r["bbox"])
        except (TypeError, ValueError):
            bbox = None
        out.append({
            "type": "detection", "seq": r["seq"], "source": r["source"],
            "t_capture": r["t_capture"], "t_ingest": r["t_ingest"],
            "ingest_latency_ms": r["ingest_latency_ms"],
            "data": {"class": r["class"], "confidence": r["confidence"],
                     "bbox": bbox, "aruco_id": r["aruco_id"],
                     "gauge_value_bar": r["gauge_value_bar"],
                     "pose_x_m": r["pose_x_m"], "pose_y_m": r["pose_y_m"],
                     "pose_z_m": r["pose_z_m"],
                     "image_ref": r["image_ref"]},
        })
    return out


def distinct_values() -> Dict[str, Any]:
    """What the filter dropdowns should offer. Driven by data actually logged,
    so a source nobody ever used never appears as a choice."""
    if _conn is None:
        return {"sources": [], "classes": []}
    with _lock:
        srcs = {r[0] for r in _conn.execute("SELECT DISTINCT source FROM readings")}
        srcs |= {r[0] for r in _conn.execute("SELECT DISTINCT source FROM detections")}
        classes = [r[0] for r in _conn.execute(
            "SELECT DISTINCT class FROM detections ORDER BY class")]
    return {"sources": sorted(s for s in srcs if s), "classes": classes}


# --------------------------------------------------------------- evidence
def _percentile(sorted_vals: List[float], pct: float) -> float:
    """Nearest-rank percentile. Exact, no interpolation, and no numpy - which
    is a dependency worth avoiding on a Raspberry Pi."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1,
                   int(round(pct / 100.0 * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


def latency_stats() -> Dict[str, Any]:
    """Evidence for REQ-M-19, computed over everything logged so far.

    An average is weak evidence for a *ceiling*: a mean of 12 ms says nothing
    about whether one reading in a thousand took six seconds. The percentiles
    and the worst case are what actually answer "within 4 seconds of capture".
    """
    if _conn is None:
        return {}
    with _lock:
        row = _conn.execute(
            """SELECT COUNT(*) n, AVG(ingest_latency_ms) avg_ms,
                      MIN(ingest_latency_ms) min_ms, MAX(ingest_latency_ms) max_ms,
                      SUM(CASE WHEN ingest_latency_ms > ? THEN 1 ELSE 0 END) over_budget
               FROM readings""", (BUDGET_MS,)).fetchone()
        vals = [r[0] for r in _conn.execute(
            "SELECT ingest_latency_ms FROM readings ORDER BY ingest_latency_ms")]
    d = dict(row) if row else {}
    d["budget_ms"] = BUDGET_MS
    if d.get("n"):
        d["avg_ms"] = round(d["avg_ms"], 1)
        d["p50_ms"] = round(_percentile(vals, 50), 1)
        d["p95_ms"] = round(_percentile(vals, 95), 1)
        d["p99_ms"] = round(_percentile(vals, 99), 1)
        d["worst_pct_of_budget"] = round(100.0 * d["max_ms"] / BUDGET_MS, 2)
        d["pass"] = d["over_budget"] == 0
    else:
        d["pass"] = None
    return d


def _span_seconds(first: Optional[str], last: Optional[str]) -> Optional[float]:
    """Seconds between two stored ISO-8601 timestamps, or None."""
    if not first or not last:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(last)
                         - datetime.fromisoformat(first)).total_seconds())
    except ValueError:
        return None


# A run is considered broken if the log goes quiet for longer than this. The
# producers publish at 1 Hz, so thirty seconds is thirty missed readings - far
# past a hiccup and firmly into "the payload stopped".
SESSION_GAP_S = 30.0

_LONGEST_SESSION_SQL = """
WITH ordered AS (
  SELECT julianday(t_capture) jd,
         LAG(julianday(t_capture)) OVER (ORDER BY t_capture, id) prev_jd
  FROM readings
),
marked AS (
  SELECT jd, CASE WHEN prev_jd IS NULL OR (jd - prev_jd) * 86400.0 > ?
                  THEN 1 ELSE 0 END AS boundary
  FROM ordered
),
sessions AS (
  SELECT jd, SUM(boundary) OVER (ORDER BY jd ROWS UNBOUNDED PRECEDING) sess
  FROM marked
)
SELECT COUNT(*) n, (MAX(jd) - MIN(jd)) * 86400.0 secs,
       strftime('%Y-%m-%dT%H:%M:%SZ', MIN(jd)) first_t,
       strftime('%Y-%m-%dT%H:%M:%SZ', MAX(jd)) last_t
FROM sessions GROUP BY sess ORDER BY secs DESC LIMIT 1
"""


def longest_session() -> Dict[str, Any]:
    """The longest unbroken run in the log - the honest answer to REQ-M-15.

    First-record-to-last-record would be the easy number, and it would be
    wrong: a database holding a fortnight of test runs would report a
    two-week "operation" made of ten-minute sessions and long silences. What
    the requirement asks for is a continuous period, so gaps split the record
    into runs and the longest one is what gets reported.
    """
    if _conn is None:
        return {}
    with _lock:
        row = _conn.execute(_LONGEST_SESSION_SQL, (SESSION_GAP_S,)).fetchone()
    if row is None or not row["n"]:
        return {"seconds": None, "readings": 0, "first": None, "last": None}
    return {"seconds": round(row["secs"], 1), "readings": row["n"],
            "first": row["first_t"], "last": row["last_t"],
            "gap_threshold_s": SESSION_GAP_S}


def mission_stats() -> Dict[str, Any]:
    """Evidence for REQ-M-15 - "logged functioning operation for a minimal
    period of 10 minutes prior to the acceptance test".

    That requirement is about the log, not about uptime, so it is answered
    from stored rows: a server restarted halfway through still has to be able
    to show a continuous logged record.
    """
    if _conn is None:
        return {}
    with _lock:
        r = _conn.execute("""SELECT COUNT(*) n, MIN(t_capture) first_t,
                                    MAX(t_capture) last_t FROM readings""").fetchone()
        d = _conn.execute("""SELECT COUNT(*) n, MIN(t_capture) first_t,
                                    MAX(t_capture) last_t FROM detections""").fetchone()
        per_class = [dict(x) for x in _conn.execute(
            """SELECT class, COUNT(*) n, MAX(confidence) best_confidence
               FROM detections GROUP BY class ORDER BY n DESC""")]
        # REQ-F-09: has the gauge ever been read below the 2 bar drill threshold?
        drill = _conn.execute(
            """SELECT COUNT(*) n, MIN(gauge_value_bar) lowest FROM detections
               WHERE gauge_value_bar IS NOT NULL AND gauge_value_bar < ?""",
            (DRILL_THRESHOLD_BAR,)).fetchone()

    session = longest_session()
    secs = session.get("seconds")
    return {
        "required_minutes": REQUIRED_LOG_SECONDS / 60,
        "readings": {"count": r["n"], "first": r["first_t"], "last": r["last_t"]},
        "detections": {"count": d["n"], "first": d["first_t"], "last": d["last_t"]},
        "per_class": per_class,
        "drill_condition": {"threshold_bar": DRILL_THRESHOLD_BAR,
                            "readings_below": drill["n"], "lowest_bar": drill["lowest"]},
        # The requirement is about one continuous run, so that is what "logged"
        # means here. The whole-database span is reported separately and
        # clearly labelled, because the two get confused otherwise.
        "longest_session": session,
        "logged_seconds": secs,
        "logged_minutes": round(secs / 60.0, 2) if secs is not None else None,
        "record_span_seconds": _span_seconds(r["first_t"], r["last_t"]),
        "pass": bool(secs is not None and secs >= REQUIRED_LOG_SECONDS),
    }
