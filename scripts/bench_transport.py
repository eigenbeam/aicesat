#!/usr/bin/env python
"""A/B the two scene transports against the SAME scene: the shipped PULL path vs the prototype PUSH path.

    uv run python scripts/bench_transport.py --url https://44-241-137-16.sslip.io --code "$AICESAT_ACCESS_CODE" \
        --scene 3c84548964

Run it ON THE BOX (or against the box) — laptop timings are not evidence about the box, and this measures a network
transport, so the round-trip count dominates and the RTT is the whole experiment.

What each column means:
  wall        seconds from first request to last byte
  bytes       what actually crossed the wire (base64 inflates the pull path by 4/3; the push path is raw f32)
  requests    HTTP round-trips. The pull path is `meta` + ceil(values/chunk) SEQUENTIAL GETs per mission per array.
  points      points actually delivered to the client
  resets      push only: sidecars the writer replaced mid-stream, i.e. bytes the client had to throw away. On a
              FINISHED scene this must be 0; during a live build it counts the preview/finalize churn, which is the
              number that says how much the preview duality is costing (see stream.py).

Scope: this replays an EXISTING, FINISHED scene through both transports — identical bytes at rest, only the
delivery differs, which is the clean transport comparison. It does NOT measure time-to-first-paint during a live
build; that needs a cold build per transport and belongs with bench_ingest_phases' cold-box discipline.
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


# ----------------------------------------------------------------------------------- the shipped PULL transport
DISPLAY_BUDGET = 400_000        # keep in step with ui/adapter.js; the pull path fetches at most this many points


def pull(c: Client, scene: str) -> dict:
    """Reproduce exactly what adapter.js does on a cold open: meta, then a bounded prefix of each array in
    sequential ~1 MB chunks. Sequential is not a simplification — it is what the client does (fetchValuesFrom
    awaits each chunk before asking for the next), and on a high-RTT link it is the whole cost."""
    t0 = time.perf_counter()
    meta = c.json(f"/api/scene/{scene}/part?part=meta")
    points = 0
    for mission, s in (meta.get("series") or {}).items():
        shown = min(s.get("n") or 0, DISPLAY_BUDGET)
        points += shown
        for kind, per in (("positions", 3), ("slopes", 2)):
            if kind == "slopes" and not s.get("has_slopes"):
                continue
            want = shown * per
            got, chunk, n_chunks = 0, 0, 1
            while got < want and chunk < n_chunks:
                d = c.json(f"/api/scene/{scene}/part?part={kind}:{urllib.parse.quote(mission)}&chunk={chunk}")
                n_chunks = d["n_chunks"]
                got += len(base64.b64decode(d["b64"])) // 4
                chunk += 1
    return {"wall": time.perf_counter() - t0, "bytes": c.bytes, "requests": c.requests,
            "points": points, "resets": 0, "first_points_s": None}


# ------------------------------------------------------------------------------------ the prototype PUSH transport
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
    ap.add_argument("--order", default="both", choices=("both", "pull", "push"))
    ap.add_argument("--cap", type=int, default=None,
                    help="stop the push path at N points. Use --cap <the pull row's points> for the like-for-like "
                         "transport comparison, with the display-budget question held constant.")
    a = ap.parse_args()

    print(f"scene {a.scene} via {a.url}   (median of {a.repeat})\n")
    print(f"{'':<6} {'wall':>8} {'wire':>12} {'reqs':>6}  {'points':>10}  {'1st pts':>8}  res")
    out = {}
    for name, fn in (("pull", pull), ("push", push)):
        if a.order != "both" and a.order != name:
            continue
        runs = []
        for _ in range(a.repeat):
            try:
                c = Client(a.url, a.code)
                runs.append(fn(c, a.scene, a.cap) if name == "push" else fn(c, a.scene))
                c.close()
            except Exception as e:
                print(f"{name}: {type(e).__name__}: {e}", file=sys.stderr)
                return 1
        runs.sort(key=lambda r: r["wall"])
        out[name] = runs[len(runs) // 2]
        show(name, out[name])

    if "pull" in out and "push" in out:
        p, q = out["pull"], out["push"]
        # NORMALISE. The two transports do not deliver the same thing: pull stops at DISPLAY_BUDGET, push delivers
        # everything. Comparing raw totals reads as "push used 4x the bytes" when it in fact moved 6x the points more
        # cheaply per point. Ratios of totals across different workloads are not a result, they are a category error.
        pb, qb = p["bytes"] / max(p["points"], 1), q["bytes"] / max(q["points"], 1)
        pt, qt = p["wall"] / max(p["points"], 1), q["wall"] / max(q["points"], 1)
        print(f"\ndelivered      pull {p['points']:>10,} pts     push {q['points']:>10,} pts"
              f"   ({q['points']/max(p['points'], 1):.1f}x, pull is capped at DISPLAY_BUDGET per mission)")
        print(f"bytes/point    pull {pb:>10.2f}      push {qb:>10.2f}      {pb/max(qb, 1e-9):.2f}x smaller"
              f"   (base64 is 4/3 of it)")
        print(f"per 1M points  pull {pt*1e6:>10.2f}s     push {qt*1e6:>10.2f}s     {pt/max(qt, 1e-9):.2f}x faster")
        print(f"round-trips    pull {p['requests']:>10}      push {q['requests']:>10}")
        rtt = p["requests"] - q["requests"]
        print(f"\nRTT SENSITIVITY: push saves {rtt} round-trips. That is worth ~{rtt} x RTT to a real client and "
              f"NOTHING on loopback.\nIf you ran this on the box against the box, the wall column is not evidence "
              f"about a browser — re-run it from the client's actual network position.")
        if q["resets"]:
            print(f"NOTE: {q['resets']} reset(s) on a finished scene — the sidecar was rewritten mid-stream, "
                  f"which should not happen once a build is terminal. Investigate before trusting the wall time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
