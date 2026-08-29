"""Wipe data/ and print the exact commands to rebuild the indexes it destroyed.

The thing that makes this dangerous is not the deletion — the lake, cache and raw downloads are all re-fetchable —
it is that the parameters needed to REBUILD the indexes live inside the directory being deleted. Each index carries a
`_build.json` naming the bbox, resolution and granule target it was built over; without those you are guessing at the
region afterwards. So this reads them first, prints them, and hands them back as ready-to-run commands.

Deleting data/scenes also breaks every saved scene URL, which is the only user-visible loss here.

    uv run python scripts/reset_data.py                     # inventory + plan, deletes nothing
    uv run python scripts/reset_data.py --keep scenes       # ... preserving saved scenes
    uv run python scripts/reset_data.py --yes               # actually delete

Stop the web service first: it holds meta.duckdb open and its background lake writers may be mid-write.
    sudo systemctl stop aicesat-web
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from aicesat import cache

SUBDIRS = ("lake", "index", "cache", "scenes", "raw")
# collection -> (index subdir, the build script that rebuilds it)
INDEXES = {
    "ATL06": ("atl06", "scripts/build_atl06_index.py"),
    "GLAS": ("glas", "scripts/build_glas_index.py"),
    "ICESSN": ("icessn", "scripts/build_icessn_index.py"),
    "ATL03": ("atl03", "scripts/build_index.py"),
}


def _du(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _n_files(p: Path) -> int:
    return sum(1 for _ in p.rglob("*")) if p.exists() else 0


def _build_params(root: Path) -> dict:
    """The bbox/res each index was built over — the one thing here that cannot be reconstructed after deletion."""
    out = {}
    for coll, (sub, script) in INDEXES.items():
        d = root / "index" / sub
        for mf in sorted(d.glob("res*/_build.json")) if d.exists() else []:
            try:
                b = json.loads(mf.read_text())
            except Exception as e:
                out[coll] = {"error": f"unreadable {mf}: {e}"}
                continue
            out[coll] = {"bbox": b.get("bbox"), "res": b.get("res"), "target": b.get("target"), "script": script,
                         "granule_files": len(list(mf.parent.glob("*.parquet")))}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default="", help="comma-separated subdirs to preserve, e.g. 'scenes' or 'scenes,cache'")
    ap.add_argument("--yes", action="store_true", help="actually delete; without it this only prints the plan")
    args = ap.parse_args()

    root = cache.DATA_DIR
    keep = {k.strip() for k in args.keep.split(",") if k.strip()}
    bad = keep - set(SUBDIRS)
    if bad:
        raise SystemExit(f"unknown --keep {sorted(bad)}; choose from {list(SUBDIRS)}")
    if not root.exists():
        raise SystemExit(f"no data directory at {root}")

    params = _build_params(root)
    print(f"data root: {root}\n")
    print(f"{'subdir':<10}{'size':>12}{'files':>12}   action")
    total = 0
    for sub in SUBDIRS:
        p = root / sub
        n, sz = _n_files(p), _du(p)
        act = "KEEP" if sub in keep else ("delete" if p.exists() else "-")
        if sub not in keep:
            total += sz
        print(f"{sub:<10}{sz/1e9:>11.2f}G{n:>12,}   {act}")
    print(f"\nfrees ~{total/1e9:.1f} GB")

    print("\nindex build parameters (READ THESE — they live inside what is being deleted):")
    if not params:
        print("  none found; no _build.json under data/index/*/res*/")
    for coll, b in params.items():
        print(f"  {coll:<7} bbox={b.get('bbox')} res={b.get('res')} target={b.get('target')} "
              f"files={b.get('granule_files')}")

    if "scenes" not in keep and (root / "scenes").exists():
        n = len(list((root / "scenes").glob("*.json")))
        print(f"\n!! data/scenes goes too: {n} saved scene doc(s). Every existing /#scene/<id> URL stops resolving.")

    print("\nrebuild after the wipe:")
    for coll, b in params.items():
        bb = b.get("bbox")
        if bb and b.get("res") is not None:
            print(f"  uv run python {b['script']} {' '.join(str(x) for x in bb)} {b['res']} 8")
        else:
            print(f"  # {coll}: no usable bbox/res recorded — pick the region by hand")
    print("  # each build now rolls up its coverage manifest when it finishes (coverage.build_manifest)")

    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to do it.")
        return

    for sub in SUBDIRS:
        p = root / sub
        if sub in keep or not p.exists():
            continue
        shutil.rmtree(p)
        print(f"deleted {p}")
    # The build scripts and the server both expect the root to exist.
    root.mkdir(parents=True, exist_ok=True)
    print(f"\ndone. {root} is now: {sorted(x.name for x in root.iterdir()) or 'empty'}")
    print("restart the service once the indexes are rebuilt:  sudo systemctl start aicesat-web")


if __name__ == "__main__":
    main()
