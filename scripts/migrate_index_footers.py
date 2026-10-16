"""One-off: move the compacted cell list out of every index file's footer into the content-addressed sidecar.

Every granule's index parquet carried the full compacted claim list (`aicesat_index_cells`, ~49 KB for Greenland) in
its footer: 77% of an ATL06 index file, 92% of ICESSN, 89% of GPS -- 3.3 GiB of the deployed 7.05 GiB index store was
copies of one string, re-read by every query that opened footers. index.cells_metadata now writes each distinct set
once to `{index_dir}/_cellsets/{sha1}.json` and stamps the sha1. This rewrites the legacy files to match (pyarrow
cannot edit a footer in place, so each file is read, re-stamped and atomically replaced), then rolls each collection's
coverage manifest up so the directory mtime it records is current again. Idempotent; run while no builder is running.

    uv run python scripts/migrate_index_footers.py [--dry-run] [--workers 8] [--collections ATL06 GLAS ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import pyarrow.parquet as pq


def _is_legacy(path) -> bool:
    try:
        meta = pq.read_schema(path).metadata or {}
    except Exception:
        return False
    from aicesat import index
    return bool(meta.get(index.CELLS_KEY.encode())) and not meta.get(index.CELLSET_KEY.encode())


def _migrate(path) -> tuple[str, str]:
    from aicesat import index
    try:
        return ("rewritten" if index.migrate_footer(path) else "skipped"), str(path)
    except Exception as e:                    # log and leave the file alone; never delete
        return f"unreadable: {type(e).__name__}: {e}", str(path)


def _bytes(paths) -> int:
    return sum(p.stat().st_size for p in paths)


def main() -> None:
    from aicesat import coverage, index

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="count legacy footers only; write nothing")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--collections", nargs="*", default=None, help="default: every collection with an index dir")
    a = ap.parse_args()
    keys = a.collections or [c["key"] for c in coverage.collections()]
    report = {}
    for key in keys:
        d, _res, _ym = coverage._index_for(key)
        if d is None or not d.exists():
            report[key] = {"skipped": "no index directory"}
            continue
        for stale in d.glob(".*.tmp"):            # a crashed earlier run's leftovers
            stale.unlink()
        files = sorted(d.glob("*.parquet"))
        before = _bytes(files)
        t0 = time.time()
        if a.dry_run:
            n_legacy = sum(1 for p in files if _is_legacy(p))
            report[key] = {"files": len(files), "legacy_footers": n_legacy, "bytes": before,
                           "seconds": round(time.time() - t0, 1)}
            print(json.dumps({key: report[key]}), file=sys.stderr)
            continue
        counts: dict[str, int] = {}
        bad = []
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for status, path in ex.map(_migrate, files, chunksize=64):
                counts[status.split(":")[0]] = counts.get(status.split(":")[0], 0) + 1
                if status.startswith("unreadable"):
                    bad.append((path, status))
        for path, status in bad:
            print(f"{key}: left alone {path}: {status}", file=sys.stderr)
        after = _bytes(sorted(d.glob("*.parquet")))
        sidecars = sorted((d / index.CELLSETS_DIR).glob("*.json")) if (d / index.CELLSETS_DIR).exists() else []
        rollup = coverage.build_manifest(key)      # restamps dir_mtime_ns: every file's mtime just changed
        report[key] = {"files": len(files), **counts, "bytes_before": before, "bytes_after": after,
                       "cellsets": len(sidecars), "cellset_bytes": _bytes(sidecars),
                       "seconds": round(time.time() - t0, 1), "manifest": rollup}
        print(json.dumps({key: report[key]}), file=sys.stderr)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
