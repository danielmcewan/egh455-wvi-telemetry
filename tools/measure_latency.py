"""REQ-M-19 evidence generator.

Sends N messages with a known capture time and reports how long the server took
to accept each one. Run this on the Pi, screenshot the output, and it becomes a
row of evidence in your test report.

    python measure_latency.py --url http://127.0.0.1:8000/api/ingest --n 100
"""
import argparse
import statistics
import time
from datetime import datetime, timezone

import requests

BUDGET_MS = 4000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/ingest")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--rate", type=float, default=10.0, help="messages per second")
    args = ap.parse_args()

    round_trip, server_side = [], []
    for i in range(1, args.n + 1):
        t0 = time.perf_counter()
        r = requests.post(args.url, timeout=5, json={
            "type": "air_reading", "seq": i,
            "t_capture": datetime.now(timezone.utc).isoformat(),
            "source": "AQ",
            "data": {"temperature_c": 23.0, "humidity_pct": 50.0,
                     "pressure_hpa": 1013.0, "light_lux": 400.0,
                     "gas_oxidising_ohm": 21000.0, "gas_reducing_ohm": 145000.0,
                     "gas_nh3_ohm": 98000.0},
        })
        r.raise_for_status()
        round_trip.append((time.perf_counter() - t0) * 1000)
        server_side.append(r.json()["ingest_latency_ms"])
        time.sleep(max(0.0, 1.0 / args.rate - (time.perf_counter() - t0)))

    def report(name, xs):
        xs_sorted = sorted(xs)
        p95 = xs_sorted[int(len(xs_sorted) * 0.95) - 1]
        print(f"  {name:<22} n={len(xs)}  mean={statistics.mean(xs):7.2f} ms  "
              f"p95={p95:7.2f} ms  max={max(xs):7.2f} ms")
        return max(xs)

    print(f"\nREQ-M-19 latency measurement — budget {BUDGET_MS} ms")
    print(f"  target: {args.url}")
    worst = max(report("capture → ingest", server_side),
                report("full HTTP round trip", round_trip))
    print(f"\n  RESULT: {'PASS' if worst <= BUDGET_MS else 'FAIL'} "
          f"(worst {worst:.2f} ms vs {BUDGET_MS} ms budget)\n")


if __name__ == "__main__":
    main()
