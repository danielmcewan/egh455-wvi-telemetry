"""Fake producer - stands in for the AQ and IP subsystems until they exist.

This is an "ingest adapter": it pushes data in through exactly the same door the
real subsystems will use. That is what lets you finish and demo the entire
dashboard before anyone else's hardware works.

The numbers wander realistically so the charts look alive, and a detection fires
every few seconds so the target gallery fills up.
"""
import asyncio
import io
import math
import random
import time
from typing import Callable, Optional

from PIL import Image, ImageDraw

from . import images
from .models import Envelope, utcnow

DETECTION_EVERY_S = 6.0
CLASSES = ["valve_open", "valve_closed", "gauge", "aruco"]

SNAPSHOT_W, SNAPSHOT_H = 224, 224
CLASS_COLOURS = {
    "valve_open":   ((214, 235, 219), (26, 127, 55)),
    "valve_closed": ((247, 222, 219), (180, 35, 24)),
    "gauge":        ((252, 243, 219), (154, 103, 0)),
    "aruco":        ((222, 228, 240), (31, 56, 100)),
}


def _snapshot(cls: str, seq: int, label: str) -> Optional[str]:
    """Write a stand-in target snapshot and return its stored name.

    REQ-F-07 asks for the images of the targets, so the gallery needs pictures
    to show before the IP subsystem exists. This writes a real JPEG into the
    same directory real snapshots land in, by the same route IP would use when
    it runs on the same Pi - so when their images arrive, nothing on this side
    changes except that the pictures stop being drawings.
    """
    bg, fg = CLASS_COLOURS.get(cls, ((240, 240, 240), (60, 60, 60)))
    img = Image.new("RGB", (SNAPSHOT_W, SNAPSHOT_H), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, SNAPSHOT_W - 9, SNAPSHOT_H - 9], outline=fg, width=2)

    if cls == "aruco":
        # A blocky marker-ish pattern, seeded so one ArUco id always looks the
        # same twice - a gallery where every card is different noise is harder
        # to sanity-check than one where repeats are recognisable.
        rng = random.Random(seq)
        cell = 24
        for gx in range(4):
            for gy in range(4):
                if rng.random() < 0.5:
                    x, y = 56 + gx * cell, 56 + gy * cell
                    d.rectangle([x, y, x + cell - 2, y + cell - 2], fill=fg)
    elif cls == "gauge":
        d.ellipse([52, 44, 172, 164], outline=fg, width=3)
        ang = math.radians(220 + (seq * 37) % 200)
        d.line([112, 104, 112 + 46 * math.cos(ang), 104 + 46 * math.sin(ang)],
               fill=fg, width=3)
    else:
        closed = cls == "valve_closed"
        d.ellipse([64, 56, 160, 152], outline=fg, width=3)
        d.line([112, 56, 112, 152] if closed else [64, 104, 160, 104],
               fill=fg, width=5)

    d.text((14, SNAPSHOT_H - 24), label, fill=fg)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    try:
        return images.save_bytes(buf.getvalue(), f"det_{seq:05d}")
    except (ValueError, OSError):
        # A missing thumbnail is a cosmetic problem; it must never take the
        # telemetry stream down with it.
        return None


class Simulator:
    def __init__(self, ingest: Callable, rate_hz: float = 1.0) -> None:
        self._ingest = ingest
        self._period = 1.0 / rate_hz
        self._air_seq = 0
        self._det_seq = 0
        self._t0 = time.time()
        self._task: asyncio.Task | None = None

    def _air(self) -> Envelope:
        """Slow sine drift plus a little noise - looks like a real sensor, and
        means you can see immediately if the dashboard has frozen."""
        t = time.time() - self._t0
        self._air_seq += 1
        return Envelope(
            type="air_reading",
            seq=self._air_seq,
            t_capture=utcnow(),
            source="SIM",
            data={
                "temperature_c": round(23.0 + 2.0 * math.sin(t / 40) + random.uniform(-.15, .15), 2),
                "humidity_pct": round(52.0 + 6.0 * math.sin(t / 55 + 1) + random.uniform(-.4, .4), 2),
                "pressure_hpa": round(1013.0 + 1.5 * math.sin(t / 90) + random.uniform(-.1, .1), 2),
                "light_lux": round(max(0.0, 420 + 260 * math.sin(t / 30) + random.uniform(-18, 18)), 1),
                "gas_oxidising_ohm": round(21500 + 2600 * math.sin(t / 47) + random.uniform(-160, 160), 1),
                "gas_reducing_ohm": round(145000 + 16000 * math.sin(t / 61 + 2) + random.uniform(-900, 900), 1),
                "gas_nh3_ohm": round(98000 + 9000 * math.sin(t / 53 + 4) + random.uniform(-600, 600), 1),
            },
        )

    def _detection(self) -> Envelope:
        self._det_seq += 1
        cls = random.choice(CLASSES)
        aruco_id = random.randint(0, 12) if cls == "aruco" else None
        # Deliberately straddles the 2 bar REQ-F-09 threshold so you can watch
        # the drill-trigger condition flip during testing.
        bar = round(random.uniform(0.8, 3.4), 2) if cls == "gauge" else None

        # HLO-M-2: ArUco pose gives the UAV's local position in the camera's
        # frame. Ranges are chosen to look like the real enclosure - a marker
        # 1.5 to 4 m ahead, roughly level, at the 1-3 m flight altitude.
        if cls == "aruco":
            pose = (round(random.uniform(-1.8, 1.8), 2),
                    round(random.uniform(-0.6, 0.6), 2),
                    round(random.uniform(1.5, 4.0), 2))
        else:
            pose = (None, None, None)

        label = cls
        if aruco_id is not None:
            label = f"{cls} #{aruco_id}"
        elif bar is not None:
            label = f"{cls} {bar:.2f} bar"

        return Envelope(
            type="detection",
            seq=self._det_seq,
            t_capture=utcnow(),
            source="SIM",
            data={
                "class": cls,
                "confidence": round(random.uniform(0.72, 0.98), 2),
                "bbox": [random.randint(20, 400), random.randint(20, 300), 96, 96],
                "aruco_id": aruco_id,
                "gauge_value_bar": bar,
                "pose_x_m": pose[0], "pose_y_m": pose[1], "pose_z_m": pose[2],
                "image_ref": _snapshot(cls, self._det_seq, label),
            },
        )

    async def _run(self) -> None:
        last_det = 0.0
        while True:
            self._ingest(self._air())
            now = time.time()
            if now - last_det >= DETECTION_EVERY_S:
                self._ingest(self._detection())
                last_det = now
            await asyncio.sleep(self._period)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
