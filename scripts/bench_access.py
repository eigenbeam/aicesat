"""Access-method benchmark (spec Appendix C.3): same bbox, same granules, same photon subset, MEASURED not modelled.

usage: uv run scripts/bench_access.py --methods index,legacy,download [--region egig_west_flank] [--granules 8] [--keep-raw]
Results are merged into data/bench/results.json (one entry per method) and printed as a table.

Metrics per method: granules opened / authenticated, HDF5 structure parses (file opens that walk metadata),
HTTP requests, bytes transferred, wall-clock by phase, photons returned (land-ice signal, strong beams, in bbox).
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def _setup():
    global args, BENCH_DIR, bbox, window, granules, names, sizes_mb, out_path, results, log
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="index,legacy,download")
    ap.add_argument("--region", default="egig_west_flank")
    ap.add_argument("--granules", type=int, default=8)
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--out", default="data/bench/results.json")
    args = ap.parse_args()

    BENCH_DIR = Path("data/bench").resolve()
    os.environ["AICESAT_DATA_DIR"] = str(BENCH_DIR / "lake_run")  # fresh index + lake so 'cold' is honest

    global atl03, auth, coverage, regions
    from aicesat import atl03, auth, coverage, regions  # noqa: E402  (after env)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("fsspec", "urllib3", "earthaccess"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("bench")

    bbox = regions.resolve_bbox(args.region)
    window = regions.DEFAULT_ATL03_WINDOW
    auth.login()
    granules = coverage.search(coverage.ATL03_SHORT_NAME, coverage.ATL03_VERSION, bbox, window)[: args.granules]
    names = [g["meta"]["native-id"] for g in granules]
    sizes_mb = {g["meta"]["native-id"]: float(g.size()) for g in granules}
    out_path = Path(args.out)
    results = json.loads(out_path.read_text()) if out_path.exists() else {}



def record(method: str, **kw):
    kw.update(method=method, region=args.region, bbox=list(bbox), window=list(window), n_granules=len(names), granules=names,
              measured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    results[method] = kw
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=1, default=str))
    log.info("recorded %s: %s", method, {k: v for k, v in kw.items() if k in ("wall_s", "requests", "bytes", "photons", "hdf5_opens")})


# ----------------------------------------------------------------------------- shared subset (segment-index slicing)
def subset_with_h5py(f, bbox) -> int:
    """The whole-granule baseline: strong beams, land-ice conf >= 3, bbox clip. Returns photon count."""
    sc = int(f["orbit_info/sc_orient"][0])
    n = 0
    for b in atl03.strong_beams(sc):
        if b in f and f"{b}/geolocation" in f and f"{b}/heights" in f:  # Harmony subsets omit empty beams' groups
            d = atl03._extract_beam(f, b, bbox)
            if d is not None:
                n += int(d["lon"].size)
                CHECK["lat"] += float(d["lat"].sum()); CHECK["h"] += float(d["h"].sum())
    return n


CHECK = {"lat": 0.0, "h": 0.0}  # cross-arm agreement: sums of the returned subset (spike: agree before timing counts)


def checksum():
    c = {"lat_sum": round(CHECK["lat"], 1), "h_sum": round(CHECK["h"], 0)}
    CHECK["lat"] = CHECK["h"] = 0.0
    return c


# ----------------------------------------------------------------------------- C. index + byte-range + lake
def run_index():
    from aicesat import index, lake, planner

    t0 = time.time()
    cells = planner.cells_for_bbox(bbox, res=index.H3_RES)
    idx = index.ensure_index(granules, cells=cells)         # one-time structure parse, amortized
    index.write_build_manifest(index.ATL03_INDEX_DIR, bbox, index.H3_RES, window, len(granules), cells=cells)
    t_index = time.time() - t0
    t1 = time.time()
    plan = planner.ensure(bbox, window)                     # was max_granules=len(granules): already every granule
    t_fetch = time.time() - t1
    t2 = time.time()
    q = lake.query_photons(bbox, plan["cells"], atl03.MIN_CONF, granules=plan["granules"])
    t_query = time.time() - t2
    st = plan["stats"]
    record("index_cold", label="H3 chunk index + byte-range GETs + Parquet lake (first touch)",
           wall_s=round(t_index + t_fetch + t_query, 1), phases={"index_build_s": round(t_index, 1), "fetch_materialize_s": round(t_fetch, 1), "query_s": round(t_query, 2)},
           requests=st["requests"], bytes=st["bytes"], hdf5_opens=len(idx["built"]), structure_parses=len(idx["built"]),
           hdf5_opens_at_query_time=0, granules_touched=st["granules_touched"], chunks=st["chunks_fetched"], photons=int(q["lon"].size),
           spans=st.get("spans"), gap_bytes=st.get("gap_bytes"), dataset_ranges=st.get("chunks"),
           fetch_seconds=st.get("fetch_seconds"), decode_materialize_seconds=st.get("decode_materialize_seconds"),
           chunks_pruned_by_boxes=st.get("chunks_pruned_by_boxes"),
           checksum={"lat_sum": round(float(q["lat"].sum()), 1), "h_sum": round(float(q["h"].sum()), 0)},
           notes="index build opens each granule once (amortized over all future queries); query-time path never opens HDF5. "
                 "Photon count uses the exact per-photon bbox predicate over whole chunks.")
    # warm: everything already materialized
    t3 = time.time()
    plan2 = planner.ensure(bbox, window)
    q2 = lake.query_photons(bbox, plan2["cells"], atl03.MIN_CONF, granules=plan2["granules"])
    record("index_warm", label="same, second query (lake warm)", wall_s=round(time.time() - t3, 2),
           requests=plan2["stats"]["requests"], bytes=plan2["stats"]["bytes"], hdf5_opens=0, structure_parses=0, hdf5_opens_at_query_time=0,
           granules_touched=0, chunks=0, photons=int(q2["lon"].size), notes="exact skip: index knows every chunk touching the cells, coverage table knows what is materialized")


# ----------------------------------------------------------------------------- B. earthaccess.open + h5py over fsspec (current best practice, client-side)
def run_legacy():
    import earthaccess
    import h5py
    from fsspec.implementations.http import HTTPFile

    counter = {"requests": 0, "bytes": 0}
    orig = HTTPFile._fetch_range

    @functools.wraps(orig)
    def counted(self, start, end):
        counter["requests"] += 1
        counter["bytes"] += max(0, end - start)
        return orig(self, start, end)

    HTTPFile._fetch_range = counted
    try:
        t0 = time.time()
        files = earthaccess.open(granules, show_progress=False)
        t_open = time.time() - t0
        n, t1 = 0, time.time()
        for fobj in files:
            with h5py.File(fobj, "r") as f:
                n += subset_with_h5py(f, bbox)
        t_read = time.time() - t1
    finally:
        HTTPFile._fetch_range = orig
    record("legacy_remote_h5py", label="earthaccess.open + h5py over fsspec (block cache), segment-index slicing",
           wall_s=round(t_open + t_read, 1), phases={"open_s": round(t_open, 1), "read_s": round(t_read, 1)},
           requests=counter["requests"], bytes=counter["bytes"], hdf5_opens=len(files), structure_parses=len(files), hdf5_opens_at_query_time=len(files),
           granules_touched=len(files), photons=n, checksum=checksum(),
           notes="every query re-opens and re-parses each granule; bytes counted at fsspec block-cache misses (block size chosen by earthaccess)")


# ----------------------------------------------------------------------------- A. whole-granule download + local h5py (naive)
def run_download():
    import earthaccess
    import h5py

    raw = BENCH_DIR / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    paths = earthaccess.download(granules, local_path=str(raw), threads=8, show_progress=False)
    t_dl = time.time() - t0
    nbytes = sum(Path(p).stat().st_size for p in paths)
    n, t1 = 0, time.time()
    for p in paths:
        with h5py.File(p, "r") as f:
            n += subset_with_h5py(f, bbox)
    t_read = time.time() - t1
    record("download_whole_granule", label="download whole granules (8 threads) + local h5py, segment-index slicing",
           wall_s=round(t_dl + t_read, 1), phases={"download_s": round(t_dl, 1), "local_read_s": round(t_read, 1)},
           requests=len(paths) * 2, bytes=nbytes, hdf5_opens=len(paths), structure_parses=len(paths), hdf5_opens_at_query_time=len(paths),
           granules_touched=len(paths), photons=n, granule_sizes_mb=sizes_mb, checksum=checksum(),
           notes="requests = 1 auth redirect + 1 GET per granule; bytes = full files on disk")
    if not args.keep_raw:
        for p in paths:
            Path(p).unlink(missing_ok=True)


# ----------------------------------------------------------------------------- D. SlideRule (h5coro, server-side in us-west-2)
def run_sliderule():
    from sliderule import sliderule

    sliderule.init("slideruleearth.io", verbose=False)
    out = BENCH_DIR / "sliderule_atl03x.parquet"
    parms = {"srt": 3, "cnf": atl03.MIN_CONF, "spots": [1, 3, 5],                     # land ice, conf >= 3, strong beams
             "t0": f"{window[0]}T00:00:00Z", "t1": f"{window[1]}T23:59:59Z",
             "output": {"path": str(out), "format": "geoparquet", "open_on_complete": True}}
    t0 = time.time()
    gdf = sliderule.run("atl03x", parms, aoi=list(bbox), resources=names)
    wall = time.time() - t0
    nbytes = out.stat().st_size if out.exists() else 0
    n = int(len(gdf))
    record("sliderule_atl03x", label="SlideRule atl03x (h5coro server-side subsetting, public cluster)",
           wall_s=round(wall, 1), phases={"request_to_geoparquet_s": round(wall, 1)},
           requests=1, bytes=nbytes, hdf5_opens=len(names), structure_parses=len(names), hdf5_opens_at_query_time=len(names),
           granules_touched=len(names), photons=n,
           notes="client sees one logical request and the returned geoparquet; server-side h5coro opens and parses every granule per "
                 "request (opaque: bytes read from S3 are not exposed). Photon filters approximate ours (srt=3, cnf>=3, spots 1/3/5).")


# ----------------------------------------------------------------------------- E. Harmony trajectory subsetter (NSIDC, async, HDF5 back)
def run_harmony():
    import datetime as dt
    import h5py
    from harmony import BBox, Client, Collection, Request

    client = Client(token=os.environ["EARTHDATA_TOKEN"])
    req = Request(collection=Collection(id="C3326974349-NSIDC_CPRD"), spatial=BBox(*bbox),
                  temporal={"start": dt.datetime.fromisoformat(window[0]), "stop": dt.datetime.fromisoformat(window[1])},
                  granule_name=names, skip_preview=True)
    assert req.is_valid(), req.error_messages()
    outdir = BENCH_DIR / "harmony"; outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    job = client.submit(req)
    client.wait_for_processing(job, show_progress=False)
    t_proc = time.time() - t0
    t1 = time.time()
    files = [f.result() for f in client.download_all(job, directory=str(outdir), overwrite=True)]
    t_dl = time.time() - t1
    nbytes = sum(Path(f).stat().st_size for f in files)
    n, t2 = 0, time.time()
    for f in files:
        try:
            with h5py.File(f, "r") as h:
                n += subset_with_h5py(h, bbox)
        except Exception as e:
            log.warning("harmony file %s unreadable: %s", f, e)
    t_read = time.time() - t2
    record("harmony_subset", label="NSIDC Harmony trajectory subsetter (async job, spatial subset, HDF5 back) + local h5py",
           wall_s=round(t_proc + t_dl + t_read, 1), phases={"submit_to_done_s": round(t_proc, 1), "download_s": round(t_dl, 1), "local_read_s": round(t_read, 1)},
           requests=len(files) + 2, bytes=nbytes, hdf5_opens=len(files), structure_parses=len(files), hdf5_opens_at_query_time=len(files),
           granules_touched=len(files), photons=n, files=[Path(f).name for f in files], checksum=checksum(),
           notes="server-side subsetting is opaque; latency is queue + processing. Subset files carry all variables (no variable subsetting for ATL03).")
    if not args.keep_raw:
        for f in files:
            Path(f).unlink(missing_ok=True)


RUNNERS = {"index": run_index, "legacy": run_legacy, "download": run_download, "sliderule": run_sliderule, "harmony": run_harmony}

if __name__ == "__main__":  # guard: the index build spawns worker processes that re-import this file
    _setup()
    for m in args.methods.split(","):
        log.info("=== %s ===", m)
        RUNNERS[m]()

    # ----------------------------------------------------------------------------- table
    print(f"\nAccess-method comparison — {args.region} {list(bbox)}, {len(names)} ATL03 v007 granules, measured {datetime.now():%Y-%m-%d}\n")
    print(f"{'method':<26}{'granules':>9}{'HDF5 opens':>12}{'requests':>10}{'MB':>10}{'wall s':>9}{'photons':>12}")
    for k, r in results.items():
        print(f"{k:<26}{r.get('granules_touched', ''):>9}{r.get('hdf5_opens_at_query_time', r.get('hdf5_opens', '')):>12}{r['requests']:>10}{r['bytes'] / 1e6:>10.0f}{r['wall_s']:>9}{r.get('photons', ''):>12}")
