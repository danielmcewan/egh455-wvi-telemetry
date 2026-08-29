"""WVI web server - the routes.

Requirement map:
  REQ-F-05  GET /api/targets         target type, surfaced as an alert in the UI
  REQ-F-06  GET /api/stream          live air readings pushed to the browser
  REQ-F-07  GET /video/stream.mjpg   live detector feed
            POST /api/targets/image  IP uploads a target snapshot
            GET /api/targets/image/  the snapshots, served back to the gallery
  REQ-F-08  runs as a server, bound to 0.0.0.0, with SQLite persistence
            GET /api/history         search and scroll back through the flight
            GET /api/export.csv      take the log away
  REQ-M-15  GET /api/mission         proof of 10 minutes of logged operation
  REQ-M-19  GET /api/latency         measured end-to-end timing evidence
  HLO-M-5   GET/POST /api/lcd        operator selects the LCD display remotely
"""
import asyncio
import csv
import io
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import db, images
from .camera import build_source, mjpeg_stream, BOUNDARY
from .hub import hub
from .models import Envelope, utcnow, validate_payload
from .simulator import Simulator

STATIC = Path(__file__).resolve().parent.parent / "static"

USE_SIM = os.getenv("WVI_SIMULATOR", "1") not in ("0", "false", "False")
USE_WEBCAM = os.getenv("WVI_WEBCAM", "0") not in ("0", "false", "False")
HEARTBEAT_S = 15.0

_camera = build_source(prefer_webcam=USE_WEBCAM)
_sim: Optional[Simulator] = None


def _attach_image(msg: Envelope, payload) -> None:
    """Reduce however IP delivered the snapshot to one stored file name.

    REQ-F-07 wants the target images on screen, and the IP subsystem has three
    ways to get one to us (see `images.py`). Resolving all three here means the
    browser, the database and the gallery only ever deal with a bare file name
    and never learn which route the picture took.
    """
    data = msg.data
    # Popped, not kept: a base64 image must never be fanned out to every
    # connected browser over SSE, nor stored as a giant string in SQLite.
    b64 = data.pop("image_b64", None)
    ref = payload.image_ref

    if b64:
        data["image_ref"] = images.save_b64(b64, ref or f"det_{msg.seq:05d}")
    elif ref:
        # A path like "targets/det_0088.jpg" is what a producer's own
        # filesystem looks like; we keep the last segment and nothing else.
        data["image_ref"] = images.safe_name(ref)
    else:
        data["image_ref"] = None


def ingest(msg: Envelope) -> dict:
    """The single door every producer comes through - simulator, HTTP, or a
    future ROS2 adapter. Keeping this one function is what makes the source
    of data swappable without touching anything else."""
    payload = validate_payload(msg)        # reject malformed data loudly
    if msg.type == "detection":
        _attach_image(msg, payload)
    event = hub.publish(msg, utcnow())     # fan out to browsers
    db.write_event(event)                  # REQ-F-08 persistence
    return event


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sim
    db.init()
    images.init()
    # Refill the gallery from the log so a restart does not blank the target
    # panel. Oldest first, because the deque prepends.
    for ev in reversed(db.recent_detections(30)):
        hub.targets.appendleft(ev)
    if USE_SIM:
        _sim = Simulator(ingest, rate_hz=1.0)
        _sim.start()
    yield
    if _sim:
        await _sim.stop()
    db.close()


