# WVI Data Contract v0.3

**Status:** proposed — needs sign-off from the AQ and IP leads.
Once agreed this becomes the WVI section of the Interface Control Document (ICD).

An **interface contract** is just a written agreement about the shape of the data
crossing the boundary between two people's code. It exists so neither of you has
to wait for the other.

**Changed in v0.3 — please read, this one needs action from IP.** Three new
optional fields on a detection: `pose_x_m`, `pose_y_m`, `pose_z_m`. The customer
needs document requires them and v0.2 did not carry them. HLO-M-3 asks for
"ArUCO marker with its ID **and localisation Coordinates (x, y, z) indicating the
current position of the UAV w.r.t the ArUCO marker**", and asks for the same
coordinates again in the Logged Data Display and in Data Storage and Logging.
HLO-M-2 says they come from ArUco pose estimation. Nothing was removed; a
detection without them is still accepted, so you can add them when your pose
solve is working.

**Changed in v0.2:** target snapshot images. REQ-F-07 asks for *the images of the
targets*, not only a list of what was found, so this version says exactly how a
picture gets from the image processing subsystem onto the dashboard. See
"Sending the target image" below. Nothing in v0.1 was removed.

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
    "pose_x_m": null,
    "pose_y_m": null,
    "pose_z_m": null,
    "image_ref": "det_0088.jpg",
    "image_b64": null
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `class` | yes | Exactly one of `valve_open`, `valve_closed`, `gauge`, `aruco`. Anything else is rejected with a 422 |
| `confidence` | yes | 0.0–1.0 |
| `bbox` | yes | `[x, y, w, h]` in pixels, top-left origin, in the coordinate space of the frame you cropped from |
| `aruco_id` | no | Integer, only when `class` is `aruco`, otherwise `null` |
| `gauge_value_bar` | no | Only when `class` is `gauge`. **Feeds the 2 bar drill threshold in REQ-F-09** |
| `pose_x_m` | no | Metres. UAV position relative to the marker, camera frame. Only when `class` is `aruco` |
| `pose_y_m` | no | Metres, same frame |
| `pose_z_m` | no | Metres, same frame. Typically the range to the marker |
| `image_ref` | no | Name of the snapshot file — see below |
| `image_b64` | no | The snapshot bytes inline — see below |

Send one message **per detection event**, not per frame.

### On the localisation coordinates

Send all three or none — two out of three is treated as none, because a partial
position is worse than no position on an operator's display. Units are **metres**
and the frame is the camera's, as HLO-M-2 specifies ("extract their pose
estimation to provide the UAV's local position coordinates (x, y, z) in the
camera's frame of reference"). If you switch to a room-fixed frame later, say so
and this contract gets a version bump, because the dashboard labels them as
relative to the marker.

`cv2.aruco.estimatePoseSingleMarkers` (or `solvePnP`) gives you a translation
vector directly; the marker is 200 mm, dictionary `5x5_100`. WVI stores whatever
you send and does no transformation.

---

## Sending the target image

**This is the part that satisfies REQ-F-07**, which asks the web interface to
display "the images of the targets ... and update every time a new picture is
taken". Send a **cropped snapshot of the target**, not the whole frame — the
gallery shows them at thumbnail size and a full 640×480 frame with the target
40 px wide is useless to look at.

- **Format:** JPEG or PNG. The server checks the actual bytes, not the file
  extension, and rejects anything else with a 422.
- **Size:** anything up to 4 MB is accepted; aim for **under 100 KB**. A
  224×224 JPEG at quality 80 is about 12 KB and looks fine at any size the
  dashboard renders it.
- **How many:** one per detection message. The server keeps the most recent 400
  and deletes older ones, so a long integration session cannot fill the Pi's SD
  card.

There are three ways to get the picture across. **Pick one and tell Daniel which
one you picked** — the dashboard behaves identically either way, but the failure
modes differ and it matters for the ICD.

### Route A — inline base64 (recommended, and the one to use if unsure)

Put the encoded bytes in `image_b64` on the detection message. One HTTP request,
no ordering problem, works whether or not your code is on the same Pi.

```python
import base64, requests, datetime

with open("crop.jpg", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

requests.post("http://<payload-ip>:8000/api/ingest", json={
    "type": "detection",
    "seq": 88,
    "t_capture": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source": "IP",
    "data": {
        "class": "gauge",
        "confidence": 0.91,
        "bbox": [412, 208, 96, 96],
        "gauge_value_bar": 1.7,
        "image_b64": b64,
    },
})
```

The server writes the file, fills `image_ref` in for you, and returns the stored
name in the response so you can log it your end. A `data:image/jpeg;base64,...`
prefix is accepted and stripped, so it does not matter if your encoder adds one.

`image_b64` is **never** stored in the database or pushed to browsers — the
server keeps the file and throws the string away. Base64 costs about 33% more
bytes than the raw file, which is why the size guidance above matters.

### Route B — upload the file, then reference it

