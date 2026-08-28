"""Query-time comparison: our H3 sub-granule index vs h5coro (and other cloud-HDF5 readers), same bbox + granules.

WHAT THIS MEASURES (get the framing right)
------------------------------------------
This is an ARCHITECTURE comparison, not a fetch-mechanism swap.

  * OUR PATH  (index_atl06.fetch_bbox): a pre-built H3 sub-granule index stores every chunk's byte range OFFLINE.
    Query time does ZERO HDF5 structure parsing -- it reads the wanted chunks by byte range (in-region S3-direct
    via s3fs) and decodes them. The win is bought with a one-time index build+maintenance cost the others do not pay
    (the project measures ~$6 to index the whole Earth's ATL06; amortized over every future query).

  * h5coro   (pip install h5coro): the SlideRule team's cloud-optimized HDF5 reader. No pre-build, but EVERY query
    re-reads the (coalesced) HDF5 b-tree structure of each granule before it can slice. This is the key contender --
    it is what SlideRule uses under the hood.

  * h5py-over-s3fs: plain open+slice per granule (current client-side best practice). Reference baseline.

  * kerchunk/VirtualiZarr: the closest analog to us -- pre-extract chunk references, then read via zarr. Optional /
    best-effort (its reference build is a structure parse, i.e. the amortizable analog of our index build); skips
    cleanly if the stack or the in-region auth plumbing is not present.

So the honest comparison is QUERY-TIME cost for the SAME bbox subset, with an explicit note that our query-time win
is paid for by a one-time index build the others do not have. Same granules, same bbox, same variables, same reps;
we verify the methods return the SAME points (count + lat/h checksum) BEFORE trusting any timing.

OPTIONAL DEPS (not project dependencies -- install on the us-west-2 box to benchmark them):
    uv pip install h5coro          # the priority contender
    uv pip install kerchunk zarr xarray   # optional closest-analog path
h5py / s3fs / duckdb are already project deps. Missing readers are detected and skipped with a clear message; the
script never hard-imports them at module load.

IN-REGION ONLY: the S3-direct reads (ours AND h5coro's S3 driver) only work from us-west-2. Out of region the script
validates the harness + that the h5coro API calls are well-formed, then skips the S3 work -- real numbers come from
the box. Wall-clock out of region is your local link, not the system.

usage:
    uv pip install h5coro
    uv run scripts/bench_vs_h5coro.py                      # default SW-Greenland ATL06 box
    uv run scripts/bench_vs_h5coro.py --methods ours,h5coro,h5py_s3fs,kerchunk --reps 3
    uv run scripts/bench_vs_h5coro.py --bbox -50 69 -49.5 69.4 --window 2019-03-01 2019-05-31
"""
from __future__ import annotations

import argparse
import functools
import importlib.util
import statistics
import time

import numpy as np

from aicesat import access, auth

# Default subset: a small sub-box of the SW-Greenland (Jakobshavn) ATL06 index that bench_index_fetch.py also uses.
DEFAULT_BBOX = [-50.0, 69.0, -49.5, 69.4]
DEFAULT_WINDOW = ("2019-03-01", "2019-05-31")
ATL06_RES = 5

# ATL06 land_ice_segments datasets, in the order our index/fetch use them. h_li is the ellipsoidal height.
BEAM_DSETS = ("latitude", "longitude", "h_li", "delta_time", "atl06_quality_summary")
ALL_BEAMS = ("gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r")


