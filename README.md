# WVI — Web Visualisation subsystem

The dashboard for UAVPayload<sup>TAQ</sup>-26. Runs on the Raspberry Pi, watched
from a laptop on the ground while the drone flies.

Owns **REQ-F-06**, **REQ-F-07**, **REQ-F-08** and **REQ-M-19**.

---

## Run it

```bash
.venv\Scripts\python.exe run.py
```

Then open <http://127.0.0.1:8000>. It starts with a **simulator** — fake sensor
data — so the whole dashboard works with no hardware attached.

| URL | What it is |
|---|---|
| `/` | The dashboard |
| `/docs` | Auto-generated API documentation. Paste this into your ICD |
| `/healthz` | Which subsystems are alive, and how stale their data is |
| `/api/latency` | Latency evidence for REQ-M-19 |

The startup banner prints an address other machines should use. `127.0.0.1` only
works on this computer; the other one works across the network.

## Jargon, in plain terms

| Term | What it actually means |
|---|---|
| **Server-Sent Events (SSE)** | One connection the server pushes new data down whenever it has some. The browser doesn't have to keep asking |
| **Polling** | The lazy alternative: asking "anything new?" every second and usually hearing "no" |
| **MJPEG** | A video stream that's just ordinary JPEG photos sent one after another. Browsers show it in an `<img>` tag — no video player needed |
| **Endpoint** | One URL the server answers on, e.g. `/api/history` |
| **Ingest** | Data coming *in* to your server from someone else's code |
| **Schema / contract** | The agreed shape of that data: field names, types, units |
| **Adapter** | A small swappable piece that converts someone else's format into yours |
| **SQLite** | A database that's just one file on disk. No server to install |
| **Bind to 0.0.0.0** | "Accept connections from any network card." `127.0.0.1` means "this computer only" |
| **Latency** | Delay. Here: sensor reading → visible on screen |
| **NFR** | Non-functional requirement — a speed/reliability limit rather than a feature. REQ-M-19 is yours |
| **Pub/sub** | Producers shout data onto a named channel; anyone interested listens. Nobody calls anybody directly |
| **Venv** | A private folder of Python packages for this project only, so it can't break other software on your machine |

## How the pieces fit

```
AQ subsystem  ─┐
IP subsystem  ─┼─► POST /api/ingest ─► ingest() ─┬─► hub  ─► SSE ─► browser
simulator     ─┘                                 └─► SQLite (the log)

camera ─► /video/stream.mjpg ─► browser <img>
```

Everything enters through **one function** — `ingest()` in `app/main.py`. That is
deliberate: the simulator, the HTTP endpoint and any future ROS2 adapter all use
it, so swapping the source of data changes nothing else.

## Files

| File | Job |
|---|---|
| `contract.md` | **The data contract.** Send this to the AQ and IP leads |
| `app/models.py` | The contract as code. Change a field here = you changed the ICD |
| `app/hub.py` | Fan-out to browsers, latest values, staleness tracking |
| `app/db.py` | SQLite writing and history queries (REQ-F-08) |
| `app/camera.py` | MJPEG stream (REQ-F-07) |
| `app/simulator.py` | Fake producer so you can build without hardware |
| `app/main.py` | The routes |
| `static/` | The dashboard page |
| `tools/publisher_stub.py` | **Give this to teammates.** All they need to send data |
| `tools/measure_latency.py` | Generates REQ-M-19 evidence |

## The three timestamps

The single most important design decision here.

| Stamp | Set by | Why |
|---|---|---|
| `t_capture` | The producer, when the sensor reads | The clock starts here |
| `t_ingest` | This server, on arrival | Same machine as the producer on the Pi, so no clock-mismatch problem |
| `t_render` | The browser, when drawn | The full path, but only trustworthy if both machines' clocks agree |

`t_capture` → `t_ingest` is your **defensible** number for REQ-M-19. The browser
figure is useful but carries a caveat you should state in your test report.

> With the simulator, ingest latency reads 0 ms — it captures and ingests in the
> same instant on one machine. That's expected, not a bug. Real numbers appear
> once data crosses a network. Use `tools/measure_latency.py` for real figures.

## Switching to real data

1. Send `contract.md` and `tools/publisher_stub.py` to the AQ and IP leads.
2. They POST to `http://<pi-ip>:8000/api/ingest`.
3. Turn the simulator off: set `WVI_SIMULATOR=0`.

That's the whole integration. **If it needs code changes, the contract was
wrong** — that's the real test of this design.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `WVI_SIMULATOR` | `1` | Fake data on/off |
| `WVI_WEBCAM` | `0` | Use a real webcam if OpenCV is installed |
| `WVI_HOST` | `0.0.0.0` | Reachable from other machines |
| `WVI_PORT` | `8000` | Port |

## Still to do

- [ ] Get the contract signed off by AQ and IP — until then it's a guess
- [ ] Deploy to the Pi and re-measure latency there (a laptop proves nothing about Pi speed)
- [ ] Prove reachability from a **third** machine, not just a second (REQ-F-08 says *any* computer)
- [ ] Point the camera at the real IP feed instead of the synthetic one
- [ ] Decide whether `image_ref` is a file path or uploaded bytes — see `contract.md`
- [ ] Run for 10 minutes continuously and keep the log (REQ-M-15)

## Understand this before your seminar

You present this individually and tutors will ask. The two things to be able to
answer without notes:

1. **Why SSE and not WebSockets?** Data only flows one way, SSE reconnects by
   itself, and it's plain HTTP. WebSockets would be extra complexity for nothing.
2. **How do you know you meet the 4-second budget?** Because you timestamp at
   capture, at ingest and at render, log all three, and `/api/latency` reports
   the worst case against the budget.

Good exercise: delete `app/main.py`'s SSE handler and the ingest path, and
rewrite both from scratch without looking. If you can, you own this code.
