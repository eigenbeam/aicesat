"""Honest, same-subset query-time comparison of the project's H3 sub-granule index vs cloud-HDF5 readers.

Targets ATL06 (the clean ICESat-2 land-ice case h5coro / SlideRule are built for). Every reader is handed the SAME
granule set (from the built index) and applies the SAME bbox+quality predicate, so all return byte-identical points;
only then are timings compared. Readers:
  ours        -- index_atl06.fetch_bbox with force=True: the H3-addressed byte-range fetch, cold on EVERY rep
  ours_cached -- the same call without force: served from the lake, no network. A SEPARATE claim from the one above,
                 so it is a separate row -- nothing resets the lake between reps, and reporting the cache hit as
                 "our access method" compares our local Parquet to everyone else's network read
  h5coro     -- SlideRule's cloud-native HDF5 reader (re-walks each granule's structure per query)
  h5py_s3fs  -- baseline: h5py slice over s3fs S3-direct
  kerchunk   -- pre-extract chunk refs (amortizable, like our index build), then read via zarr (optional)

ROBUSTNESS (this is a benchmark harness, so fairness matters):
  * Methods are INTERLEAVED within each rep (rep0: ours,h5coro,... ; rep1: ours,h5coro,... ), so a transient network
    hiccup penalises every method equally instead of whichever one happened to be running -- the single biggest
    reason a naive run's winner flips between invocations.
  * Full variance is reported (median, p95, stdev, min over reps), not just the median, so noise is visible.
  * --granule-steps sweeps the SAME box at increasing granule counts to find the crossover where h5coro's per-granule
    structure walk overtakes our fixed index overhead -- the interesting axis, not a single point.
  * --warmup discards the first N (cold) reps per method; --csv captures every (region, method, rep) row.

Run IN-REGION on the us-west-2 box (S3-direct) for real numbers:
    set -a; . ./aicesat.env; set +a
    uv pip install h5coro                          # + optionally: uv pip install 'zarr<3' kerchunk xarray
    uv run scripts/build_atl06_index.py            # if the ATL06 index isn't already built
    uv run scripts/bench_vs_h5coro.py --reps 7 --warmup 1
    uv run scripts/bench_vs_h5coro.py --granule-steps 1,3,10,25,50 --reps 5 --warmup 1 --csv bench.csv
    uv run scripts/bench_vs_h5coro.py --methods ours,h5coro,h5py_s3fs,kerchunk --reps 7 --warmup 1
"""
import argparse
import contextlib
import functools
import importlib.util
import math
import os
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


def stats_of(times: list[float]) -> dict:
    """Median/mean/stdev/p95/min over the (post-warmup) reps -- the honest spread, not one number."""
    ts = sorted(times)
    n = len(ts)
    if n == 0:
        return {"n": 0, "min": float("nan"), "med": float("nan"), "mean": float("nan"), "std": float("nan"), "p95": float("nan")}
    p95 = ts[min(n - 1, int(math.ceil(0.95 * n)) - 1)]
    return {"n": n, "min": ts[0], "med": statistics.median(ts), "mean": statistics.fmean(ts),
            "std": statistics.pstdev(ts) if n > 1 else 0.0, "p95": p95}


def _med_int(vals):
    """Median of the per-rep request/byte counts, ignoring None (methods without a counter)."""
    xs = [v for v in vals if v is not None]
    return None if not xs else int(round(statistics.median(xs)))


def _granule_date(g: dict) -> str:
    """'ATL06_20190314235626_...' -> '2019-03-14'. ATL06 granule names embed the acquisition date at chars 7-14."""
    d = g["granule"][6:14]
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


@contextlib.contextmanager
def _aws_env(creds: dict):
    """Temporarily expose STS creds as AWS_* env vars so any fsspec/botocore session (e.g. kerchunk's reference
    filesystem, which does not reliably thread remote_options) resolves them. Scoped + restored, so interleaved
    runs of the other methods are unaffected."""
    keys = {"AWS_ACCESS_KEY_ID": creds["accessKeyId"], "AWS_SECRET_ACCESS_KEY": creds["secretAccessKey"],
            "AWS_SESSION_TOKEN": creds["sessionToken"]}
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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


