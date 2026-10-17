"""Where does an index build spend its time, and how does it scale with workers?

Builds the same N granules of one collection into a SCRATCH data directory under several configurations and reports,
per configuration: wall, granules/s, seconds per granule per worker, CPU busy (so latency-bound and CPU-bound are told
apart), and per-granule I/O split into the two paths a builder has — block-cache reads (h5py walking the HDF5 structure
through s3fs, counted at S3File._fetch_range) and range GETs (RangeReader.fetch, the position arrays). The numbers
that motivated this (2026-10-17, m6i.xlarge, ATL06 over Summit, 100 granules):

    workers   wall    gran/s   s/gran/worker   cpu busy      <- latency-bound to 16, CPU-bound past it
        4     81.0     1.23        3.24           20%
        8     41.9     2.39        3.35           44%
       16     26.2     3.81        4.20           80%
       32     26.2     3.81        8.39           92%

    per granule: ~66 block-cache GETs (2.0 s, serial) + 12 range GETs (0.5 s) + ~0.3 s CPU

Each configuration runs in its own subprocess with its own AICESAT_DATA_DIR subdirectory, so nothing is skipped as
"already built" and the production index is never touched: the script REFUSES to run unless AICESAT_DATA_DIR names
a scratch directory. The granule list comes from one CMR search (cached under the scratch dir) and is pickled once, so
every configuration builds the identical set.

    AICESAT_DATA_DIR=/tmp/bench uv run python scripts/bench_build_io.py --collection ATL06 \
        --bbox -38.9 72.5 -38.2 72.75 --granules 100 --workers 4 8 16 [--block-size 1 4 16] [--whole-object on off] \
        [--profile 4]

--block-size sweeps the fsspec block size (MiB) h5py reads the structure through. --whole-object on|off toggles the
in-region whole-object read (AICESAT_WHOLE_OBJECT_MAX_BYTES; "off" sets it to 0). --profile N runs N granules in-process
under cProfile after one warm-up granule and prints the top functions by cumulative and internal time. GPSTRUTH is not
supported (its builder needs the traverse-metadata granule resolved first; see scripts/build_gpstruth_index.py).
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

PRODUCTION_DIRS = ("/opt/aicesat/data",)
WHOLE_OBJECT_ENV = "AICESAT_WHOLE_OBJECT_MAX_BYTES"


def _scratch_dir() -> Path:
    d = os.environ.get("AICESAT_DATA_DIR")
    if not d:
        raise SystemExit("set AICESAT_DATA_DIR to a scratch directory; this script builds index files")
    p = Path(d).resolve()
    repo_data = (Path(__file__).resolve().parents[1] / "data").resolve()
    if p == repo_data or any(str(p).startswith(x) for x in PRODUCTION_DIRS):
        raise SystemExit(f"refusing to build into {p}: that is a production data directory")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _collection(key: str):
    """(short_name, version, window, res, builder) for a collection; builders are looked up at call time."""
    from aicesat import coverage, index, index_atl06, index_gedi, index_glas, index_icessn, regions
    table = {
        "ATL06": ("ATL06", "007", None, index_atl06.ATL06_RES, lambda: index_atl06.build_atl06_index),
        "ATL03": ("ATL03", "007", regions.DEFAULT_ATL03_WINDOW, index.H3_RES, lambda: index.build_granule_index),
        "GLAS": (coverage.GLAS_SHORT_NAME, coverage.GLAS_VERSION, None, index_glas.GLAS_RES, lambda: index_glas.build_glas_index),
        "GEDI": ("GEDI02_A", "003", None, index_gedi.GEDI_RES, lambda: index_gedi.build_gedi_index),
        "ICESSN": (coverage.ICESSN_SHORT_NAME, coverage.ICESSN_VERSION, None, index_icessn.ICESSN_RES, lambda: index_icessn.build_icessn_index),
    }
    if key not in table:
        raise SystemExit(f"collection {key} not supported; choose from {sorted(table)}")
    return table[key]


# --- per-process I/O counters (patched in at worker start) --------------------------------------------------------
_IO = {"block_gets": 0, "block_bytes": 0, "range_gets": 0, "range_bytes": 0}


def _patch_io(block_size_mib: int | None) -> None:
    """Count the two I/O paths and, if asked, change the block size h5py reads the structure through."""
    from aicesat import access
    try:
        import s3fs

        orig_fr = s3fs.S3File._fetch_range

        def counted_fetch_range(self, start, end):
            _IO["block_gets"] += 1
            _IO["block_bytes"] += max(0, int(end) - int(start))
            return orig_fr(self, start, end)

        s3fs.S3File._fetch_range = counted_fetch_range
    except Exception:
        pass
    orig_fetch = access.RangeReader.fetch

    def counted_fetch(self, url, ranges):
        before = (self.stats.requests, self.stats.bytes)
        out = orig_fetch(self, url, ranges)
        _IO["range_gets"] += self.stats.requests - before[0]
        _IO["range_bytes"] += self.stats.bytes - before[1]
        return out

    access.RangeReader.fetch = counted_fetch
    if block_size_mib:
        orig_open = access.cloud_hdf5_file
        access.cloud_hdf5_file = functools.partial(orig_open, block_size=block_size_mib << 20)


def _job(key: str, granule, res: int, cells, block_size_mib: int | None):
    """One granule in a worker: build it, return (name, rows, err, io counters delta)."""
    if not _IO.get("_patched"):
        _patch_io(block_size_mib)
        _IO["_patched"] = True
    from aicesat import coverage
    before = dict(_IO)
    try:
        t = _collection(key)[4]()(granule, res=res, cells=cells)
        name, rows, err = coverage.granule_name(granule), t.num_rows, None
    except Exception as e:  # noqa: BLE001 - the bench reports failures, it does not stop on them
        name, rows, err = "?", 0, f"{type(e).__name__}: {e}"
    delta = {k: _IO[k] - before.get(k, 0) for k in ("block_gets", "block_bytes", "range_gets", "range_bytes")}
    return name, rows, err, delta


def _cpu_snapshot():
    """(total jiffies, idle jiffies) from /proc/stat, or None where there is no /proc."""
    try:
        v = list(map(int, open("/proc/stat").readline().split()[1:]))
        return sum(v), v[3] + v[4]
    except Exception:
        return None


def _run_one(pkl: Path, workers: int, block_size_mib: int | None) -> dict:
    """Child process: build every pickled granule with `workers` processes; print one JSON line."""
    import concurrent.futures as cf
    import multiprocessing

    from aicesat import auth
    d = pickle.load(open(pkl, "rb"))
    key, granules, cells, res = d["key"], d["granules"], d["cells"], d["res"]
    auth.login()
    c0, t0 = _cpu_snapshot(), time.time()
    ok = err = rows = 0
    io = {"block_gets": 0, "block_bytes": 0, "range_gets": 0, "range_bytes": 0}
    errs = []
    fn = functools.partial(_job, key, res=res, cells=cells, block_size_mib=block_size_mib)
    with cf.ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as ex:
        for name, n, e, delta in ex.map(fn, granules, chunksize=1):
            if e:
                err += 1
                errs.append(f"{name}: {e[:100]}")
            else:
                ok += 1
                rows += n
            for k in io:
                io[k] += delta[k]
    wall = time.time() - t0
    c1 = _cpu_snapshot()
    busy = None
    if c0 and c1 and c1[0] > c0[0]:
        busy = round((1 - (c1[1] - c0[1]) / (c1[0] - c0[0])) * 100)
    n = len(granules)
    print(json.dumps({"workers": workers, "block_mib": block_size_mib, "wall": round(wall, 1),
                      "gran_per_s": round(n / wall, 2), "s_per_gran_worker": round(wall / n * workers, 2),
                      "cpu_busy_pct": busy, "ok": ok, "err": err, "rows": rows,
                      "block_gets_per_gran": round(io["block_gets"] / n, 1), "block_mb_per_gran": round(io["block_bytes"] / n / 1e6, 1),
                      "range_gets_per_gran": round(io["range_gets"] / n, 1), "range_mb_per_gran": round(io["range_bytes"] / n / 1e6, 1),
                      "errors": errs[:3]}))
    return {}


def _profile(pkl: Path, n: int, block_size_mib: int | None) -> None:
    """In-process: one warm-up granule, then N granules under cProfile."""
    import cProfile
    import io
    import pstats

    from aicesat import auth
    d = pickle.load(open(pkl, "rb"))
    key, granules, cells, res = d["key"], d["granules"], d["cells"], d["res"]
    auth.login()
    _patch_io(block_size_mib)
    build = _collection(key)[4]()
    build(granules[0], res=res, cells=cells)
    pr = cProfile.Profile()
    t0, c0 = time.time(), time.process_time()
    before = dict(_IO)
    pr.enable()
    for g in granules[1:1 + n]:
        build(g, res=res, cells=cells)
    pr.disable()
    print(f"\n{n} granules in-process: wall {time.time() - t0:.1f}s, process CPU {time.process_time() - c0:.1f}s, "
          f"block GETs {(_IO['block_gets'] - before['block_gets']) / n:.0f}/gran ({(_IO['block_bytes'] - before['block_bytes']) / n / 1e6:.1f} MB), "
          f"range GETs {(_IO['range_gets'] - before['range_gets']) / n:.0f}/gran ({(_IO['range_bytes'] - before['range_bytes']) / n / 1e6:.1f} MB)")
    for key_, limit in (("cumulative", 25), ("tottime", 15)):
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats(key_).print_stats(limit)
        print("\n".join(line for line in s.getvalue().splitlines() if line.strip())[:5000])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--collection", default="ATL06")
    ap.add_argument("--bbox", nargs=4, type=float, default=[-38.9, 72.5, -38.2, 72.75], metavar=("W", "S", "E", "N"))
    ap.add_argument("--granules", type=int, default=100)
    ap.add_argument("--workers", nargs="*", type=int, default=[8])
    ap.add_argument("--block-size", nargs="*", type=int, default=[None], metavar="MiB")
    ap.add_argument("--whole-object", nargs="*", choices=["on", "off"], default=["on"])
    ap.add_argument("--profile", type=int, default=0, metavar="N")
    ap.add_argument("--_run", nargs=3, metavar=("PKL", "WORKERS", "BLOCK"), help=argparse.SUPPRESS)
    ap.add_argument("--_profile", nargs=3, metavar=("PKL", "N", "BLOCK"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if a._run:
        pkl, w, b = a._run
        _run_one(Path(pkl), int(w), int(b) if b not in ("None", "0") else None)
        return 0
    if a._profile:
        pkl, n, b = a._profile
        _profile(Path(pkl), int(n), int(b) if b not in ("None", "0") else None)
        return 0

    scratch = _scratch_dir()
    from aicesat import auth, coverage, planner
    short, version, window, res, _ = _collection(a.collection)
    bbox = tuple(a.bbox)
    fine = planner.coverage_cells(bbox)
    ring = planner.search_polygon(fine)
    cells = planner.addressing_cells(fine, res)
    auth.login()
    granules = coverage.search(short, version, bbox, window, polygon=ring)
    granules = sorted(granules, key=coverage.granule_name)[:a.granules]
    if not granules:
        raise SystemExit(f"no {a.collection} granules over {bbox}")
    sizes = []
    for g in granules:
        try:
            sizes.append(float(g.size()))
        except Exception:
            pass
    pkl = scratch / f"granules_{a.collection}.pkl"
    pickle.dump({"key": a.collection, "granules": granules, "cells": cells, "res": res}, open(pkl, "wb"))
    print(f"{a.collection}: {len(granules)} granules over {bbox}, {len(cells)} addressing cells"
          + (f"; sizes MB min {min(sizes):.0f} median {sorted(sizes)[len(sizes) // 2]:.0f} max {max(sizes):.0f}" if sizes else ""))
    if a.profile:
        subprocess.run([sys.executable, __file__, "--_profile", str(pkl), str(a.profile), str(a.block_size[0] or 0)],
                       env={**os.environ, "AICESAT_DATA_DIR": str(scratch / "profile")}, check=False)
        return 0
    hdr = f"{'whole':>5} {'block':>5} {'workers':>7} {'wall s':>7} {'gran/s':>7} {'s/g/w':>6} {'cpu%':>5} {'ok':>4} {'err':>4} " \
          f"{'blkGET/g':>8} {'blkMB/g':>8} {'rngGET/g':>8} {'rngMB/g':>8}"
    print(hdr)
    for whole in a.whole_object:
        for block in a.block_size:
            for w in a.workers:
                sub = scratch / f"run_{a.collection}_{whole}_b{block or 'def'}_w{w}"
                if sub.exists():
                    import shutil
                    shutil.rmtree(sub)
                env = {**os.environ, "AICESAT_DATA_DIR": str(sub)}
                if whole == "off":
                    env[WHOLE_OBJECT_ENV] = "0"
                out = subprocess.run([sys.executable, __file__, "--_run", str(pkl), str(w), str(block or 0)],
                                     env=env, capture_output=True, text=True, check=False)
                lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
                if not lines:
                    print(f"{whole:>5} {str(block or 'def'):>5} {w:>7}  FAILED: {out.stderr.strip().splitlines()[-1][:120] if out.stderr.strip() else out.stdout[-200:]}")
                    continue
                r = json.loads(lines[-1])
                print(f"{whole:>5} {str(block or 'def'):>5} {w:>7} {r['wall']:>7} {r['gran_per_s']:>7} {r['s_per_gran_worker']:>6} "
                      f"{str(r['cpu_busy_pct'] if r['cpu_busy_pct'] is not None else '-'):>5} {r['ok']:>4} {r['err']:>4} "
                      f"{r['block_gets_per_gran']:>8} {r['block_mb_per_gran']:>8} {r['range_gets_per_gran']:>8} {r['range_mb_per_gran']:>8}")
                for e in r["errors"]:
                    print(f"      err: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
