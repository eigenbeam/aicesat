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


def save(k: str, arrays: dict[str, np.ndarray], meta: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE_DIR / f"{k}.npz", **arrays)
    (CACHE_DIR / f"{k}.json").write_text(json.dumps(meta, indent=1, default=str))


def scene_path(scene_id: str) -> Path:
    SCENE_DIR.mkdir(parents=True, exist_ok=True)
    return SCENE_DIR / f"{scene_id}.json"


def load_scene(scene_id: str) -> dict | None:
    p = scene_path(scene_id)
    return json.loads(p.read_text()) if p.exists() else None


def save_scene(scene_id: str, doc: dict) -> None:
    scene_path(scene_id).write_text(json.dumps(doc, default=_json_default))


def _json_default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    raise TypeError(type(o))
