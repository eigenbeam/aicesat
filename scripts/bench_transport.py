#!/usr/bin/env python
"""Measure the scene push transport (src/aicesat/stream.py) end to end.

RETIRED A/B, kept as the record that justified deleting the pull path. Laptop -> box, scene 3c84548964, at EQUAL
point counts:

    same point count   pull 1.58s   push 0.62s    2.6x
    bytes/point        pull 18.07   push 12.12    1.49x (base64 was 4/3 of it)
    round-trips        pull 9       push 1
    uncapped push delivered 2,803,444 pts where the viewer had been showing 479,832 -- 17% of the scene

Decomposed: ~86ms RTT, ~18.6 MB/s. Pull spent ~0.77s of its 1.58s waiting on round-trips; push spends its time moving
bytes. Two numbers measured earlier in that work were harness artefacts and are NOT the result: 9.45x came from
urllib opening a fresh TLS connection per pull request (a browser reuses one), and a capped run that read 0.78x came
from contending with its own abandoned streams. The pull arm is gone from this script because the endpoint is gone.

    uv run python scripts/bench_transport.py --url https://44-241-137-16.sslip.io --code "$AICESAT_ACCESS_CODE" \
        --scene 3c84548964

Run it ON THE BOX (or against the box) — laptop timings are not evidence about the box, and this measures a network
transport, so the round-trip count dominates and the RTT is the whole experiment.

What each column means:
  wall        seconds from the request to the terminal `done` frame
  bytes       what actually crossed the wire (raw f32, no base64)
  requests    HTTP round-trips (one, by construction)
  points      points delivered to the client
  resets      push only: sidecars the writer replaced mid-stream, i.e. bytes the client had to throw away. On a
              FINISHED scene this must be 0; during a live build it counts the preview/finalize churn, which is the
              number that says how much the preview duality is costing (see stream.py).

Scope: an EXISTING, FINISHED scene. It does not measure time-to-first-paint during a live build; that needs a cold
build and belongs with bench_ingest_phases' cold-box discipline.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import struct
import sys
import time
import urllib.parse

HEADER = struct.Struct("<BBHI")
K_CONTROL, K_POSITIONS, K_SLOPES = 0, 1, 2


def gate_token(code: str) -> str:
    """Mirror server._gate_token so the bench can authenticate without a browser. Pinned by
    test_bench_transport_gate_token_matches_the_server — a silent drift here reads as an auth failure, not a bug."""
    return hmac.new(code.encode(), b"aicesat-gate-v1", hashlib.sha256).hexdigest()


class Client:
    """ONE kept-alive connection for every request, because that is what the browser does.

    urllib opens a fresh TCP+TLS connection per call. Benchmarking the pull path that way charges it a full handshake
    (~3 RTT plus cert work) for each of its sequential chunk GETs, when a browser pays roughly one RTT per chunk over
    an already-open HTTP/2 connection to Caddy. That inflated the pull wall by seconds and flattered the push path.
    The round-trip COUNT is the honest variable here; the handshake count is an artefact of the harness."""

    def __init__(self, base: str, code: str = ""):
        u = urllib.parse.urlsplit(base.rstrip("/"))
        self.host, self.scheme, self.prefix = u.netloc, u.scheme, u.path
        self.conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
            u.netloc, timeout=300)
        self.headers = {"Cookie": f"aicesat_gate={gate_token(code)}"} if code else {}
        self.requests = 0
        self.bytes = 0

    def open(self, path: str):
        self.requests += 1
        self.conn.request("GET", self.prefix + path, headers=self.headers)
        r = self.conn.getresponse()
        if r.status != 200:
            body = r.read()
            raise RuntimeError(f"HTTP {r.status} for {path}: {body[:200]!r}")
        return r

    def json(self, path: str):
        raw = self.open(path).read()          # must be fully read, or the connection cannot be reused
        self.bytes += len(raw)
        return json.loads(raw)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ------------------------------------------------------------------------------------------- the push transport
def push(c: Client, scene: str, cap: int | None = None) -> dict:
    """`cap` is the per-mission point budget, sent as `?limit=` so the SERVER stops. It is the only way to compare
    the transports like for like: uncapped, push ships 5.8x the data, so its wall answers a different question."""
    t0 = time.perf_counter()
    # DECLARE the budget. Stopping client-side does not stop the server (the proxy keeps draining the origin), so a
    # client-side cap both wastes box work and contaminates the next run by contending with the abandoned stream.
    resp = c.open(f"/api/scene/{scene}/stream" + (f"?limit={cap}" if cap else ""))
    buf, vals, resets, first = b"", 0, 0, None
    total = 0
    while True:
        piece = resp.read(65536)
        if not piece:
            break
        total += len(piece)
        buf += piece
        while len(buf) >= HEADER.size:
            kind, _mid, _flags, n = HEADER.unpack_from(buf, 0)
            if len(buf) < HEADER.size + n:
                break
            payload = buf[HEADER.size:HEADER.size + n]
            buf = buf[HEADER.size + n:]
            if kind == K_CONTROL:
                msg = json.loads(payload)
                if msg.get("t") == "reset":
                    resets += 1
            else:
                if first is None:
                    first = time.perf_counter() - t0
                if kind == K_POSITIONS:
                    vals += n // 4
    return {"wall": time.perf_counter() - t0, "bytes": total, "requests": 1,
            "points": vals // 3, "resets": resets, "first_points_s": first}


def show(name: str, r: dict) -> None:
    first = "—" if r["first_points_s"] is None else f"{r['first_points_s']:.2f}s"
    print(f"{name:<6} {r['wall']:>7.2f}s {r['bytes']/1e6:>9.2f} MB {r['requests']:>6}  "
          f"{r['points']:>10,}  {first:>8}  {r['resets']:>3}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8768")
    ap.add_argument("--code", default="", help="access code (the beta gate); omit for a private/local process")
    ap.add_argument("--scene", required=True, help="an existing scene id")
    ap.add_argument("--repeat", type=int, default=3, help="runs per transport; the MEDIAN is reported")
    ap.add_argument("--cap", type=int, default=None,
                    help="stop the push path at N points. Use --cap <the pull row's points> for the like-for-like "
                         "transport comparison, with the display-budget question held constant.")
    a = ap.parse_args()

    print(f"scene {a.scene} via {a.url}   (median of {a.repeat})\n")
    print(f"{'':<6} {'wall':>8} {'wire':>12} {'reqs':>6}  {'points':>10}  {'1st pts':>8}  res")
    runs = []
    for _ in range(a.repeat):
        try:
            c = Client(a.url, a.code)
            runs.append(push(c, a.scene, a.cap))
            c.close()
        except Exception as e:
            print(f"push: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    runs.sort(key=lambda r: r["wall"])
    r = runs[len(runs) // 2]
    show("push", r)
    print(f"\nbytes/point {r['bytes'] / max(r['points'], 1):.2f}   "
          f"per 1M points {r['wall'] / max(r['points'], 1) * 1e6:.2f}s   "
          f"throughput {r['bytes'] / 1e6 / max(r['wall'], 1e-9):.1f} MB/s")
    if r["resets"]:
        print(f"\nNOTE: {r['resets']} reset(s) on a FINISHED scene. Expect one per mission at its finalize during a\n"
              f"build and none afterwards; more than that means a sidecar is being rewritten when it should not be.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