# ============================================================================= setups: each returns a uniform handle
# handle = {"method", "one_pass": ()->(n_pts, lat, h, requests|None, bytes|None), "opens", "granules", "note",
#           "teardown"()?, "build_s"?}  OR  {"method", "skipped": reason}
# The harness drives the rep loop and interleaves methods, so setups do ONE-TIME work (creds, presign, ref build)
# and one_pass does the per-query work that is actually timed.

# ----------------------------------------------------------------------------- A. OUR PATH: H3 index + byte-range
def setup_ours(granules, bbox, window, res, cold=True):
    """`cold=True` re-fetches from NASA on EVERY rep; `cold=False` is the lake cache hit.

    This used to be one method with neither flag, which quietly measured the wrong thing: nothing resets the lake
    between reps, so after rep 0 every chunk is marked ingested and fetch_bbox returns from local Parquet with
    requests=0. With --warmup 1 the single rep that touched the network was then DISCARDED, so the timed comparison
    was our local cache against h5coro's cold network read. Both numbers are worth having — the index advantage and
    the cache advantage are different claims — so they are now two rows, each labelled for what it is.
    """
    from aicesat import index_atl06

    def one_pass():
        arr, st = index_atl06.fetch_bbox(bbox, window=window, res=res, force=cold)
        m = arr["h"].size > 0
        return (int(arr["h"].size), arr["lat"] if m else np.array([]), arr["h"] if m else np.array([]),
                st.get("requests"), st.get("bytes"), st.get("gap_bytes", 0))

    return {"method": "ours (H3 index, cold)" if cold else "ours (lake cache hit)",
            "one_pass": one_pass, "opens": 0, "granules": len(granules),
            "note": ("query-time HDF5 opens = 0 (addressing came from the index); force=True so every rep fetches"
                     if cold else "NO network: served from Parquet the index already materialised")}


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


def setup_h5coro(granules, bbox, window, res):
    if not have("h5coro"):
        return {"method": "h5coro", "skipped": "not installed -- run: uv pip install h5coro"}
    if not access.in_region():
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
        req0, byt0 = counter["requests"], counter["bytes"]
        n_pts, lat_all, h_all = 0, [], []
        for g in granules:
            resource = g["s3url"].replace("s3://", "")   # h5coro wants bucket/key, no scheme
            h5 = h5c.H5Coro(resource, s3driver.S3Driver, credentials=cred)
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
                except Exception:  # noqa: BLE001  (missing/empty beam -- h5coro yields None, _h5coro_get raises)
                    continue
                if not (len(lat) == len(lon) == len(h) == len(q)):
                    continue
                m = bbox_quality_mask(lat, lon, h, q, bbox)
                if m.any():
                    n_pts += int(m.sum()); lat_all.append(lat[m]); h_all.append(h[m])
        lat_cat = np.concatenate(lat_all) if lat_all else np.array([])
        h_cat = np.concatenate(h_all) if h_all else np.array([])
        return (n_pts, lat_cat, h_cat, counter["requests"] - req0, counter["bytes"] - byt0)

    return {"method": "h5coro", "one_pass": one_pass, "opens": len(granules), "granules": len(granules),
            "teardown": restore,
            "note": "re-reads each granule's HDF5 structure every query (no pre-build); GETs/bytes = wrapped S3Driver.read"}


# ----------------------------------------------------------------------------- C. h5py-over-s3fs (client baseline)
def setup_h5py_s3fs(granules, bbox, window, res):
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

    if orig is not None:
        s3fs.core.S3File._fetch_range = counted

    def one_pass():
        req0, byt0 = counter["requests"], counter["bytes"]
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
        return (n_pts, lat_cat, h_cat, counter["requests"] - req0, counter["bytes"] - byt0)

    def teardown():
        if orig is not None:
            s3fs.core.S3File._fetch_range = orig

    return {"method": "h5py+s3fs", "one_pass": one_pass, "opens": len(granules), "granules": len(granules),
            "teardown": teardown,
            "note": "open + h5py slice per granule; block-cache reads counted at s3fs _fetch_range"}