Use this if the image is large, or if the picture and the metadata come from
different parts of your code.

```bash
curl -F "file=@crop.jpg" http://<payload-ip>:8000/api/targets/image
# {"stored":true,"image_ref":"crop.jpg","url":"/api/targets/image/crop.jpg","bytes":11834}
```

Then send the detection with `"image_ref": "crop.jpg"`. Upload **before** the
detection message, or the card appears with no picture until you send another.

### Route C — write the file yourself (same Pi only)

If your code runs on the same Raspberry Pi, write the file into
`wvi/data/targets/` and send only its name in `image_ref`.

Zero bytes on the wire, but it only works when we share a filesystem, and it
breaks silently if the path is wrong. Route A is safer.

### What the dashboard does with it

Whichever route you use, `image_ref` ends up as a bare file name and the image
is served at `GET /api/targets/image/<name>`. Send a path like
`targets/det_0088.jpg` and the server keeps only `det_0088.jpg`; names are
restricted to letters, digits, dot, dash and underscore. A detection with no
image still appears in the gallery, marked "no snapshot" — **a missing picture
never costs you the detection.**

---

## What WVI guarantees back

| Endpoint | What it does | Requirement |
|---|---|---|
| `GET /api/stream` | Pushes every message to browsers over Server-Sent Events | REQ-F-06 |
| `GET /video/stream.mjpg` | Live camera feed as MJPEG | REQ-F-07 |
| `GET /api/targets` | Recent detections, filterable by `class` | REQ-F-05, REQ-F-07 |
| `POST /api/targets/image` | Upload a target snapshot (route B) | REQ-F-07 |
| `GET /api/targets/image/<name>` | Serve a stored snapshot | REQ-F-07 |
| `GET /api/history` | Search and page the timestamped log | REQ-F-08 |
| `GET /api/export.csv` | Download the filtered log | REQ-F-08 |
| `GET /api/filters` | Sources and classes actually present in the log | — |
| `GET /api/mission` | Continuous-operation and detection-tally evidence | REQ-M-15 |
| `GET /api/latency` | Measured latency evidence, with percentiles | REQ-M-19 |
| `GET /healthz` | Per-source staleness — tells you if a subsystem died | — |

### The live camera feed is still yours to deliver

`GET /video/stream.mjpg` currently serves synthetic frames. REQ-F-07 wants the
**object detector's** feed — the camera image with the bounding boxes already
drawn on it — and REQ-M-16 says that processing happens onboard. So the boxes
get drawn by IP, not by the dashboard.

Two options, and this one is **not yet decided** — it needs a conversation:

1. **IP exposes its own MJPEG endpoint** and WVI proxies or embeds it. Cleanest
   split: the annotated frame never leaves the process that made it.
2. **IP pushes annotated JPEG frames to WVI** and WVI serves the MJPEG stream.
   More work on this side, but one less port to open.

Either way WVI needs the frames *with the boxes already on them*. Please raise
this before integration week.

## Payload LCD display mode — for the enclosure subsystem

HLO-M-5 says the operator must be able to select what the LCD shows "using the
proximity sensor on the Pimoroni Env sensor **and remotely from the GCS Web
interface**". WVI provides the remote half; the panel itself is yours.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/lcd` | GET | Returns `{"mode": "ip" \| "detection" \| "temperature", "set_at": ..., "set_by": ...}` |
| `/api/lcd?mode=<mode>` | POST | The dashboard control. You should not need to call this |

Poll the GET every second or two from your LCD driver and show whatever `mode`
says. WVI holds the operator's choice and nothing else — it does not talk to the
panel, and it does not know or care whether the proximity sensor has since
changed the display locally. If you want local and remote selection to stay in
step, POST back when the proximity sensor changes it, with `by=proximity`.

## Change control

Any change to a field name or unit **must** be agreed by AQ, IP and WVI, and the
version number above bumped. That rule is the whole reason a contract is worth
writing down.

## Open questions

1. ~~Should `image_ref` be a path on the Pi's filesystem, or should IP POST the
   actual image bytes to WVI?~~ **Resolved in v0.2** — all three routes are
   supported; IP picks one and says which.
2. ~~Do we need a `location` or pose field for where the UAV was at capture
   time? Not required by any REQ, but GCS may want it.~~ **Resolved in v0.3, and
   the earlier answer was wrong** — HLO-M-3 requires the localisation
   coordinates in three separate places. Fields added; IP needs to populate
   them.
3. Is TAI a separate producer, or does it re-publish IP's detections with the
   classification filled in? Affects whether `source` is `IP` or `TAI`.
4. **Who serves the annotated video?** See "The live camera feed" above. This is
   the last genuinely undecided interface.
5. Does IP want the `gauge_value_bar` reading to come from them, or should WVI
   derive the drill condition from something else? Currently WVI only *reports*
   the sub-2-bar condition; actuating the drill belongs to EDM, and nobody has
   written down how EDM learns about it.
