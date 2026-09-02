"""The push transport (stream.py): frames arrive in order, resume skips what the client holds, and a sidecar the
writer REPLACES under the reader is reported as a reset rather than silently corrupting the client's buffer.

The generator only sleeps when a sweep produced nothing, so the tests drive the "build" from the sleep callback: each
quiet sweep runs the next scripted step. That makes the timing deterministic — no wall-clock sleeps, no flakes.
"""
import numpy as np
import pytest

from aicesat import cache, stream


@pytest.fixture(autouse=True)
def _scene_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")


def _scene(sid="s1", missions=("ATL06",)):
    doc = {"scene_id": sid, "frame": {}, "z0": 0.0,
           "series": {m: {"mission": m, "color": [1, 2, 3], "n": 0} for m in missions}}
    cache.save_scene(sid, doc)
    return doc


def _pts(n, v=1.0):
    return np.full(n * 3, v, dtype=cache.ARRAY_DTYPE)


def _drive(sid, steps, cursors=None, done_flag=None):
    """Run the generator to completion, running one scripted step per quiet sweep. Returns the decoded frames."""
    done_flag = {"done": False} if done_flag is None else done_flag
    script = list(steps)

    def sleep(_s):
        if script:
            script.pop(0)()
        else:
            done_flag["done"] = True

    got = []
    for fr in stream.frames(sid, cursors, is_done=lambda: done_flag["done"], sleep=sleep):
        got.extend(stream.iter_frames(fr))
    assert not script, f"{len(script)} scripted steps never ran"
    return got


def _payloads(frames, kind=stream.KIND_POSITIONS):
    return np.concatenate([np.frombuffer(p, dtype=cache.ARRAY_DTYPE) for k, _m, p in frames if k == kind]) \
        if any(k == kind for k, _m, _p in frames) else np.empty(0, dtype=cache.ARRAY_DTYPE)


def _controls(frames):
    import json
    return [json.loads(p) for k, _m, p in frames if k == stream.KIND_CONTROL]


# --- codec -----------------------------------------------------------------------------------------------------
def test_frame_roundtrip():
    buf = stream.control({"t": "init"}) + stream.frame(stream.KIND_POSITIONS, 3, b"abcd")
    got = list(stream.iter_frames(buf))
    assert [(k, m) for k, m, _ in got] == [(stream.KIND_CONTROL, 0), (stream.KIND_POSITIONS, 3)]
    assert got[1][2] == b"abcd"


def test_iter_frames_rejects_a_truncated_payload():
    buf = stream.frame(stream.KIND_POSITIONS, 1, b"abcdefgh")[:-2]
    with pytest.raises(ValueError):
        list(stream.iter_frames(buf))


# --- streaming -------------------------------------------------------------------------------------------------
def test_points_stream_in_order_as_they_are_appended():
    _scene()
    steps = [lambda v=v: cache.scene_array_append("s1", "ATL06", "positions", _pts(10, v)) for v in (1.0, 2.0, 3.0)]
    frames = _drive("s1", steps)
    vals = _payloads(frames)
    assert vals.size == 90
    assert list(np.unique(vals[:30])) == [1.0] and list(np.unique(vals[60:])) == [3.0]
    assert _controls(frames)[-1]["t"] == "done"


def test_a_resumed_stream_does_not_resend_what_the_client_holds():
    _scene()
    cache.scene_array_append("s1", "ATL06", "positions", _pts(10, 1.0))
    steps = [lambda: cache.scene_array_append("s1", "ATL06", "positions", _pts(10, 2.0))]
    frames = _drive("s1", steps, cursors={"ATL06:positions": 30})
    vals = _payloads(frames)
    assert vals.size == 30, "the first 30 values were already held and must not be re-sent"
    assert list(np.unique(vals)) == [2.0]