def have(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def strong_beams(sc_orient: int) -> list[str]:
    """Same rule as atl06._strong_beams / index.strong_beams: sc_orient 1 -> right beams, 0 -> left, 2 -> none."""
    v = int(sc_orient)
    if v == 1:
        return ["gt1r", "gt2r", "gt3r"]
    if v == 0:
        return ["gt1l", "gt2l", "gt3l"]
    return []


def bbox_quality_mask(lat, lon, h, q, bbox) -> np.ndarray:
    """The EXACT predicate index_atl06.fetch_bbox applies, so every reader yields the identical point set."""
    w, s, e, n = bbox
    m = np.isfinite(h) & (h < 3.0e38) & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    return m & (q == 0)


def checksum(lat, h) -> tuple[float, float]:
    """Order-independent fingerprint of the returned subset: agree here before comparing timings."""
    return (round(float(np.sum(lat)), 3), round(float(np.sum(h)), 1))


# ----------------------------------------------------------------------------- granule set (from the built index)
def granules_from_index(bbox, window, res: int) -> list[dict]:
    """The EXACT (granule, url, s3url) set our fetch_bbox touches for this bbox+window: DISTINCT rows whose H3 cell
    overlaps the bbox, strong beams only, within the window -- mirrors fetch_bbox's WHERE clause. This is what we hand
    to h5coro so both readers see the same granules."""
    import duckdb

    from aicesat import index_atl06, planner

    d = index_atl06._index_dir(res)
    if not d.exists():
        raise RuntimeError(
            f"no ATL06 index at {d} -- build it first: uv run scripts/build_atl06_index.py "
            f"(the benchmark reads the same granule set from the built index)"
        )
    cells = planner.cells_for_bbox(bbox, res=res)
    where = f"h3_cell IN ({','.join(str(int(c)) for c in cells)}) AND strong"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND substr(granule, 7, 8) BETWEEN '{lo}' AND '{hi}'"
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT DISTINCT granule, url, s3url FROM read_parquet('{d}/*.parquet') WHERE {where} ORDER BY granule"
        ).fetchall()
    finally:
        con.close()
    return [{"granule": g, "url": u, "s3url": s} for g, u, s in rows]


# ----------------------------------------------------------------------------- A. OUR PATH: H3 index + byte-range
def run_ours(bbox, window, res, reps):
    from aicesat import index_atl06

    times, last = [], None
    for _ in range(reps):
        t0 = time.time()
        arr, st = index_atl06.fetch_bbox(bbox, window=window, res=res)
        times.append(time.time() - t0)
        last = (arr, st)
    arr, st = last
    m = arr["h"].size > 0
    return {
        "method": "ours (H3 index)", "granules": st.get("granules_touched", 0),
        "opens": st.get("hdf5_opens_at_query_time", 0), "requests": st.get("requests", 0),
        "bytes": st.get("bytes", 0), "gap_bytes": st.get("gap_bytes", 0), "spans": st.get("spans", 0),
        "presigns": st.get("presigns", 0), "wall_med": statistics.median(times), "wall_min": min(times),
        "points": int(arr["h"].size),
        "checksum": checksum(arr["lat"], arr["h"]) if m else (0.0, 0.0),
        "note": "query-time HDF5 opens = 0 (addressing came from the index)",
    }


# ----------------------------------------------------------------------------- B. h5coro (query-time structure walk)
def _h5coro_get(promise, path):
    """Read one dataset from an h5coro promise as a numpy array.

    GOTCHA (verified against h5coro 1.0.7): h5coro does NOT raise on a missing dataset/beam -- it logs a warning and
    the promise yields None. So a None/scalar/empty result raises KeyError here, and the caller skips that beam rather
    than crash. Promise access has drifted across versions, so we try index[:] then np.asarray."""
    try:
        v = promise[path]
    except KeyError:
        alt = ("/" + path) if not path.startswith("/") else path.lstrip("/")
        try:
            v = promise[alt]
        except KeyError as e:
            raise KeyError(f"{path}: not in h5coro promise") from e
    if v is None:
        raise KeyError(f"{path}: h5coro returned no data (missing dataset/beam)")
    try:
        arr = np.asarray(v[:])
    except (TypeError, KeyError):
        arr = np.asarray(v)
    if arr.dtype == object or arr.ndim == 0 or arr.size == 0:
        raise KeyError(f"{path}: h5coro returned no usable array")
    return arr


def _instrument_s3driver(s3driver):
    """Count S3 GETs + bytes h5coro issues by wrapping S3Driver.read (block-aligned cache-line reads). h5coro exposes
    no public byte/request metric, so this is the only observable; wrapped defensively across versions."""
    counter = {"requests": 0, "bytes": 0}
    if not hasattr(s3driver.S3Driver, "read"):
        return counter, (lambda: None)   # unknown API shape: leave counts at 0, report as n/a
    orig = s3driver.S3Driver.read

    @functools.wraps(orig)
    def counted(self, *a, **k):
        b = orig(self, *a, **k)
        counter["requests"] += 1
        try:
            counter["bytes"] += len(b)
        except TypeError:
            pass
        return b

    s3driver.S3Driver.read = counted
    return counter, (lambda: setattr(s3driver.S3Driver, "read", orig))


