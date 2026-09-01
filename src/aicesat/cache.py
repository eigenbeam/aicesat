"""On-disk cache for extracted samples (npz + json sidecar) and scene documents."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

DATA_DIR = Path(os.environ.get("AICESAT_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
CACHE_DIR = DATA_DIR / "cache"
SCENE_DIR = DATA_DIR / "scenes"


def key(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def load(k: str) -> tuple[dict[str, np.ndarray], dict] | None:
    npz, meta = CACHE_DIR / f"{k}.npz", CACHE_DIR / f"{k}.json"
    if not (npz.exists() and meta.exists()):
        return None
    with np.load(npz, allow_pickle=False) as z:
        arrays = {n: z[n] for n in z.files}
    return arrays, json.loads(meta.read_text())


NPZ_COMPRESSLEVEL = 1   # see save(): level 1 is ~all of the compression at ~a third of the CPU


def save(k: str, arrays: dict[str, np.ndarray], meta: dict) -> None:
    """Write an extract to the content-addressed cache as a standard .npz.

    Hand-rolled instead of np.savez_compressed only to set the deflate level, which numpy does not expose. On a 1.7M
    point extract (the ATL06 case on the deployed box) the default level 6 costs 2.6 s; level 1 costs 1.0 s and
    produces a file the same size (35.5 vs 35.2 MB) — the coordinates are floats and barely compress past level 1.
    Storing uncompressed would be 0.03 s but 54 MB, and this cache has no eviction, so the disk is not free.
    The format is exactly numpy's, so np.load (and any already-cached level-6 file) still reads it."""
    import io
    import zipfile

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / f".{k}.npz.tmp"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=NPZ_COMPRESSLEVEL) as zf:
        for name, arr in arrays.items():
            buf = io.BytesIO()
            np.lib.format.write_array(buf, np.asanyarray(arr), allow_pickle=False)
            zf.writestr(name + ".npy", buf.getvalue())
    tmp.replace(CACHE_DIR / f"{k}.npz")     # atomic: a reader never sees a half-written cache entry
    (CACHE_DIR / f"{k}.json").write_text(json.dumps(meta, indent=1, default=str))


def scene_path(scene_id: str) -> Path:
    SCENE_DIR.mkdir(parents=True, exist_ok=True)
    return SCENE_DIR / f"{scene_id}.json"


# --- scene bulk arrays: a binary sidecar, not JSON in the doc ---------------------------------------------------
# Positions used to live in the scene doc as a JSON list of floats. That made the doc scale with point count (real
# scenes measured 14.7 and 19.0 MB), so cache.load_scene cost 120 ms and scene_part paid it once PER CHUNK REQUEST —
# 117 ms per request of which 9 ms was the actual array work. Worse, append_partial re-serialised the whole growing
# list on every streamed granule, making a build quadratic in granules.
#
# Each (scene, mission, kind) is now a flat little-endian float32 file that readers mmap and slice, and the streaming
# build APPENDS to. The doc keeps only counts. The files live under SCENE_DIR/<scene_id>/, which delete_scene already
# removes wholesale, so they are cleaned up with the scene.
ARRAY_DTYPE = np.dtype("<f4")


def scene_array_path(scene_id: str, mission: str, kind: str) -> Path:
    safe = "".join(c for c in f"{mission}.{kind}" if c.isalnum() or c in "._-")
    return SCENE_DIR / scene_id / f"{safe}.f32"


def scene_array_len(scene_id: str, mission: str, kind: str) -> int:
    p = scene_array_path(scene_id, mission, kind)
    try:
        return p.stat().st_size // ARRAY_DTYPE.itemsize
    except OSError:
        return 0


def scene_array_write(scene_id: str, mission: str, kind: str, arr) -> int:
    """Replace the array. Atomic: a poller mid-build must never mmap a half-written file."""
    a = np.ascontiguousarray(arr, dtype=ARRAY_DTYPE)
    p = scene_array_path(scene_id, mission, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(a.tobytes())
    os.replace(tmp, p)
    return int(a.size)


def scene_array_append(scene_id: str, mission: str, kind: str, arr) -> int:
    """Append, returning the new element count. This is what makes a streaming build linear instead of quadratic."""
    a = np.ascontiguousarray(arr, dtype=ARRAY_DTYPE)
    p = scene_array_path(scene_id, mission, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "ab") as f:
        f.write(a.tobytes())
    return scene_array_len(scene_id, mission, kind)


def scene_array_read(scene_id: str, mission: str, kind: str, start: int = 0, count: int | None = None) -> np.ndarray:
    """A slice of the array, memory-mapped — the whole point is not materialising the rest to serve one chunk."""
    p = scene_array_path(scene_id, mission, kind)
    n = scene_array_len(scene_id, mission, kind)
    if not n or start >= n:
        return np.empty(0, dtype=ARRAY_DTYPE)
    stop = n if count is None else min(n, start + max(0, int(count)))
    mm = np.memmap(p, dtype=ARRAY_DTYPE, mode="r", offset=start * ARRAY_DTYPE.itemsize, shape=(stop - start,))
    return np.asarray(mm)


def load_scene(scene_id: str) -> dict | None:
    p = scene_path(scene_id)
    return json.loads(p.read_text()) if p.exists() else None


def save_scene(scene_id: str, doc: dict) -> None:
    # Atomic write: a scene doc is now persisted repeatedly *during* a build while the UI polls it, so a reader must
    # never observe a half-written file. Write a temp sibling then os.replace (atomic rename on POSIX/Windows).
    p = scene_path(scene_id)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, default=_json_default))
    os.replace(tmp, p)


def _json_default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    raise TypeError(type(o))