# ----------------------------------------------------------------------------- D. kerchunk/zarr (optional analog)
def setup_kerchunk(granules, bbox, window, res):
    """Closest analog to us: pre-extract chunk references once (a structure parse == our amortizable index build),
    then read via zarr. The reference-filesystem->s3 auth is fiddly, so this skips cleanly on any failure rather than
    report an unfair number. kerchunk needs zarr<3; STS creds are injected via env for the reference fs."""
    for mod in ("kerchunk", "zarr", "fsspec"):
        if not have(mod):
            return {"method": "kerchunk+zarr", "skipped": f"install {mod} (uv pip install 'zarr<3' kerchunk xarray)"}
    if not access.in_region():
        return {"method": "kerchunk+zarr", "skipped": "needs the us-west-2 box (reads granules from S3)"}
    import zarr
    zmajor = int(str(zarr.__version__).split(".")[0])
    if zmajor >= 3:
        return {"method": "kerchunk+zarr", "skipped": f"needs zarr<3 (have {zarr.__version__}) -- run: uv pip install 'zarr<3'"}
    try:
        import h5py
        from kerchunk.hdf import SingleHdf5ToZarr

        c = access.s3_credentials()
        so = {"key": c["accessKeyId"], "secret": c["secretAccessKey"], "token": c["sessionToken"]}

        # --- build phase (one-time; the amortizable structure parse, analogous to our index build) -- NOT timed here
        t_build0 = time.time()
        refs = {}
        with _aws_env(c):
            for g in granules:
                with h5py.File(access.cloud_hdf5_file(g["url"], g["s3url"]), "r") as fh:
                    refs[g["s3url"]] = SingleHdf5ToZarr(fh, g["s3url"]).translate()
        t_build = time.time() - t_build0

        def one_pass():
            import fsspec
            with _aws_env(c):   # belt-and-suspenders: the reference fs's s3fs resolves creds from env even if
                n_pts, lat_all, h_all = 0, [], []   # remote_options is not threaded (the NoCredentialsError cause)
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
                return (n_pts, lat_cat, h_cat, None, None)

        return {"method": "kerchunk+zarr", "one_pass": one_pass, "opens": 0, "granules": len(granules),
                "build_s": round(t_build, 1),
                "note": f"read-phase timing; refs pre-built in {t_build:.1f}s (amortizable, like our index)"}
    except Exception as e:  # noqa: BLE001
        return {"method": "kerchunk+zarr", "skipped": f"wiring failed ({type(e).__name__}: {e}) -- optional path"}


SETUPS = {"ours": setup_ours,
          "ours_cached": lambda g, b, w, r: setup_ours(g, b, w, r, cold=False),
          "h5coro": setup_h5coro, "h5py_s3fs": setup_h5py_s3fs, "kerchunk": setup_kerchunk}


# ============================================================================= harness: interleaved reps per region
def bench_region(label, granules, bbox, window, res, methods, reps, warmup):
    """Set up every method once, then drive reps INTERLEAVED (all methods each rep) so network variance is shared.
    Returns a list of per-method result dicts with full timing stats + the per-rep records (for CSV)."""
    setups = {m: SETUPS[m](granules, bbox, window, res) for m in methods}
    recs = {m: [] for m in methods}   # per-rep: {wall, req, byt, points, checksum, warmup}
    total = reps + warmup
    from aicesat.access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, default_coalesce_gap,
                                in_region, pool_size)
    _nw = pool_size(max(len(granules), 2), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV,
                    cpu_bound=False)
    print(f"config: s3_direct={in_region()}  fetch_workers={_nw}  "
          f"coalesce_gap={default_coalesce_gap()/(1 << 20):.2f} MiB")
    for rep in range(total):
        is_warm = rep < warmup
        for m in methods:
            s = setups[m]
            if s.get("skipped"):
                continue
            t0 = time.time()
            out = s["one_pass"]()
            dt = time.time() - t0
            # one_pass returns (n_points, lat, h, requests, bytes) and OPTIONALLY a 6th over-fetch count. Only our
            # path coalesces byte ranges, so only it has one; the others read exactly what they ask for.
            assert len(out) in (5, 6), f"{m}: one_pass returned {len(out)} values, expected 5 or 6"
            npts, lat, h, req, byt = out[:5]
            gap = out[5] if len(out) == 6 else 0
            # Settle our background lake writers BEFORE the next method is timed. fetch_bbox returns as soon as the
            # points are in memory and writes Parquet on daemon threads, and reps are INTERLEAVED — so without this
            # our writes run concurrently with h5coro's timed rep and charge it for our work. Outside the timer, so
            # ours is still time-to-data.
            from aicesat import lake as _lake
            _lake.drain_writes()
            recs[m].append({"wall": dt, "req": req, "byt": byt, "gap": gap or 0, "points": npts,
                            "checksum": checksum(lat, h) if npts else (0.0, 0.0), "warmup": is_warm})
    for s in setups.values():
        td = s.get("teardown")
        if td:
            try:
                td()
            except Exception:  # noqa: BLE001
                pass

    results = []
    for m in methods:
        s = setups[m]
        if s.get("skipped"):
            results.append({"region": label, "method": s["method"], "skipped": s["skipped"]})
            continue
        timed = [r for r in recs[m] if not r["warmup"]] or recs[m]   # if warmup>=reps, fall back to all
        st = stats_of([r["wall"] for r in timed])
        pts = timed[-1]["points"] if timed else 0
        cs = timed[-1]["checksum"] if timed else (0.0, 0.0)
        results.append({
            "region": label, "method": s["method"], "granules": s.get("granules", len(granules)),
            "opens": s.get("opens", 0), "requests": _med_int([r["req"] for r in timed]),
            "bytes": _med_int([r["byt"] for r in timed]), "gap_bytes": _med_int([r["gap"] for r in timed]),
            "stats": st, "points": pts, "checksum": cs,
            "build_s": s.get("build_s"), "note": s.get("note", ""), "records": recs[m],
        })
    return results