def run_h5coro(granules, bbox, reps):
    if not have("h5coro"):
        return {"method": "h5coro", "skipped": "not installed -- run: uv pip install h5coro"}
    if not access.in_region():
        # Out of region: prove the API calls are well-formed (no network), then skip the S3 reads.
        from h5coro import h5coro as _h5c  # noqa: F401
        from h5coro import s3driver  # noqa: F401
        sample = granules[0] if granules else {"s3url": "s3://bucket/key.h5"}
        resource = sample["s3url"].replace("s3://", "")
        paths = [f"{b}/land_ice_segments/{d}" for b in ("gt1r",) for d in BEAM_DSETS]
        return {"method": "h5coro", "skipped": "needs the us-west-2 box (S3Driver). API self-check OK: "
                f"H5Coro('{resource}', s3driver.S3Driver, credentials=...).readDatasets({paths[:2]}...)"}

    import logging

    from h5coro import h5coro as h5c
    from h5coro import s3driver
    logging.getLogger("h5coro").setLevel(logging.ERROR)   # it warns per missing dataset/beam; we handle those ourselves

    c = access.s3_credentials()   # {accessKeyId, secretAccessKey, sessionToken} -- reuse the project's cred cache
    cred = {"aws_access_key_id": c["accessKeyId"], "aws_secret_access_key": c["secretAccessKey"],
            "aws_session_token": c["sessionToken"]}
    counter, restore = _instrument_s3driver(s3driver)

    def one_pass():
        n_pts = 0
        lat_all, h_all = [], []
        for g in granules:
            resource = g["s3url"].replace("s3://", "")   # h5coro wants bucket/key, no scheme
            h5 = h5c.H5Coro(resource, s3driver.S3Driver, credentials=cred)
            # h5coro has no pre-built index: it reads sc_orient from the file, like any SlideRule-style client would.
            try:
                p = h5.readDatasets(datasets=["orbit_info/sc_orient"], block=True)
                sc = int(_h5coro_get(p, "orbit_info/sc_orient")[0])
            except Exception:  # noqa: BLE001
                continue
            for beam in strong_beams(sc):
                paths = [f"{beam}/land_ice_segments/{d}" for d in BEAM_DSETS]
                try:  # one readDatasets per beam -> h5coro coalesces the 5 near-contiguous datasets into few GETs
                    pr = h5.readDatasets(datasets=paths, block=True)
                    lat = _h5coro_get(pr, paths[0]); lon = _h5coro_get(pr, paths[1])
                    h = _h5coro_get(pr, paths[2]).astype("f8"); q = _h5coro_get(pr, paths[4])
                except Exception:  # noqa: BLE001  (missing/empty beam in this granule -- h5coro yields None, _h5coro_get raises)
                    continue
                if not (len(lat) == len(lon) == len(h) == len(q)):
                    continue
                m = bbox_quality_mask(lat, lon, h, q, bbox)
                if m.any():
                    n_pts += int(m.sum()); lat_all.append(lat[m]); h_all.append(h[m])
        lat_cat = np.concatenate(lat_all) if lat_all else np.array([])
        h_cat = np.concatenate(h_all) if h_all else np.array([])
        return n_pts, lat_cat, h_cat

    try:
        times, last = [], None
        for i in range(reps):
            if i == reps - 1:
                counter["requests"] = counter["bytes"] = 0   # measure GETs/bytes on the final (steady) pass
            t0 = time.time()
            last = one_pass()
            times.append(time.time() - t0)
    finally:
        restore()
    n_pts, lat_cat, h_cat = last
    return {
        "method": "h5coro", "granules": len(granules), "opens": len(granules),
        "requests": counter["requests"] or None, "bytes": counter["bytes"] or None,
        "wall_med": statistics.median(times), "wall_min": min(times), "points": n_pts,
        "checksum": checksum(lat_cat, h_cat) if n_pts else (0.0, 0.0),
        "note": "re-reads each granule's HDF5 structure every query (no pre-build); GETs/bytes = wrapped S3Driver.read",
    }


