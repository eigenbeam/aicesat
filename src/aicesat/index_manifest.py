"""The sub-granule index build manifest (`_build.json`) — which regions an index actually covers.

One manifest per index directory (`data/index/<collection>/res<R>/`). It used to hold a single `bbox`,
so a second build over a different region silently clobbered the first: every Parquet from the old
region stayed on disk and would still answer perfectly, but coverage checks started failing for it.

The manifest now keeps a `regions` list, and a query is covered when it fits inside ANY ONE of them —
never inside their union. A union would claim the gap between two disjoint regions, turning an honest
"not indexed" error into a silently empty answer, which is the worse failure.

Manifests written before this change keep working: `regions()` falls back to the legacy `bbox` key.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MANIFEST = "_build.json"


def path(d) -> Path:
    return Path(d) / MANIFEST


def read(d) -> dict | None:
    """The manifest dict, or None when it is missing or unreadable."""
    p = path(d)
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text())
        return m if isinstance(m, dict) else None
    except Exception:
        return None


def _ok(b) -> bool:
    return isinstance(b, (list, tuple)) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)


def _contains(outer, inner) -> bool:
    """True if `outer` fully contains `inner` — the same W/S/E/N test the four call sites used inline."""
    w, s, e, n = inner
    return outer[0] <= w and outer[1] <= s and e <= outer[2] and n <= outer[3]


def regions(d) -> list[list[float]]:
    """Every region this index claims, newest last. Falls back to the legacy single `bbox`."""
    m = read(d)
    if not m:
        return []
    rs = m.get("regions")
    if isinstance(rs, list) and rs:
        return [[float(v) for v in r] for r in rs if _ok(r)]
    b = m.get("bbox")
    return [[float(v) for v in b]] if _ok(b) else []


def covers(d, bbox) -> bool:
    """True if bbox fits inside a single built region. Deliberately not a union of them — see module docstring."""
    try:
        return any(_contains(r, bbox) for r in regions(d))
    except Exception:
        return False


def record(d, bbox, res, target, complete: bool = False) -> dict:
    """Add `bbox` to the region list: absorb any region it contains, skip it if already covered.

    `bbox` (the legacy key) is left as the region just built, not the union of all of them — an
    un-updated reader then under-claims coverage, which errors honestly instead of answering wrongly.
    """
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    bbox = [float(v) for v in bbox]
    m = read(d) or {}
    kept = [r for r in regions(d) if not _contains(bbox, r)]      # drop regions the new one subsumes
    if not any(_contains(r, bbox) for r in kept):                 # already covered -> nothing to add
        kept.append(bbox)
    m.update({"bbox": bbox, "regions": kept, "res": int(res), "target": int(target),
              "started": time.time(), "complete": bool(complete)})
    path(d).write_text(json.dumps(m, indent=1))
    return m


def mark_complete(d) -> None:
    """Flip the manifest to complete. Only call once a build has finished without being killed —
    the manifest is written up front (so the UI can show progress), so `complete` is what tells a
    half-built index apart from a finished one after it has been copied to another machine."""
    m = read(d)
    if m is None:
        return
    m["complete"] = True
    m["finished"] = time.time()
    path(d).write_text(json.dumps(m, indent=1))


def is_complete(d) -> bool:
    """True only if a build explicitly finished here. Pre-`complete` manifests read as False."""
    m = read(d)
    return bool(m and m.get("complete"))
