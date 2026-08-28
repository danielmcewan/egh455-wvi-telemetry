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
  4. IP only: send a CROPPED snapshot of the target with each detection. That
     is REQ-F-07 - the dashboard has to show the picture, not just the label.
     See send_detection() below; the easiest route is `image_path=`.
"""
import argparse
import base64
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Anything over about 100 KB is bigger than the gallery can use. A 224x224
# JPEG at quality 80 is roughly 12 KB and renders fine at any size shown.
SOFT_IMAGE_WARN_BYTES = 100 * 1024


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
                   aruco_id=None, gauge_value_bar=None, image_ref=None,
                   image_path=None) -> dict:
    """Send one detection event - one per DETECTION, not one per frame.

    Two ways to attach the target snapshot, pick either:

      image_path="crop.jpg"    read the file and send the bytes inline. This is
                               the recommended route: one request, and it works
                               whether or not your code runs on the same Pi.

      image_ref="crop.jpg"     name only. Use this if you already uploaded the
                               file with upload_image() below, or if your code
                               writes straight into the payload's data/targets/
                               folder because you share the Pi.

    Returns the server's reply, which includes the stored `image_ref` so you
    can log your end of the handshake.
    """
    data = {
        "class": cls,                    # valve_open | valve_closed | gauge | aruco
        "confidence": confidence,        # 0.0 - 1.0
        "bbox": bbox,                    # [x, y, w, h] in pixels
        "aruco_id": aruco_id,            # only for class "aruco"
        "gauge_value_bar": gauge_value_bar,   # only for class "gauge"
        "image_ref": image_ref,
    }

    if image_path:
        raw = Path(image_path).read_bytes()
        if len(raw) > SOFT_IMAGE_WARN_BYTES:
            print(f"  note: {image_path} is {len(raw)//1024} KB. Crop tighter or "
                  f"drop the JPEG quality - the gallery shows these small.")
        # Base64 costs about a third more bytes than the raw file, which is why
        # the size note above is worth heeding.
        data["image_b64"] = base64.b64encode(raw).decode()

    payload = {"type": "detection", "seq": seq, "t_capture": now_iso(),
               "source": "IP", "data": data}
    r = requests.post(url, json=payload, timeout=10)
    if r.status_code == 422:
        # The server says exactly what was wrong. Read it - it saves an hour.
        print("  REJECTED (422):", r.text[:500])
    r.raise_for_status()
    return r.json()


def upload_image(base_url: str, image_path: str) -> str:
    """Upload a snapshot on its own and get back the name to put in image_ref.

    Only needed when the picture and the detection metadata come from different
    parts of your code, or when the file is too big to sit inside a JSON body.
    Upload BEFORE sending the detection that references it.

        ref = upload_image("http://<pi-ip>:8000", "crop.jpg")
        send_detection(url, 88, "gauge", 0.91, [..], image_ref=ref)
    """
    p = Path(image_path)
    with p.open("rb") as f:
        r = requests.post(f"{base_url.rstrip('/')}/api/targets/image",
                          files={"file": (p.name, f, "image/jpeg")}, timeout=15)
    r.raise_for_status()
    return r.json()["image_ref"]


def _demo_jpeg(path: Path) -> Path:
    """Make a small stand-in snapshot so this file demonstrates the image path
    even on a machine with no camera attached."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    img = Image.new("RGB", (224, 224), (252, 243, 219))
    d = ImageDraw.Draw(img)
    d.ellipse([52, 44, 172, 164], outline=(154, 103, 0), width=3)
    d.line([112, 104, 150, 132], fill=(154, 103, 0), width=3)
    d.text((14, 196), "demo gauge crop", fill=(154, 103, 0))
    img.save(path, format="JPEG", quality=80)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/ingest")
    ap.add_argument("--n", type=int, default=5, help="how many demo messages to send")
    ap.add_argument("--image", help="a JPEG/PNG crop to attach to the demo detection")
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

    image = args.image or _demo_jpeg(Path("_demo_target_crop.jpg"))
    reply = send_detection(args.url, 1, "gauge", 0.91, [412, 208, 96, 96],
                           gauge_value_bar=1.7,
                           image_path=str(image) if image else None)
    print("sent detection seq=1 (gauge, 1.7 bar - below the 2 bar drill threshold)")
    print("  server stored the snapshot as:", reply.get("image_ref"))
    print("  it is now visible in the Target Detections panel on the dashboard.")