def test_points_written_just_before_the_status_flip_are_not_dropped():
    """The build marks the scene ready AFTER its last write. A generator that stopped on the first `done` it saw
    would lose everything written since the previous sweep."""
    _scene()
    done = {"done": False}

    def finish():
        cache.scene_array_append("s1", "ATL06", "positions", _pts(5, 9.0))
        done["done"] = True

    frames = _drive("s1", [finish], done_flag=done)
    assert _payloads(frames).size == 15


def test_a_replaced_sidecar_is_reported_as_a_reset_and_resent():
    """scene.series REPLACES the preview at finalize (os.replace -> new inode). The client's bytes are then stale,
    and a size check alone cannot see it: here the replacement is SMALLER, but it is a different array entirely."""
    _scene()
    cache.scene_array_append("s1", "ATL06", "positions", _pts(20, 1.0))
    steps = [lambda: cache.scene_array_write("s1", "ATL06", "positions", _pts(8, 7.0))]
    frames = _drive("s1", steps)

    resets = [c for c in _controls(frames) if c["t"] == "reset"]
    assert len(resets) == 1 and resets[0]["mission"] == "ATL06"
    # the reader must end up holding ONLY the replacement
    after = [f for f in frames if f[0] == stream.KIND_POSITIONS]
    idx = next(i for i, f in enumerate(frames) if f[0] == stream.KIND_CONTROL and b'"reset"' in f[2])
    post = _payloads(frames[idx:])
    assert post.size == 24 and list(np.unique(post)) == [7.0]
    assert len(after) >= 2


def test_a_larger_replacement_is_still_caught():
    """The ABA case: the file is replaced by a BIGGER array, so the cursor is still < total and a size-only check
    would happily stream the new file's tail onto the old file's head."""
    _scene()
    cache.scene_array_append("s1", "ATL06", "positions", _pts(10, 1.0))
    steps = [lambda: cache.scene_array_write("s1", "ATL06", "positions", _pts(40, 5.0))]
    frames = _drive("s1", steps)
    assert [c["t"] for c in _controls(frames)].count("reset") == 1
    idx = next(i for i, f in enumerate(frames) if f[0] == stream.KIND_CONTROL and b'"reset"' in f[2])
    assert list(np.unique(_payloads(frames[idx:]))) == [5.0]


def test_missions_are_announced_as_their_legs_land():
    doc = _scene(missions=("ATL06",))
    cache.scene_array_append("s1", "ATL06", "positions", _pts(4))

    def add_glas():
        doc["series"]["GLAS"] = {"mission": "GLAS", "color": [9, 9, 9], "n": 0}
        cache.save_scene("s1", doc)
        cache.scene_array_append("s1", "GLAS", "positions", _pts(4, 2.0))

    frames = _drive("s1", [add_glas])
    names = [c["name"] for c in _controls(frames) if c["t"] == "mission"]
    assert names == ["ATL06", "GLAS"]
    ids = {c["name"]: c["id"] for c in _controls(frames) if c["t"] == "mission"}
    assert {m for _k, m, _p in frames if _k == stream.KIND_POSITIONS} == {ids["ATL06"], ids["GLAS"]}


def test_slopes_stream_alongside_positions_on_their_own_kind():
    _scene(missions=("ICESSN",))
    cache.scene_array_append("s1", "ICESSN", "positions", _pts(6))
    cache.scene_array_append("s1", "ICESSN", "slopes", np.full(12, 0.5, dtype=cache.ARRAY_DTYPE))
    frames = _drive("s1", [])
    assert _payloads(frames, stream.KIND_POSITIONS).size == 18
    assert _payloads(frames, stream.KIND_SLOPES).size == 12


def test_a_large_array_is_split_into_bounded_frames():
    """One 12 MB catch-up read must not become one 12 MB frame: the reader should be able to paint before it all
    lands, and a single write of that size is what stalls a slow client."""
    _scene()
    n_vals = 3 * stream.MAX_PAYLOAD // cache.ARRAY_DTYPE.itemsize
    cache.scene_array_append("s1", "ATL06", "positions", np.ones(n_vals, dtype=cache.ARRAY_DTYPE))
    frames = _drive("s1", [])
    bulk = [p for k, _m, p in frames if k == stream.KIND_POSITIONS]
    assert len(bulk) == 3 and max(len(p) for p in bulk) <= stream.MAX_PAYLOAD
    assert _payloads(frames).size == n_vals