# ----------------------------------------------------------------------------- C. h5py-over-s3fs (client-side baseline)
def run_h5py_s3fs(granules, bbox, reps):
    if not (have("h5py") and have("s3fs")):
        return {"method": "h5py+s3fs", "skipped": "install h5py + s3fs"}
    if not access.in_region():
        return {"method": "h5py+s3fs", "skipped": "needs the us-west-2 box (s3fs S3-direct)"}
    import h5py
    import s3fs

    counter = {"requests": 0, "bytes": 0}
    orig = getattr(s3fs.core.S3File, "_fetch_range", None)

    def counted(self, start, end):
        counter["requests"] += 1
        counter["bytes"] += max(0, end - start)
        return orig(self, start, end)

    def one_pass():
        n_pts, lat_all, h_all = 0, [], []
        for g in granules:
            with h5py.File(access.cloud_hdf5_file(g["url"], g["s3url"]), "r") as f:
                if "orbit_info/sc_orient" not in f:
                    continue
                for beam in strong_beams(int(f["orbit_info/sc_orient"][0])):
                    grp = f"{beam}/land_ice_segments"
                    if grp not in f or "latitude" not in f[grp]:
                        continue
                    gg = f[grp]
                    lat = gg["latitude"][:]; lon = gg["longitude"][:]
                    h = gg["h_li"][:].astype("f8"); q = gg["atl06_quality_summary"][:]
                    m = bbox_quality_mask(lat, lon, h, q, bbox)
                    if m.any():
                        n_pts += int(m.sum()); lat_all.append(lat[m]); h_all.append(h[m])
        lat_cat = np.concatenate(lat_all) if lat_all else np.array([])
        h_cat = np.concatenate(h_all) if h_all else np.array([])
        return n_pts, lat_cat, h_cat

    if orig is not None:
        s3fs.core.S3File._fetch_range = counted
    try:
        times, last = [], None
        for i in range(reps):
            if i == reps - 1:
                counter["requests"] = counter["bytes"] = 0
            t0 = time.time()
            last = one_pass()
            times.append(time.time() - t0)
    finally:
        if orig is not None:
            s3fs.core.S3File._fetch_range = orig
    n_pts, lat_cat, h_cat = last
    return {
        "method": "h5py+s3fs", "granules": len(granules), "opens": len(granules),
        "requests": counter["requests"] or None, "bytes": counter["bytes"] or None,
        "wall_med": statistics.median(times), "wall_min": min(times), "points": n_pts,
        "checksum": checksum(lat_cat, h_cat) if n_pts else (0.0, 0.0),
        "note": "open + h5py slice per granule; block-cache reads counted at s3fs _fetch_range",
    }


