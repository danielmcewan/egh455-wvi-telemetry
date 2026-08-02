"""Timestamped persistence - REQ-F-08.

SQLite because it is one file, needs no server process, and survives a Pi
losing power mid-flight. Every row carries BOTH timestamps so you can query
after the fact how long ingest actually took.
"""
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "telemetry.db"

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
    image_ref         TEXT
);
CREATE INDEX IF NOT EXISTS idx_detections_capture ON detections(t_capture);
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
    _conn.commit()


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
                       class, confidence, bbox, aruco_id, gauge_value_bar, image_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (event["seq"], event["source"], event["t_capture"], event["t_ingest"],
                 event["ingest_latency_ms"], d.get("class") or d.get("cls"),
                 d.get("confidence"), json.dumps(d.get("bbox")), d.get("aruco_id"),
                 d.get("gauge_value_bar"), d.get("image_ref")),
            )
        _conn.commit()


def history(kind: str = "readings", since: Optional[str] = None,
            until: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
    """Scroll back through the flight - the other half of REQ-F-08."""
    if _conn is None:
        return []
    table = "readings" if kind == "readings" else "detections"
    sql = f"SELECT * FROM {table} WHERE 1=1"
    params: List[Any] = []
    if since:
        sql += " AND t_capture >= ?"; params.append(since)
    if until:
        sql += " AND t_capture <= ?"; params.append(until)
    sql += " ORDER BY t_capture DESC LIMIT ?"
    params.append(max(1, min(limit, 5000)))
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def latency_stats() -> Dict[str, Any]:
    """Evidence for REQ-M-19, computed over everything logged so far."""
    if _conn is None:
        return {}
    with _lock:
        row = _conn.execute(
            """SELECT COUNT(*) n, AVG(ingest_latency_ms) avg_ms,
                      MAX(ingest_latency_ms) max_ms,
                      SUM(CASE WHEN ingest_latency_ms > 4000 THEN 1 ELSE 0 END) over_budget
               FROM readings"""
        ).fetchone()
    d = dict(row) if row else {}
    if d.get("n"):
        d["avg_ms"] = round(d["avg_ms"], 1)
        d["budget_ms"] = 4000
        d["pass"] = d["over_budget"] == 0
    return d
