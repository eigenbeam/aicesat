"""The PUSH transport: a scene's point arrays and DEM surface, streamed as they land. The only transport for them.

What this replaced, and why. The old path was a PULL loop: the browser polled `meta`, diffed a `seriesVersion`, and
fetched a bounded PREFIX of each mission's sidecar as base64 JSON chunks. Nearly every awkward part of that design
existed to make "a prefix of a growing array" a fair sample of the whole — a seeded shuffle at write time, a
power-of-2 thinning loop behind a preview cap, a client-side display budget, and chunk-resume arithmetic. Push the
bytes as they land and not one of those questions has to be answered; all of it is deleted.

Measured before deleting, laptop -> box, at EQUAL point counts: 1.58s -> 0.62s, 18.07 -> 12.12 bytes per point,
9 round-trips -> 1. The pull path was latency-bound; this one is bandwidth-bound.

The MCP App transport cannot stream (tools/call is request/response), so it renders the DEM surface and metadata
without point clouds, via the chunked parts that api.scene_part still serves for everything except points.

## Frame format (little-endian), header + payload:

    u8  kind        0 = control (payload is UTF-8 JSON), 1 = positions (f32), 2 = slopes (f32), 3 = DEM surface (f32)
    u8  mission     index into the mission table built by the control frames; 0 on a control frame
    u16 flags       reserved, always 0
    u32 n_bytes     payload length in bytes

Control frames carry the small stuff (mission table, resets, done) so the reader needs exactly one code path and no
second HTTP request. Bulk arrays stay raw f32 — no base64, which is a third of the bytes on the wire.

## The sidecar is NOT append-only, and that is the point of the `reset` frame

`tail -f` would be sound if the sidecar only ever grew. It grows all build long — and then, once, it is REPLACED:
scene.series writes the finalized array over the streamed preview at add_series. That is not vestigial. The
authoritative arrays are not always the same points: GLAS runs drop_glas_outliers in add_series and nowhere else, so
its final series is a strict subset of what streamed. Whichever way, os.replace gives the path a NEW INODE while the
client holds bytes from the old one, and size alone cannot detect it (a replacement can be LARGER than what we
already sent — classic ABA). So identity is the inode: when it changes, emit `{"t":"reset"}` for that mission and
restart its cursor at zero.

Expect at most one reset per mission per build, at its finalize. More than that means something is rewriting a
sidecar it should not, and the count is on the `done` frame so a client can say so.
"""
from __future__ import annotations

import json
import struct
import time

import numpy as np

from . import cache

HEADER = struct.Struct("<BBHI")
KIND_CONTROL, KIND_POSITIONS, KIND_SLOPES, KIND_SURFACE = 0, 1, 2, 3
ARRAY_KINDS = {"positions": KIND_POSITIONS, "slopes": KIND_SLOPES}

MAX_PAYLOAD = 1 << 20      # split a large catch-up read into ~1 MB frames so the reader can paint before it all lands
# Values per point, per array kind. A frame is truncated to a MULTIPLE of this: MAX_PAYLOAD/4 is 262144 values, which
# is not divisible by 3, so a naive split ends a positions frame mid-point. A client that paints between two frames
# then holds a buffer whose length is not a multiple of 3 and reads xyz off the end into undefined — which surfaced
# as NaN view bounds and an opaque "@math.gl/web-mercator: assertion failed" from deck.gl, not as anything nearby.
VALUES_PER_POINT = {"positions": 3, "slopes": 2}
POLL_S = 0.25              # sidecar re-stat interval while the build runs; cheap (one stat per mission per kind)
MAX_WAIT_S = 1800.0        # hard stop, so a build that dies without ever setting a terminal status cannot pin a thread
MAX_FINAL_SWEEPS = 4       # once the build is terminal the sidecars are static, so a sweep that keeps producing is a
                           # bug (a rewrite loop) — bound it rather than letting it hold the connection to MAX_WAIT_S


def frame(kind: int, mission_id: int, payload: bytes) -> bytes:
    return HEADER.pack(kind, mission_id, 0, len(payload)) + payload