# ----------------------------------------------------------------------------- report + csv
def print_region(label, results, reg):
    ran = [r for r in results if not r.get("skipped")]
    print("\n" + "=" * 125)
    print(f"REGION {label}   (reps timing = post-warmup)")
    print(f"{'method':<22}{'gran':>5}{'opens':>6}{'reqs':>7}{'MB read':>9}{'MB want':>9}{'med':>9}{'p95':>9}"
          f"{'std':>8}{'min':>9}{'points':>9}{'checksum(lat,h)':>22}")
    print("-" * 125)
    for r in results:
        if r.get("skipped"):
            reason = r["skipped"].split(".")[0][:78]
            print(f"{r['method']:<22}  skipped -- {reason}")
            continue
        st = r["stats"]
        mb = None if r.get("bytes") is None else r["bytes"] / 1e6
        mbs = "  n/a" if mb is None else f"{mb:.1f}"
        # bytes READ includes what coalescing pulled across gaps; bytes WANTED is what the query actually needed.
        # They differ by ~2.4x on our path at the 1 MB in-region gap, and only one of them is a fair "data moved".
        want = None if mb is None else (r["bytes"] - (r.get("gap_bytes") or 0)) / 1e6
        wants = "  n/a" if want is None else f"{want:.1f}"
        reqs = "  n/a" if r.get("requests") is None else f"{r['requests']:d}"
        print(f"{r['method']:<22}{r.get('granules', ''):>5}{r.get('opens', ''):>6}{reqs:>7}{mbs:>9}{wants:>9}"
              f"{st['med']:>9.2f}{st['p95']:>9.2f}{st['std']:>8.2f}{st['min']:>9.2f}{r['points']:>9}"
              f"{str(r['checksum']):>22}")
    print("-" * 125)
    if len(ran) >= 2:
        ref = ran[0]
        ok = all(r["points"] == ref["points"] and r["checksum"] == ref["checksum"] for r in ran)
        if ok:
            print(f"CORRECTNESS: PASS -- all {len(ran)} methods returned {ref['points']} identical points "
                  f"(checksum {ref['checksum']}).")
        else:
            print("CORRECTNESS: MISMATCH -- methods disagree, timings NOT comparable:")
            for r in ran:
                print(f"    {r['method']:<22} points={r['points']} checksum={r['checksum']}")
    # per-region winner (median, with spread so a close call reads as close)
    # explicitly the COLD row: comparing our cache hit to h5coro's network read is not a comparison.
    ours = next((r for r in ran if r["method"].startswith("ours (H3 index")), None)
    h5c = next((r for r in ran if r["method"] == "h5coro"), None)
    if ours and h5c:
        ot, os_, ht, hs = ours["stats"]["med"], ours["stats"]["std"], h5c["stats"]["med"], h5c["stats"]["std"]
        gap = abs(ot - ht)
        close = gap < (os_ + hs)   # within combined stdev == a statistical tie
        if close:
            print(f"WINNER: tie within noise -- ours {ot:.2f}±{os_:.2f}s vs h5coro {ht:.2f}±{hs:.2f}s "
                  f"(gap {gap:.2f}s < combined stdev {os_ + hs:.2f}s). The stable difference is bytes moved: "
                  f"ours {(ours['bytes'] or 0)/1e6:.1f} MB read / "
                  f"{((ours['bytes'] or 0) - (ours.get('gap_bytes') or 0))/1e6:.1f} MB wanted "
                  f"vs h5coro {(h5c['bytes'] or 0)/1e6:.1f} MB.")
        elif ot < ht:
            print(f"WINNER: ours, {ht/max(ot,1e-9):.2f}x faster (median {ot:.2f}s vs {ht:.2f}s) -- skips the "
                  f"per-query HDF5 structure walk (0 opens vs {h5c['opens']}).")
        else:
            print(f"WINNER: h5coro, {ot/max(ht,1e-9):.2f}x faster (median {ht:.2f}s vs {ot:.2f}s) on this subset "
                  "-- reported honestly.")