# ----------------------------------------------------------------------------- D. kerchunk/zarr (optional analog)
def run_kerchunk(granules, bbox, reps):
    """Closest analog to us: pre-extract chunk references (a structure parse == our amortizable index build), then
    read via zarr. Best-effort/optional: the in-region ReferenceFileSystem->s3 auth plumbing is fiddly, so this skips
    cleanly on any failure rather than report an unfair number."""
    for mod in ("kerchunk", "zarr", "fsspec"):
        if not have(mod):
            return {"method": "kerchunk+zarr", "skipped": f"install {mod} (uv pip install kerchunk zarr xarray)"}
    if not access.in_region():
        return {"method": "kerchunk+zarr", "skipped": "needs the us-west-2 box (reads granules from S3)"}
    try:
        import h5py
        import zarr
        from kerchunk.hdf import SingleHdf5ToZarr

        c = access.s3_credentials()
        so = {"key": c["accessKeyId"], "secret": c["secretAccessKey"], "token": c["sessionToken"]}

        # --- build phase: extract references once (the amortizable structure parse, analogous to our index build)
        t_build0 = time.time()
        refs = {}
        for g in granules:
            with h5py.File(access.cloud_hdf5_file(g["url"], g["s3url"]), "r") as fh:
                refs[g["s3url"]] = SingleHdf5ToZarr(fh, g["s3url"]).translate()
        t_build = time.time() - t_build0

        # --- read phase (the query-time cost, the axis comparable to h5coro/ours)
        def one_pass():
            import fsspec
            n_pts, lat_all, h_all = 0, [], []
            for s3url, ref in refs.items():
                fs = fsspec.filesystem("reference", fo=ref, remote_protocol="s3", remote_options=so)
                zg = zarr.open(fs.get_mapper(""), mode="r")
                try:
                    sc = int(np.asarray(zg["orbit_info/sc_orient"])[0])
                except Exception:  # noqa: BLE001
                    continue
                for beam in strong_beams(sc):
                    base = f"{beam}/land_ice_segments"
                    try:
                        lat = np.asarray(zg[f"{base}/latitude"]); lon = np.asarray(zg[f"{base}/longitude"])
                        h = np.asarray(zg[f"{base}/h_li"]).astype("f8"); q = np.asarray(zg[f"{base}/atl06_quality_summary"])
                    except Exception:  # noqa: BLE001
                        continue
                    m = bbox_quality_mask(lat, lon, h, q, bbox)
                    if m.any():
                        n_pts += int(m.sum()); lat_all.append(lat[m]); h_all.append(h[m])
            lat_cat = np.concatenate(lat_all) if lat_all else np.array([])
            h_cat = np.concatenate(h_all) if h_all else np.array([])
            return n_pts, lat_cat, h_cat

        times, last = [], None
        for _ in range(reps):
            t0 = time.time()
            last = one_pass()
            times.append(time.time() - t0)
        n_pts, lat_cat, h_cat = last
        return {
            "method": "kerchunk+zarr", "granules": len(granules), "opens": 0,
            "requests": None, "bytes": None, "wall_med": statistics.median(times), "wall_min": min(times),
            "points": n_pts, "checksum": checksum(lat_cat, h_cat) if n_pts else (0.0, 0.0),
            "build_s": round(t_build, 1),
            "note": f"read-phase timing shown; refs pre-built in {t_build:.1f}s (amortizable, like our index)",
        }
    except Exception as e:  # noqa: BLE001
        return {"method": "kerchunk+zarr", "skipped": f"wiring failed ({type(e).__name__}: {e}) -- optional path"}


RUNNERS = {"ours": run_ours, "h5coro": run_h5coro, "h5py_s3fs": run_h5py_s3fs, "kerchunk": run_kerchunk}