def control(obj: dict) -> bytes:
    return frame(KIND_CONTROL, 0, json.dumps(obj).encode())


def iter_frames(buf: bytes):
    """Decode a byte string into (kind, mission_id, payload). Used by the tests and by anything replaying a capture;
    the browser reader in adapter.js is the same state machine over a chunked stream."""
    off = 0
    while off + HEADER.size <= len(buf):
        kind, mission, _flags, n = HEADER.unpack_from(buf, off)
        off += HEADER.size
        if off + n > len(buf):
            raise ValueError("truncated frame payload")
        yield kind, mission, buf[off:off + n]
        off += n
    if off != len(buf):
        raise ValueError("trailing bytes are not a whole frame header")


def _sidecar_state(scene_id: str, mission: str, kind: str) -> tuple[int, int]:
    """(inode, n_values) for one sidecar. Inode is the identity test — see the module docstring on `reset`."""
    try:
        st = cache.scene_array_path(scene_id, mission, kind).stat()
    except OSError:
        return (0, 0)
    return (st.st_ino, st.st_size // cache.ARRAY_DTYPE.itemsize)


def scene_is_done(scene_id: str) -> bool:
    """Terminal when the registry says so AND no job is still running for the scene. Imported lazily: api imports
    plenty, and this module must stay importable from a test that never touches the build path."""
    from . import api

    rec = api._registry().get(scene_id)
    if rec is None:
        return False
    if any(j.get("scene_id") == scene_id and j.get("status") == "running" for j in api._jobs.values()):
        return False
    return rec.get("status") in ("ready", "error")


def frames(scene_id: str, cursors: dict[str, int] | None = None, *, limit: int | None = None, is_done=None,
           poll_s: float = POLL_S,
           max_wait_s: float = MAX_WAIT_S, sleep=time.sleep, now=time.monotonic):
    """Yield the scene's point frames as they land, then a terminal `done` control frame.

    `cursors` resumes a dropped connection or a page reload: {"ATL06:positions": n_values_already_held}. Anything the
    client already has is not re-sent — the same job seriesVersion + chunk arithmetic does in the pull transport, but
    as one integer per array rather than a state machine.

    `limit` is the client's per-mission point budget, DECLARED rather than enforced by hanging up. A client that just
    stops reading does not stop the server: with a reverse proxy in front, the origin keeps draining into the proxy's
    buffer and produces the whole scene for a client that wanted a sixth of it. Measured — it is what made a capped
    A/B run 4x slower than it should have been, by contending with its own abandoned streams.

    Ends only when `is_done()` reports terminal AND a full sweep produced nothing new, so points written between the
    last sweep and the status flip are never dropped.
    """
    is_done = (lambda: scene_is_done(scene_id)) if is_done is None else is_done
    cursors = dict(cursors or {})
    inodes: dict[str, int] = {}
    mission_ids: dict[str, int] = {}
    started = now()
    final_sweeps = 0
    surface_sent = False

    yield control({"t": "init", "scene_id": scene_id, "cursors": cursors})

    while True:
        done = is_done()
        sent = False

        doc = cache.load_scene(scene_id) or {}

        # DEM surface: static once it lands, so send it exactly once. It rides the stream rather than a second
        # request because it is the ONLY other bulk array the viewer needs — carrying it here is what lets the
        # browser drop the chunked/base64 transport entirely. NaN marks a nodata cell, as it does on disk.
        surf = doc.get("surface")
        if surf and not surface_sent:
            z = np.asarray([np.nan if v is None else v for v in (surf.get("z") or [])], dtype=cache.ARRAY_DTYPE)
            yield control({"t": "surface", **{k: v for k, v in surf.items() if k != "z"}, "n_values": int(z.size)})
            for off in range(0, z.size, MAX_PAYLOAD // cache.ARRAY_DTYPE.itemsize):
                piece = z[off:off + MAX_PAYLOAD // cache.ARRAY_DTYPE.itemsize]
                yield frame(KIND_SURFACE, 0, np.ascontiguousarray(piece).tobytes())
            surface_sent = True
            sent = True

        # Announce any new missions, then collect the outstanding work. Missions appear as their legs land, so the
        # table is built incrementally rather than declared up front. Ids are assigned once and never reused: the
        # reader indexes its buffers by them.
        work = []
        for mission in sorted(doc.get("series") or {}):
            if mission not in mission_ids:
                mission_ids[mission] = len(mission_ids) + 1
                series = doc["series"][mission]
                yield control({"t": "mission", "id": mission_ids[mission], "name": mission,
                               "color": series.get("color"), "n": series.get("n")})
                sent = True
            mid = mission_ids[mission]

            for kind, frame_kind in ARRAY_KINDS.items():
                key = f"{mission}:{kind}"
                ino, total = _sidecar_state(scene_id, mission, kind)
                if not ino:
                    continue
                per_point = VALUES_PER_POINT[kind]
                if limit is not None:
                    # the budget is in POINTS, so positions and slopes stay aligned with each other
                    total = min(total, limit * per_point)
                if inodes.get(key, ino) != ino:
                    # The file was replaced under us (finalize swapping in the authoritative series). Everything the
                    # client holds for this array is stale — say so and start it over.
                    yield control({"t": "reset", "mission": mission, "kind": kind})
                    cursors[key] = 0
                    sent = True
                inodes[key] = ino
                if cursors.get(key, 0) < total:
                    work.append([mission, mid, kind, frame_kind, ino, total, per_point, key])

        # ROUND-ROBIN, one frame per array per pass. Draining each array in turn meant the small missions waited out
        # the large one: sorted() visits ATL06 first, and on a real scene that is tens of MB, so GLAS (22k points) and
        # ICESSN (71k) — about 1 MB between them, and the whole reason the view is cross-mission — did not appear
        # until ICESat-2 had finished. Interleaved, they complete in the first pass or two.
        while work:
            still = []
            for item in work:
                mission, mid, kind, frame_kind, ino, total, per_point, key = item
                start = cursors.get(key, 0)
                room = (MAX_PAYLOAD // cache.ARRAY_DTYPE.itemsize) // per_point * per_point   # whole points only
                piece = cache.scene_array_read(scene_id, mission, kind, start, min(total - start, room))
                # Re-stat AFTER the read: a replacement mid-read would have handed us bytes from the new file at an
                # offset that means nothing in it. Drop the piece; the next sweep sees the inode change and resets.
                if _sidecar_state(scene_id, mission, kind)[0] != ino or piece.size == 0:
                    continue
                yield frame(frame_kind, mid, piece.tobytes())
                cursors[key] = start + int(piece.size)
                sent = True
                if cursors[key] < total:
                    still.append(item)
            work = still

        if done:
            # Terminal status is observed BEFORE the sweep, so this sweep saw everything the build wrote — but keep
            # going while it is still producing, because the last granules can land between two sweeps. Bounded: a
            # build that is done cannot legitimately keep writing.
            final_sweeps += 1
            if not sent or final_sweeps >= MAX_FINAL_SWEEPS:
                yield control({"t": "done", "cursors": cursors, "missions": mission_ids,
                               "drained": not sent})
                return
        if now() - started > max_wait_s:
            yield control({"t": "done", "cursors": cursors, "missions": mission_ids, "timeout": True})
            return
        if not sent:
            sleep(poll_s)


def parse_limit(spec: str | None) -> int | None:
    """`?limit=400000` -> 400000. Junk means no limit: a bad budget must degrade to "send everything", never to a
    failed stream."""
    spec = (spec or "").strip()
    return int(spec) if spec.isdigit() and int(spec) > 0 else None


def parse_cursors(spec: str | None) -> dict[str, int]:
    """`?from=ATL06:positions=12000,GLAS:positions=900` -> {"ATL06:positions": 12000, ...}. Unparseable entries are
    dropped rather than raising: a bad cursor must degrade to "send it all", never to a failed stream."""
    out: dict[str, int] = {}
    for item in (spec or "").split(","):
        name, _, value = item.partition("=")
        name = name.strip()
        if not name or not value.strip().isdigit():
            continue
        if len(name.split(":")) == 2 and name.split(":")[1] in ARRAY_KINDS:
            out[name] = int(value)
    return out