def write_csv(path, all_results, reg, bbox, window, res):
    import csv
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["region", "method", "rep", "warmup", "wall_s", "requests", "bytes", "points", "checksum_lat", "checksum_h",
                    "in_region", "bbox", "window", "res"])
        bs, ws = str(list(bbox)), str(list(window))
        for results in all_results:
            for r in results:
                if r.get("skipped"):
                    w.writerow([r["region"], r["method"], "", "", "", "", "", "", "", "", reg, bs, ws, res])
                    continue
                for i, rec in enumerate(r["records"]):
                    w.writerow([r["region"], r["method"], i, int(rec["warmup"]), f"{rec['wall']:.4f}",
                                rec["req"] if rec["req"] is not None else "", rec["byt"] if rec["byt"] is not None else "",
                                rec["points"], rec["checksum"][0], rec["checksum"][1], reg, bs, ws, res])
    print(f"\nwrote per-rep rows -> {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--methods", default="ours,h5coro,h5py_s3fs,kerchunk")
    ap.add_argument("--bbox", type=float, nargs=4, default=DEFAULT_BBOX, metavar=("W", "S", "E", "N"))
    ap.add_argument("--window", nargs=2, default=list(DEFAULT_WINDOW), metavar=("START", "END"))
    ap.add_argument("--res", type=int, default=ATL06_RES)
    ap.add_argument("--reps", type=int, default=5, help="timed repetitions per method (median + spread reported)")
    ap.add_argument("--warmup", type=int, default=0, help="discard the first N (cold) reps per method")
    ap.add_argument("--max-granules", type=int, default=0, help="cap the granule set (0 = all in bbox)")
    ap.add_argument("--granule-steps", default="", help="comma list, e.g. 1,3,10,25,50: sweep the SAME box capped at "
                    "each N to find the crossover (overrides --max-granules)")
    ap.add_argument("--csv", default="", help="write every (region, method, rep) row to this CSV")
    a = ap.parse_args()
    bbox, window = list(a.bbox), tuple(a.window)
    methods = [m.strip() for m in a.methods.split(",") if m.strip()]
    for m in methods:
        if m not in SETUPS:
            print(f"unknown method {m!r} (choose from {list(SETUPS)})"); return

    auth.login()
    reg = access.in_region()
    print(f"in_region={reg}  |  " + ("S3-direct: wall-clock is the REAL perf number." if reg else
          "OUT-OF-REGION: S3 methods are skipped (they need the us-west-2 box); the API self-checks still run."))
    print(f"ATL06 bbox={bbox} window={window} res={a.res} reps={a.reps} warmup={a.warmup}  methods={methods}\n")

    try:
        full = granules_from_index(bbox, window, a.res)
    except RuntimeError as e:
        print(f"cannot select granules: {e}")
        return
    if not full:
        print("no granules touch this bbox/window in the index -- widen the box or build a covering index.")
        return
    print(f"granule set from the built index: {len(full)} ATL06 v007 granule(s)"
          f" (first: {full[0]['granule']})\n")

    # regions to bench: a granule-step sweep, or a single region (optionally capped by --max-granules).
    # A granule cap is applied by TIGHTENING THE WINDOW to the first N granules' date range -- because `ours`
    # (index_atl06.fetch_bbox) is driven by (bbox, window), not by a granule list, so a post-hoc list cap would
    # leave `ours` fetching the full set while the others saw the subset (the correctness gate would -- correctly --
    # refuse to compare them). Window-tightening makes every method see the identical granules.
    def _capped_region(target_n: int):
        n = min(target_n, len(full))
        w = (window[0], _granule_date(full[n - 1]))          # upper bound = the Nth granule's acquisition date
        g = granules_from_index(bbox, w, a.res)              # the actual set the window selects (>= n if dates tie)
        return (f"N={len(g)}", g, w)

    if a.granule_steps:
        steps = [int(x) for x in a.granule_steps.split(",") if x.strip()]
        seen, regions = set(), []
        for nn in steps:                                     # a target beyond the available granules collapses to
            r = _capped_region(nn)                           # the full set -> dedup so we don't bench N=10 thrice
            if r[0] not in seen:
                seen.add(r[0]); regions.append(r)
        if len(regions) < len(steps):
            print(f"note: only {len(full)} granules in this bbox/window; sweep collapsed to {[r[0] for r in regions]}. "
                  "Widen --bbox/--window for a longer sweep.\n")
    elif a.max_granules:
        regions = [_capped_region(a.max_granules)]
    else:
        regions = [(f"N={len(full)}", full, window)]

    all_results = []
    for label, granules, wr in regions:
        print(f"--- benching {label} ({len(granules)} granules, window {wr[0]}..{wr[1]}) "
              f"x {len(methods)} methods, interleaved ---")
        results = bench_region(label, granules, bbox, wr, a.res, methods, a.reps, a.warmup)
        print_region(label, results, reg)
        all_results.append(results)

    # sweep summary: median wall per method across N (the crossover, at a glance)
    if len(all_results) > 1:
        disp = {"ours": "ours (H3 index, cold)", "ours_cached": "ours (lake cache hit)", "h5coro": "h5coro", "h5py_s3fs": "h5py+s3fs", "kerchunk": "kerchunk+zarr"}
        labels = [r[0] for r in regions]
        print("\n" + "=" * 125)
        print("SWEEP (median wall seconds by granule count)")
        print("method".ljust(18) + "".join(f"{lab:>12}" for lab in labels))
        print("-" * 125)
        for m in methods:
            cells = []
            for results in all_results:
                r = next((x for x in results if _mkey(x.get("method", "")) == m), None)
                cells.append("skip" if (r is None or r.get("skipped")) else f"{r['stats']['med']:.2f}")
            print(disp.get(m, m).ljust(18) + "".join(f"{c:>12}" for c in cells))
        # Per-granule scaling is the claim worth making: a single ratio at one N says much less than how the ratio
        # MOVES. Anchored on the first step so "5.7x for 10x the granules" is readable straight off the table.
        pts = [next((x.get("points") for x in results if not x.get("skipped")), 0) for results in all_results]
        print("\npoints returned".ljust(18) + "".join(f"{p:>12,}" for p in pts))
        for m in methods:
            row = [next((x for x in results if _mkey(x.get("method", "")) == m and not x.get("skipped")), None)
                   for results in all_results]
            if row[0] and row[-1] and row[0]["stats"]["med"] > 0:
                growth = row[-1]["stats"]["med"] / row[0]["stats"]["med"]
                print(f"  {disp.get(m, m):<24} {labels[0]} -> {labels[-1]}: {growth:.1f}x slower")
        # A step that adds granules carrying no points in the bbox measures per-granule OVERHEAD against a fixed
        # payload, not throughput. That is a fair and useful thing to measure, but a reader who does not notice the
        # points column will read it as throughput.
        if len(set(pts)) < len(pts):
            same = [labels[i] for i in range(1, len(pts)) if pts[i] == pts[i - 1]]
            print(f"NOTE: {', '.join(same)} returned the SAME point count as the previous step — those granules carry "
                  f"no data inside the bbox, so that column is per-granule OVERHEAD, not throughput.")
        print("CAVEAT: our per-query win is bought with a one-time index build the others never pay (~$6 whole-Earth "
              "ATL06, amortized over all queries). h5coro/h5py/kerchunk re-walk each granule's structure every query.")

    if a.csv:
        write_csv(a.csv, all_results, reg, bbox, window, a.res)
    if not reg:
        print("\n(out-of-region wall-clock is your local link, not the system -- rerun on the box for real numbers)")


def _mkey(method_name: str) -> str:
    """Map a display method name back to its --methods key for sweep-table matching."""
    n = method_name.lower()
    if n.startswith("ours"):
        return "ours"
    if n.startswith("h5coro"):
        return "h5coro"
    if n.startswith("h5py"):
        return "h5py_s3fs"
    if n.startswith("kerchunk"):
        return "kerchunk"
    return method_name


if __name__ == "__main__":
    main()