def test_the_stream_ends_on_a_done_frame_carrying_resumable_cursors():
    _scene()
    cache.scene_array_append("s1", "ATL06", "positions", _pts(7))
    frames = _drive("s1", [])
    last = _controls(frames)[-1]
    assert last["t"] == "done" and last["cursors"]["ATL06:positions"] == 21


# --- cursor parsing --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spec,want", [
    ("ATL06:positions=30,GLAS:slopes=8", {"ATL06:positions": 30, "GLAS:slopes": 8}),
    ("ATL06:positions=abc", {}),
    ("ATL06:bogus=30", {}),
    ("garbage", {}),
    (None, {}),
    ("", {}),
])
def test_parse_cursors(spec, want):
    assert stream.parse_cursors(spec) == want


def test_the_stream_terminates_even_if_the_sidecar_keeps_changing_after_done():
    """Termination must not depend on a sweep going quiet. A writer that keeps rewriting a sidecar after the build is
    terminal would otherwise hold the connection (and a server thread) until MAX_WAIT_S — 30 minutes.

    The churn hangs off `now`, which the generator calls once per sweep, so every sweep sees a freshly replaced
    sidecar and therefore always has something to send."""
    _scene()
    n = {"i": 0}

    def now():
        n["i"] += 1
        cache.scene_array_write("s1", "ATL06", "positions", _pts(4, float(n["i"])))
        return 0.0                      # never trips max_wait_s: termination must come from the sweep bound

    got = []
    for fr in stream.frames("s1", is_done=lambda: True, sleep=lambda _s: None, now=now):
        got.extend(stream.iter_frames(fr))
    last = _controls(got)[-1]
    assert last["t"] == "done" and last["drained"] is False, "a churning sidecar must not hold the stream open"
    assert n["i"] <= stream.MAX_FINAL_SWEEPS + 2, f"{n['i']} sweeps after done"


def test_a_declared_limit_stops_the_server_rather_than_the_client():
    """A client with a display budget must be able to SAY so. Relying on it to hang up does not stop the server —
    behind a reverse proxy the origin keeps producing into the proxy's buffer — so the budget belongs in the request."""
    _scene()
    cache.scene_array_append("s1", "ATL06", "positions", _pts(100, 1.0))
    frames = _drive("s1", [], cursors=None)
    assert _payloads(frames).size == 300, "unlimited baseline"

    frames = list(stream.frames("s1", limit=25, is_done=lambda: True, sleep=lambda _s: None))
    decoded = [f for fr in frames for f in stream.iter_frames(fr)]
    assert _payloads(decoded).size == 75, "25 points = 75 values, and not one value more"


def test_the_limit_keeps_slopes_aligned_with_positions():
    """Slopes are 2 values per point, positions 3. A limit expressed in BYTES or VALUES would silently misalign them
    and tilt every platelet against the wrong point."""
    _scene(missions=("ICESSN",))
    cache.scene_array_append("s1", "ICESSN", "positions", _pts(50))
    cache.scene_array_append("s1", "ICESSN", "slopes", np.full(100, 0.5, dtype=cache.ARRAY_DTYPE))
    frames = [f for fr in stream.frames("s1", limit=10, is_done=lambda: True, sleep=lambda _s: None)
              for f in stream.iter_frames(fr)]
    assert _payloads(frames, stream.KIND_POSITIONS).size == 30
    assert _payloads(frames, stream.KIND_SLOPES).size == 20


@pytest.mark.parametrize("spec,want", [("400000", 400_000), ("0", None), ("-5", None), ("abc", None), (None, None)])
def test_parse_limit(spec, want):
    assert stream.parse_limit(spec) == want
