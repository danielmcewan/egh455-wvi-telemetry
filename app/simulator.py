"""Fake producer - stands in for the AQ and IP subsystems until they exist.

This is an "ingest adapter": it pushes data in through exactly the same door the
real subsystems will use. That is what lets you finish and demo the entire
dashboard before anyone else's hardware works.

The numbers wander realistically so the charts look alive, and a detection fires
every few seconds so the target gallery fills up.
"""
import asyncio
import math
import random
import time
from typing import Callable

from .models import Envelope, utcnow

DETECTION_EVERY_S = 6.0
CLASSES = ["valve_open", "valve_closed", "gauge", "aruco"]


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
        return Envelope(
            type="detection",
            seq=self._det_seq,
            t_capture=utcnow(),
            source="SIM",
            data={
                "class": cls,
                "confidence": round(random.uniform(0.72, 0.98), 2),
                "bbox": [random.randint(20, 400), random.randint(20, 300), 96, 96],
                "aruco_id": random.randint(0, 12) if cls == "aruco" else None,
                # Deliberately straddles the 2 bar REQ-F-09 threshold so you can
                # watch the drill-trigger condition flip during testing.
                "gauge_value_bar": round(random.uniform(0.8, 3.4), 2) if cls == "gauge" else None,
                "image_ref": f"targets/det_{self._det_seq:04d}.jpg",
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
