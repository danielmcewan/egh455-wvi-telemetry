"""The data contract, expressed as code.

This module IS the interface specification - if you change a field name here,
you have changed the ICD and you owe your teammates a heads-up.

Every message shares one envelope so the server can route on `type` without
knowing anything about how the data was produced.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Always work in UTC internally. Format for humans at the edges only."""
    return datetime.now(timezone.utc)


class Envelope(BaseModel):
    """The outer shape every producer must send."""

    type: Literal["air_reading", "detection"]
    seq: int = Field(ge=0, description="Producer's own counter. Lets us spot dropped messages.")
    t_capture: datetime = Field(description="When the SENSOR read it - not when it was sent.")
    source: Literal["AQ", "IP", "TAI", "SIM"] = Field(description="Which subsystem produced this.")
    data: Dict[str, Any]


class AirData(BaseModel):
    """REQ-F-03 / REQ-F-06. Units live in the field names on purpose - it makes a
    unit mismatch a loud error instead of a silently wrong dashboard."""

    temperature_c: float
    humidity_pct: float
    pressure_hpa: float
    light_lux: float
    # MiCS-6814 reports resistance. Do not invent ppm you cannot calibrate.
    gas_oxidising_ohm: float
    gas_reducing_ohm: float
    gas_nh3_ohm: float


class DetectionData(BaseModel):
    """REQ-F-05 / REQ-F-07."""

    # `class` is a reserved word in Python, so the field is `cls` and the alias
    # is what actually travels on the wire.
    cls: Literal["valve_open", "valve_closed", "gauge", "aruco"] = Field(alias="class")
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: List[int] = Field(min_length=4, max_length=4, description="[x, y, w, h] in pixels")
    aruco_id: Optional[int] = None
    # Feeds REQ-F-09's 2 bar drill threshold. Null unless cls == "gauge".
    gauge_value_bar: Optional[float] = None

    # REQ-F-07 demands the *images of the targets* be displayed, not just a
    # list of what was found. There are three legitimate ways for IP to get a
    # snapshot to us and all three end up populating `image_ref`:
    #   1. IP writes the file into data/targets/ itself (same Pi) and sends
    #      just the file name here;
    #   2. IP sends the bytes inline as base64 in `image_b64` below - the
    #      server writes the file and fills `image_ref` in on their behalf;
    #   3. IP POSTs the file to /api/targets/image first and puts the returned
    #      name here.
    # Whichever they pick, the browser only ever sees `image_ref`.
    image_ref: Optional[str] = None
    # Base64 of the JPEG/PNG bytes. Optional, and stripped before storage -
    # we keep the file, not a giant string in the database or on the wire.
    image_b64: Optional[str] = Field(default=None, exclude=True)

    model_config = {"populate_by_name": True}


PAYLOAD_MODELS = {"air_reading": AirData, "detection": DetectionData}


def validate_payload(msg: Envelope):
    """Validate `data` against the right model for this message type."""
    return PAYLOAD_MODELS[msg.type].model_validate(msg.data)
