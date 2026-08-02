# WVI Data Contract v0.1

**Status:** proposed — needs sign-off from the AQ and IP leads.
Once agreed this becomes the WVI section of the Interface Control Document (ICD).

An **interface contract** is just a written agreement about the shape of the data
crossing the boundary between two people's code. It exists so neither of you has
to wait for the other.

---

## How to send data

`POST http://<payload-ip>:8000/api/ingest` with a JSON body.
Any language. No shared library. See `tools/publisher_stub.py`.

The server replies `422` with a description of exactly what was wrong if the
shape does not match — so you find mistakes immediately, not on demo day.

## The envelope

Every message, whatever its type, looks like this:

| Field | Type | Meaning |
|---|---|---|
| `type` | `"air_reading"` \| `"detection"` | Which kind of message this is |
| `seq` | integer | Your own counter, +1 each message. Lets us detect drops |
| `t_capture` | ISO-8601 UTC string | **When the sensor read it.** Not when you sent it |
| `source` | `"AQ"` \| `"IP"` \| `"TAI"` \| `"SIM"` | Which subsystem produced it |
| `data` | object | Depends on `type` — see below |

> `t_capture` is the one field people get wrong. Stamping it at send time makes
> the 4-second latency requirement (REQ-M-19) unmeasurable, because you have
> thrown away the information about how long the sensor-to-network step took.

## `type: "air_reading"` — from AQ

```json
{
  "type": "air_reading",
  "seq": 1421,
  "t_capture": "2026-07-30T14:22:31.482000+00:00",
  "source": "AQ",
  "data": {
    "temperature_c": 23.7,
    "humidity_pct": 51.2,
    "pressure_hpa": 1013.4,
    "light_lux": 412.0,
    "gas_oxidising_ohm": 21500.0,
    "gas_reducing_ohm": 145000.0,
    "gas_nh3_ohm": 98000.0
  }
}
```

Units are part of the field names on purpose — a mismatch becomes an obvious
error instead of a silently wrong dashboard. Gas values are **resistance in
ohms**, which is what the MiCS-6814 sensor actually reports; don't convert to
ppm you can't calibrate.

Suggested rate: **1 Hz**. Faster is fine, the dashboard will keep up.

## `type: "detection"` — from IP / TAI

```json
{
  "type": "detection",
  "seq": 88,
  "t_capture": "2026-07-30T14:22:33.001000+00:00",
  "source": "IP",
  "data": {
    "class": "gauge",
    "confidence": 0.91,
    "bbox": [412, 208, 96, 96],
    "aruco_id": null,
    "gauge_value_bar": 1.7,
    "image_ref": "targets/det_0088.jpg"
  }
}
```

| Field | Notes |
|---|---|
| `class` | One of `valve_open`, `valve_closed`, `gauge`, `aruco` |
| `confidence` | 0.0–1.0 |
| `bbox` | `[x, y, w, h]` in pixels, top-left origin |
| `aruco_id` | Only when `class` is `aruco`, otherwise `null` |
| `gauge_value_bar` | Only when `class` is `gauge`. **Feeds the 2 bar drill threshold in REQ-F-09** |
| `image_ref` | Path/name of the saved snapshot, or `null` |

Send one message **per detection event**, not per frame.

---

## What WVI guarantees back

| Endpoint | What it does | Requirement |
|---|---|---|
| `GET /api/stream` | Pushes every message to browsers over Server-Sent Events | REQ-F-06 |
| `GET /video/stream.mjpg` | Live camera feed as MJPEG | REQ-F-07 |
| `GET /api/targets` | Recent detections | REQ-F-07 |
| `GET /api/history` | Query the timestamped log | REQ-F-08 |
| `GET /api/latency` | Measured latency evidence | REQ-M-19 |
| `GET /healthz` | Per-source staleness — tells you if a subsystem died | — |

## Change control

Any change to a field name or unit **must** be agreed by AQ, IP and WVI, and the
version number above bumped. That rule is the whole reason a contract is worth
writing down.

## Open questions

1. Should `image_ref` be a path on the Pi's filesystem, or should IP POST the
   actual image bytes to WVI? Filesystem path is simpler if both run on the
   same Pi — confirm this is the case.
2. Do we need a `location` or pose field for where the UAV was at capture time?
   Not required by any REQ, but GCS may want it.
3. Is TAI a separate producer, or does it re-publish IP's detections with the
   classification filled in? Affects whether `source` is `IP` or `TAI`.
