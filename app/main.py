"""WVI web server - the routes.

Requirement map:
  REQ-F-06  GET /api/stream          live air readings pushed to the browser
  REQ-F-07  GET /video/stream.mjpg   live detector feed
            GET /api/targets         snapshot of each target found
  REQ-F-08  runs as a server, bound to 0.0.0.0, with SQLite persistence
            GET /api/history         scroll back through the flight
  REQ-M-19  GET /api/latency         measured end-to-end timing evidence
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import db
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


def ingest(msg: Envelope) -> dict:
    """The single door every producer comes through - simulator, HTTP, or a
    future ROS2 adapter. Keeping this one function is what makes the source
    of data swappable without touching anything else."""
    validate_payload(msg)                 # reject malformed data loudly
    event = hub.publish(msg, utcnow())    # fan out to browsers
    db.write_event(event)                 # REQ-F-08 persistence
    return event


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sim
    db.init()
    if USE_SIM:
        _sim = Simulator(ingest, rate_hz=1.0)
        _sim.start()
    yield
    if _sim:
        await _sim.stop()
    db.close()


app = FastAPI(title="UAVPayloadTAQ-26 - Web Visualisation (WVI)",
              version="0.1.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


# ----------------------------------------------------------------- REQ-F-06
@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events: one connection the server pushes down whenever data
    exists. Chosen over WebSockets because the traffic only ever flows one way,
    and the browser reconnects on its own if the link drops."""
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
    return {"accepted": True, "seq": event["seq"],
            "ingest_latency_ms": event["ingest_latency_ms"]}


# ----------------------------------------------------------------- REQ-F-07
@app.get("/video/stream.mjpg")
async def video():
    return StreamingResponse(
        mjpeg_stream(_camera),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")


@app.get("/api/targets")
async def targets(limit: int = Query(60, ge=1, le=200)):
    return {"targets": list(hub.targets)[:limit]}


# ----------------------------------------------------------------- REQ-F-08
@app.get("/api/history")
async def history(kind: str = Query("readings", pattern="^(readings|detections)$"),
                  since: Optional[str] = None, until: Optional[str] = None,
                  limit: int = Query(500, ge=1, le=5000)):
    return {"kind": kind, "rows": db.history(kind, since, until, limit)}


# ----------------------------------------------------------------- REQ-M-19
@app.get("/api/latency")
async def latency():
    """Server-side latency evidence. Free of clock skew because producer and
    server share a machine on the Pi."""
    return db.latency_stats()


@app.get("/healthz")
async def healthz():
    return JSONResponse(hub.health())


app.mount("/static", StaticFiles(directory=STATIC), name="static")
