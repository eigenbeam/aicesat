"""Prototype PUSH transport for a scene's bulk point arrays — the stage-1 half of the two-stage design.

Why this exists. The shipped transport is a PULL loop: the browser polls `meta`, diffs a `seriesVersion`, and fetches
a bounded PREFIX of each mission's sidecar as base64 JSON chunks. Nearly every awkward part of that design exists to
make "a prefix of a growing array" a fair sample of the whole — the shuffle at write time in scene.series, the
power-of-2 thinning behind scene.PARTIAL_PREVIEW_CAP, adapter.js's DISPLAY_BUDGET, seriesVersion, and the chunk-resume
arithmetic in fetchValuesFrom. Push the bytes as they land and not one of those questions has to be answered.

This module is ADDITIVE and changes no existing behaviour: api.scene_part and the poll loop are untouched, so the two
transports can be A/B'd against the same build on the box before anything is deleted. Measure before deleting.

## Frame format (little-endian), header + payload:

    u8  kind        0 = control (payload is UTF-8 JSON), 1 = positions (f32), 2 = slopes (f32)
    u8  mission     index into the mission table built by the control frames; 0 on a control frame
    u16 flags       reserved, always 0
    u32 n_bytes     payload length in bytes

Control frames carry the small stuff (mission table, resets, done) so the reader needs exactly one code path and no
second HTTP request. Bulk arrays stay raw f32 — no base64, which is a third of the bytes on the wire.

## The sidecar is NOT append-only, and that is the point of the `reset` frame

`tail -f` would be sound if the sidecar only ever grew. It does not:
  * scene.append_partial HALVES the preview whenever it crosses PARTIAL_PREVIEW_CAP (cache.scene_array_write), and
  * scene.series REPLACES the preview wholesale with the finalized shuffled array at add_series.
Both go through os.replace, so the path gets a NEW INODE while the client holds bytes from the old one. Size alone
cannot detect it (a replacement can be larger than what we had already sent — classic ABA). So identity is the inode:
when it changes, emit `{"t":"reset"}` for that mission and restart its cursor at zero.

That makes the prototype correct against today's writer. It is also the measurement worth taking: every reset is a
sidecar rewrite the client had to throw away and refetch. In the target design — where the streamed points ARE the
series, with no preview/finalize duality — the sidecar is append-only and resets go to zero. Count them.
"""
from __future__ import annotations

import json
import struct
import time

from . import cache

HEADER = struct.Struct("<BBHI")
KIND_CONTROL, KIND_POSITIONS, KIND_SLOPES = 0, 1, 2
ARRAY_KINDS = {"positions": KIND_POSITIONS, "slopes": KIND_SLOPES}

MAX_PAYLOAD = 1 << 20      # split a large catch-up read into ~1 MB frames so the reader can paint before it all lands
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

    yield control({"t": "init", "scene_id": scene_id, "cursors": cursors})

    while True:
        done = is_done()
        sent = False

        doc = cache.load_scene(scene_id) or {}
        for mission in sorted(doc.get("series") or {}):
            if mission not in mission_ids:
                # Missions appear as their legs land, so the table is built incrementally rather than declared up
                # front. Ids are assigned once and never reused: the reader indexes its buffers by them.
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
                if limit is not None:
                    # positions are 3 values per point, slopes 2 — the budget is in POINTS, so both stay aligned
                    total = min(total, limit * (3 if kind == "positions" else 2))
                if inodes.get(key, ino) != ino:
                    # The file was replaced under us (preview thinning, or finalize swapping in the real series).
                    # Everything the client holds for this array is stale — say so and start it over.
                    yield control({"t": "reset", "mission": mission, "kind": kind})
                    cursors[key] = 0
                    sent = True
                inodes[key] = ino

                start = cursors.get(key, 0)
                while start < total:
                    count = min(total - start, MAX_PAYLOAD // cache.ARRAY_DTYPE.itemsize)
                    piece = cache.scene_array_read(scene_id, mission, kind, start, count)
                    # Re-stat AFTER the read: a replacement mid-read would have handed us bytes from the new file at
                    # an offset that means nothing in it. Drop the piece; the next sweep sees the inode change and
                    # emits the reset.
                    if _sidecar_state(scene_id, mission, kind)[0] != ino:
                        break
                    if piece.size == 0:
                        break
                    yield frame(frame_kind, mid, piece.tobytes())
                    start += int(piece.size)
                    cursors[key] = start
                    sent = True

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
