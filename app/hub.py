"""In-memory fan-out hub - the seam that makes this subsystem swappable.

Producers (the simulator, the HTTP ingest endpoint, a future ROS2 adapter) all
call `publish()`. Consumers (each connected browser) get their own queue.

Nothing in here knows how a gas sensor works. That is the whole point:
if the AQ lead swaps their sensor, this file does not change.
"""
import asyncio
from collections import deque
from datetime import timedelta
from typing import Any, Deque, Dict, List, Optional

from .models import Envelope, utcnow

# A slow browser must never block the producer, so each subscriber queue is
# bounded and we drop the oldest frame rather than stalling the whole server.
QUEUE_MAX = 100
TARGET_GALLERY_MAX = 60
STALE_AFTER_S = 5.0


class Hub:
    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []
        self.latest_air: Optional[Dict[str, Any]] = None
        self.targets: Deque[Dict[str, Any]] = deque(maxlen=TARGET_GALLERY_MAX)
        self.last_seen: Dict[str, Any] = {}
        self.seq_seen: Dict[str, int] = {}
        self.dropped: Dict[str, int] = {}

    # ---------------------------------------------------------------- consumers
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ---------------------------------------------------------------- producers
    def publish(self, msg: Envelope, t_ingest) -> Dict[str, Any]:
        """Record the message and fan it out. Returns the dict sent to browsers."""
        # Gap detection: a jump in seq means a producer message never arrived.
        # Counters are per (source, type) - one producer runs an independent
        # sequence for each message type, so tracking by source alone invents
        # drops that never happened.
        key = (msg.source, msg.type)
        prev = self.seq_seen.get(key)
        if prev is not None and msg.seq > prev + 1:
            self.dropped[msg.source] = self.dropped.get(msg.source, 0) + (msg.seq - prev - 1)
        self.seq_seen[key] = msg.seq
        self.last_seen[msg.source] = t_ingest

        event = {
            "type": msg.type,
            "seq": msg.seq,
            "source": msg.source,
            "t_capture": msg.t_capture.isoformat(),
            "t_ingest": t_ingest.isoformat(),
            # Server-side half of the REQ-M-19 budget. Same machine as the
            # producer on the Pi, so this number is free of clock skew.
            "ingest_latency_ms": round((t_ingest - msg.t_capture).total_seconds() * 1000, 1),
            "data": msg.data,
        }

        if msg.type == "air_reading":
            self.latest_air = event
        elif msg.type == "detection":
            self.targets.appendleft(event)

        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()      # drop oldest
                    q.put_nowait(event)
                except Exception:
                    pass
        return event

    # ---------------------------------------------------------------- health
    def health(self) -> Dict[str, Any]:
        """Per-source staleness. On demo day this tells you instantly whether a
        subsystem died or your page broke - not a requirement, but it will save you."""
        now = utcnow()
        sources = {}
        for name, seen in self.last_seen.items():
            age = (now - seen).total_seconds()
            sources[name] = {
                "last_seen": seen.isoformat(),
                "age_s": round(age, 2),
                "stale": age > STALE_AFTER_S,
                "dropped_messages": self.dropped.get(name, 0),
            }
        return {
            "ok": True,
            "server_time": now.isoformat(),
            "subscribers": self.subscriber_count,
            "sources": sources,
            "targets_held": len(self.targets),
        }


hub = Hub()