app = FastAPI(title="UAVPayloadTAQ-26 - Web Visualisation (WVI)",
              version="0.2.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


# ----------------------------------------------------------------- REQ-F-06
@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events: one connection the server pushes down whenever data
    exists. Chosen over WebSockets because telemetry only ever flows one way -
    payload to operator - and the browser reconnects on its own if the link
    drops. The one command that travels the other way (the LCD mode, HLO-M-5)
    is an ordinary POST and does not need a bidirectional socket held open."""
    q = hub.subscribe()

    async def gen():
        try:
            # Send current state immediately so a freshly opened page is never blank.
            snapshot = {"type": "snapshot",
                        "latest_air": hub.latest_air,
                        "targets": list(hub.targets)[:12],
                        "health": hub.health()}
            yield f"data: {json.dumps(snapshot)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_S)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # A comment line keeps proxies from closing an idle connection.
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


# ----------------------------------------------------------------- producers
@app.post("/api/ingest")
async def api_ingest(payload: dict):
    """Where the AQ and IP subsystems POST their data. Any language, any
    framework - it is just an HTTP request with JSON in it."""
    try:
        msg = Envelope.model_validate(payload)
        event = ingest(msg)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))
    except ValueError as e:
        # Raised by the image helpers - a bad snapshot should not silently
        # become a detection with no picture attached.
        raise HTTPException(status_code=422, detail=str(e))
    return {"accepted": True, "seq": event["seq"],
            "ingest_latency_ms": event["ingest_latency_ms"],
            "image_ref": event["data"].get("image_ref")}


# ----------------------------------------------------------------- REQ-F-07
@app.get("/video/stream.mjpg")
async def video():
    return StreamingResponse(
        mjpeg_stream(_camera),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")


@app.post("/api/targets/image")
async def upload_target_image(file: UploadFile = File(...),
                              name: Optional[str] = Query(None)):
    """Upload a snapshot on its own, then reference it from a detection.

    This is the route to use when the image is too big to sit comfortably
    inside a JSON message, or when IP wants to send the picture and the
    detection metadata from different parts of their code.

    Returns the stored name - put that in `image_ref` on the detection.
    """
    data = await file.read()
    try:
        stored = images.save_bytes(data, name or file.filename or "target")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"stored": True, "image_ref": stored,
            "url": f"/api/targets/image/{stored}", "bytes": len(data)}


@app.get("/api/targets/image/{name}")
async def target_image(name: str):
    """Serve one stored snapshot. Names are validated, not trusted."""
    p = images.path_for(name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such target image")
    # Images are immutable once written, so let the browser keep them - a
    # gallery of sixty thumbnails should not refetch on every render.
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/targets")
async def targets(limit: int = Query(60, ge=1, le=200),
                  cls: Optional[str] = Query(None, alias="class"),
                  q: Optional[str] = None):
    """The live gallery - most recent detections held in memory.

    Filtering here is deliberately in-memory and cheap; searching the whole
    flight is what /api/history?kind=detections is for.
    """
    out = []
    for ev in hub.targets:
        d = ev.get("data", {})
        name = d.get("class") or d.get("cls") or ""
        if cls and name != cls:
            continue
        if q and q.lower() not in json.dumps(ev).lower():
            continue
        out.append(ev)
        if len(out) >= limit:
            break
    return {"targets": out, "held": len(hub.targets)}


# ----------------------------------------------------------------- REQ-F-08
@app.get("/api/history")
async def history(kind: str = Query("readings", pattern="^(readings|detections)$"),
                  since: Optional[str] = None, until: Optional[str] = None,
                  limit: int = Query(500, ge=1, le=5000),
                  offset: int = Query(0, ge=0),
                  q: Optional[str] = None,
                  source: Optional[str] = None,
                  cls: Optional[str] = Query(None, alias="class"),
                  min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
                  sort: str = "t_capture",
                  order: str = Query("desc", pattern="^(asc|desc)$")):
    """Search and page through the log. `q` matches any text column."""
    result = db.history(kind=kind, since=since, until=until, limit=limit,
                        q=q, source=source, cls=cls, min_confidence=min_confidence,
                        offset=offset, sort=sort, order=order)
    return {"kind": kind, **result}


@app.get("/api/filters")
async def filters():
    """The values the filter dropdowns should offer, taken from what has
    actually been logged rather than from a hardcoded list."""
    return db.distinct_values()


@app.get("/api/export.csv")
async def export_csv(kind: str = Query("readings", pattern="^(readings|detections)$"),
                     since: Optional[str] = None, until: Optional[str] = None,
                     q: Optional[str] = None, source: Optional[str] = None,
                     cls: Optional[str] = Query(None, alias="class"),
                     min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0)):
    """Download the filtered log as CSV.

    REQ-F-08's rationale is that the client wants the sensor data; a table they
    can only look at through a browser is a weaker answer than one they can
    take away and open in Excel or pandas.
    """
    rows = db.iter_rows(kind, q=q, since=since, until=until, source=source,
                        cls=cls, min_confidence=min_confidence)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()),
                           extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    else:
        buf.write("no rows matched the current filter\n")
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="uavpayload-{kind}-{stamp}.csv"'})


# ----------------------------------------------------------------- evidence
@app.get("/api/latency")
async def latency():
    """Server-side latency evidence for REQ-M-19. Free of clock skew because
    producer and server share a machine on the Pi."""
    return db.latency_stats()


@app.get("/api/mission")
async def mission():
    """REQ-M-15 evidence, plus the per-class detection tally REQ-F-05 is about
    and the REQ-F-09 drill-threshold record."""
    stats = db.mission_stats()
    stats["images_stored"] = images.stored_count()
    return stats


# ----------------------------------------------------------------- HLO-M-5
# "The operator must be able to select the desired display, e.g. (IP address,
# live target detection or temperature) using the proximity sensor on the
# Pimoroni Env sensor AND REMOTELY FROM THE GCS WEB INTERFACE."
#
# The LCD itself belongs to the enclosure subsystem. What WVI owes the system
# is somewhere for the operator to express the choice and somewhere for the
# payload to read it back, so this is deliberately a two-line state machine and
# not an attempt to drive the panel from here.
LCD_MODES = ("ip", "detection", "temperature")
_lcd = {"mode": "ip", "set_at": None, "set_by": None}


@app.get("/api/lcd")
async def lcd_state():
    """Polled by the payload-side LCD driver. Returns the requested mode."""
    return {**_lcd, "modes": list(LCD_MODES)}


@app.post("/api/lcd")
async def set_lcd(mode: str = Query(..., pattern="^(ip|detection|temperature)$"),
                  by: str = Query("web")):
    """Operator selects what the LCD shows, from the web interface."""
    _lcd.update(mode=mode, set_at=utcnow().isoformat(), set_by=by)
    return {"accepted": True, **_lcd}


@app.get("/healthz")
async def healthz():
    return JSONResponse(hub.health())


app.mount("/static", StaticFiles(directory=STATIC), name="static")
