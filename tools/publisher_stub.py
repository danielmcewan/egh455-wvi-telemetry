"""HAND THIS FILE TO THE AQ AND IP LEADS.

This is everything another subsystem needs in order to get data onto the
dashboard. No shared library, no imports from the web server, no need to
understand any of it. Just an HTTP POST with JSON in it.

    python publisher_stub.py --url http://<pi-ip>:8000/api/ingest

Rules that matter:
  1. t_capture is when the SENSOR READ, not when you sent it. If you stamp it
     at send time the latency measurement (REQ-M-19) becomes meaningless.
  2. seq counts up by one, per producer. It is how we spot dropped messages.
  3. Units are baked into the field names. Send ohms where it says ohm.
"""
import argparse
import time
from datetime import datetime, timezone

import requests


def now_iso() -> str:
    """ISO-8601 in UTC, e.g. 2026-07-30T14:22:31.482000+00:00"""
    return datetime.now(timezone.utc).isoformat()


def send_air_reading(url: str, seq: int, readings: dict) -> None:
    payload = {
        "type": "air_reading",
        "seq": seq,
        "t_capture": now_iso(),      # stamp at the moment of the sensor read
        "source": "AQ",
        "data": readings,
    }
    r = requests.post(url, json=payload, timeout=2)
    r.raise_for_status()


def send_detection(url: str, seq: int, cls: str, confidence: float, bbox: list,
                   aruco_id=None, gauge_value_bar=None, image_ref=None) -> None:
    payload = {
        "type": "detection",
        "seq": seq,
        "t_capture": now_iso(),
        "source": "IP",
        "data": {
            "class": cls,                    # valve_open | valve_closed | gauge | aruco
            "confidence": confidence,        # 0.0 - 1.0
            "bbox": bbox,                    # [x, y, w, h] in pixels
            "aruco_id": aruco_id,            # only for class "aruco"
            "gauge_value_bar": gauge_value_bar,   # only for class "gauge"
            "image_ref": image_ref,
        },
    }
    r = requests.post(url, json=payload, timeout=2)
    r.raise_for_status()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/ingest")
    ap.add_argument("--n", type=int, default=5, help="how many demo messages to send")
    args = ap.parse_args()

    for i in range(1, args.n + 1):
        send_air_reading(args.url, i, {
            "temperature_c": 23.7,
            "humidity_pct": 51.2,
            "pressure_hpa": 1013.4,
            "light_lux": 412.0,
            "gas_oxidising_ohm": 21500.0,
            "gas_reducing_ohm": 145000.0,
            "gas_nh3_ohm": 98000.0,
        })
        print(f"sent air_reading seq={i}")
        time.sleep(1)

    send_detection(args.url, 1, "gauge", 0.91, [412, 208, 96, 96],
                   gauge_value_bar=1.7, image_ref="targets/det_0001.jpg")
    print("sent detection seq=1 (gauge, 1.7 bar - below the 2 bar drill threshold)")