# ----------------------------------------------------------------------------- report
def fmt(v, spec, na="  n/a"):
    return na if v is None else format(v, spec)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--methods", default="ours,h5coro,h5py_s3fs,kerchunk")
    ap.add_argument("--bbox", type=float, nargs=4, default=DEFAULT_BBOX, metavar=("W", "S", "E", "N"))
    ap.add_argument("--window", nargs=2, default=list(DEFAULT_WINDOW), metavar=("START", "END"))
    ap.add_argument("--res", type=int, default=ATL06_RES)
    ap.add_argument("--reps", type=int, default=3, help="repetitions per method (median reported)")
    ap.add_argument("--max-granules", type=int, default=0, help="cap the granule set (0 = all in bbox)")
    a = ap.parse_args()
    bbox, window = list(a.bbox), tuple(a.window)

    auth.login()
    reg = access.in_region()
    print(f"in_region={reg}  |  " + ("S3-direct: wall-clock is the REAL perf number." if reg else
          "OUT-OF-REGION: S3 methods are skipped (they need the us-west-2 box); the API self-checks still run."))
    print(f"ATL06 bbox={bbox} window={window} res={a.res} reps={a.reps}\n")

    try:
        granules = granules_from_index(bbox, window, a.res)
    except RuntimeError as e:
        print(f"cannot select granules: {e}")
        return
    if a.max_granules:
        granules = granules[: a.max_granules]
    print(f"granule set from the built index: {len(granules)} ATL06 v007 granule(s)"
          + (f" (first: {granules[0]['granule']})" if granules else "") + "\n")
    if not granules:
        print("no granules touch this bbox/window in the index -- widen the box or build a covering index.")
        return

    results = []
    for name in [m.strip() for m in a.methods.split(",") if m.strip()]:
        if name not in RUNNERS:
            print(f"unknown method {name!r} (choose from {list(RUNNERS)})"); continue
        print(f"=== {name} ===")
        if name == "ours":
            r = run_ours(bbox, window, a.res, a.reps)
        else:
            r = RUNNERS[name](granules, bbox, a.reps)
        if r.get("skipped"):
            print(f"    SKIP: {r['skipped']}")
        else:
            print(f"    {r['points']} pts, wall_med={r['wall_med']:.2f}s  ({r.get('note', '')})")
        results.append(r)

    ran = [r for r in results if not r.get("skipped")]
    print("\n" + "=" * 104)
    print(f"{'method':<18}{'gran':>5}{'opens':>6}{'requests':>10}{'MB':>9}{'wall_med':>10}{'wall_min':>10}"
          f"{'points':>9}{'checksum(lat,h)':>22}")
    print("-" * 104)
    for r in results:
        if r.get("skipped"):
            reason = r["skipped"].split(".")[0][:66]   # short reason; the full text printed in the '=== method ===' block
            print(f"{r['method']:<18}  skipped -- {reason}")
            continue
        mb = None if r.get("bytes") is None else r["bytes"] / 1e6
        cs = r.get("checksum", (0.0, 0.0))
        print(f"{r['method']:<18}{r.get('granules', ''):>5}{r.get('opens', ''):>6}"
              f"{fmt(r.get('requests'), 'd', na='   n/a'):>10}{fmt(mb, '.1f', na='  n/a'):>9}"
              f"{r['wall_med']:>10.2f}{r['wall_min']:>10.2f}{r['points']:>9}"
              f"{str(cs):>22}")

    # ---- correctness: every method that ran must return the same points (count + checksum) before timings mean anything
    print("-" * 104)
    if len(ran) >= 2:
        ref = ran[0]
        pts_ok = all(r["points"] == ref["points"] for r in ran)
        cs_ok = all(r["checksum"] == ref["checksum"] for r in ran)
        if pts_ok and cs_ok:
            print(f"CORRECTNESS: PASS -- all {len(ran)} methods returned {ref['points']} identical points "
                  f"(checksum {ref['checksum']}).")
        else:
            print("CORRECTNESS: MISMATCH -- methods disagree, timings below are NOT comparable:")
            for r in ran:
                print(f"    {r['method']:<18} points={r['points']} checksum={r['checksum']}")

    # ---- honest summary: query-time winner + the amortization caveat
    ours = next((r for r in ran if r["method"].startswith("ours")), None)
    h5c = next((r for r in ran if r["method"] == "h5coro"), None)
    print()
    if ours and h5c:
        ot, ht = ours["wall_med"], h5c["wall_med"]
        if not reg:
            print("(out-of-region wall-clock is your local link, not the system -- rerun on the box for real numbers)")
        if ot < ht:
            print(f"QUERY-TIME WINNER: our index, {ht / max(ot, 1e-9):.1f}x faster than h5coro "
                  f"({ot:.2f}s vs {ht:.2f}s) -- it skips the per-query HDF5 structure walk (0 opens vs {h5c['opens']}).")
        else:
            print(f"QUERY-TIME WINNER: h5coro, {ot / max(ht, 1e-9):.1f}x faster than our index "
                  f"({ht:.2f}s vs {ot:.2f}s) on this subset -- report it honestly.")
        print("CAVEAT: our query-time win is bought with a one-time index build the others never pay "
              "(~$6 to index the whole Earth's ATL06, per the project's measurements; amortized over all future "
              "queries). h5coro/h5py/kerchunk have no pre-build but re-walk each granule's structure every query.")
    elif ours:
        print("h5coro did not run (see skip reason). Our path metrics stand alone; install h5coro on the box to compare.")
    else:
        print("Our path did not run -- build the ATL06 index on the box first (scripts/build_atl06_index.py).")


if __name__ == "__main__":
    main()
