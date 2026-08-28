"""ATL06 addressing index — the ATL03 index pattern (index.py) applied to land-ice segments.

Each land_ice_segments dataset is one value per ~40 m segment, and on v007 they are all chunked identically
(10 000 segments/chunk), so segment i lives in chunk i//C and ONE index row per (granule, beam, chunk, h3_cell)
carries the byte range of every dataset for that chunk. Index-only: we read the granule's metadata + the segment
lat/lon (to assign cells); the elevation/time/quality bulk is fetched later, on demand, by byte range.

Prototype (vertical slice) for extending the ATL03 sub-granule index to the time-series collections.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import h3
import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache
from .index import BEAMS, _chunk_manifest, _filters, strong_beams   # reuse the ATL03 HDF5 helpers verbatim

log = logging.getLogger(__name__)

H3_RES = 6
ATL06_RES = 5   # per-collection choice: ATL06's 400 km chunks make res 5 the sweet spot (finer buys ~nothing on
                # scene-sized queries but multiplies index/scan cost) — see the resolution analysis.
ATL06_INDEX_VERSION = "1"
ATL06_DATASETS = ("latitude", "longitude", "h_li", "delta_time", "atl06_quality_summary")
ATL06_INDEX_DIR = cache.DATA_DIR / "index" / "atl06"


def _index_dir(res: int):
    return ATL06_INDEX_DIR / f"res{res}"


def indexed_atl06_granules(res: int = ATL06_RES) -> set[str]:
    """Granule names (stems, with .h5) already indexed at this res with the current schema — for resumable builds."""
    out = set()
    d = _index_dir(res)
    for p in (d.glob("*.parquet") if d.exists() else []):
        meta = pq.read_schema(p).metadata or {}
        if meta.get(b"aicesat_atl06_index_version", b"").decode() == ATL06_INDEX_VERSION:
            out.add(p.stem)
    return out
_NAME_RE = re.compile(r"ATL06_(\d{14})_(\d{4})(\d{2})(\d{2})_(\d{3})_(\d{2})\.h5")


def parse_granule_name(name: str) -> dict:
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"unexpected ATL06 granule name {name}")
    return {"start": m.group(1), "rgt": int(m.group(2)), "cycle": int(m.group(3)), "region": int(m.group(4)),
            "version": int(m.group(5)), "release": int(m.group(6))}


def build_atl06_index(granule, res: int = ATL06_RES) -> pa.Table:
    """Parse one ATL06 granule's structure (the only time its HDF5 b-trees are read) into addressing rows."""
    auth.login()
    from .access import RangeReader, access_url, cloud_hdf5_file, decode_chunk
    from .coverage import granule_name

    url = granule.data_links()[0]
    name = granule_name(granule)
    info = parse_granule_name(name)
    s3 = (granule.data_links(access="direct") or [""])[0]
    t0 = time.time()
    base_cols = ["granule", "url", "s3url", "revision", "sc_orient", "sdp_epoch", "beam", "strong", "cycle", "rgt",
                 "chunk_index", "seg_start", "seg_end", "h3_cell", "lat_min", "lat_max", "lon_min", "lon_max"]
    rows: dict[str, list] = {k: [] for k in base_cols}
    for ds in ATL06_DATASETS:
        for k in ("offset", "size", "filters", "dtype", "ncols", "mask"):
            rows[f"{ds}_{k}"] = []

    reader = RangeReader()
    with h5py.File(cloud_hdf5_file(url, s3, reader=reader), "r") as f:   # in-region: s3fs; else one shared presign
        sc_orient = int(f["orbit_info/sc_orient"][0])
        sdp = float(f["ancillary_data/atlas_sdp_gps_epoch"][0])
        strong = strong_beams(sc_orient)

        def read_via_chunks(ds: h5py.Dataset) -> np.ndarray:
            infos = _chunk_manifest(ds)
            fl = _filters(ds)
            raws = reader.fetch(access_url(url, s3), [(int(ci.byte_offset), int(ci.size)) for ci in infos])
            parts = [decode_chunk(raw, str(ds.dtype), fl, 1, int(ci.filter_mask)) for raw, ci in zip(raws, infos)]
            return np.concatenate(parts)[: ds.shape[0]]

        for beam in BEAMS:
            base = f"{beam}/land_ice_segments"
            if base not in f or f"{base}/latitude" not in f:
                continue
            lis = f[base]
            dsets = {d: lis[d] for d in ATL06_DATASETS}
            C = int(dsets["latitude"].chunks[0])
            nchunks = dsets["latitude"].id.get_num_chunks()
            for d, ds in dsets.items():
                if int(ds.chunks[0]) != C or ds.id.get_num_chunks() != nchunks:
                    raise ValueError(f"{beam}/{d}: chunking differs from latitude ({ds.chunks[0]} vs {C})")
                if ds.ndim > 1:
                    raise ValueError(f"{beam}/{d}: unexpected 2-D land-ice-segment dataset")
                bad = [x for x in _filters(ds).split(",") if x and x not in ("gzip", "shuffle")]
                if bad:
                    raise ValueError(f"{beam}/{d}: unsupported HDF5 filters {bad}")
            lat = read_via_chunks(dsets["latitude"])
            lon = read_via_chunks(dsets["longitude"])
            n = int(lat.shape[0])
            manifests = {d: _chunk_manifest(ds) for d, ds in dsets.items()}
            meta = {d: (_filters(ds), str(ds.dtype), 1) for d, ds in dsets.items()}

            seg = np.arange(n)
            ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90)
            ks = (seg[ok] // C).astype("i8")
            latok, lonok = lat[ok].astype("f8"), lon[ok].astype("f8")
            try:
                from h3ronpy.vector import coordinates_to_cells
                cells = np.asarray(coordinates_to_cells(latok, lonok, res), dtype="u8")
            except Exception:
                cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), res)) for a, o in zip(latok, lonok)], dtype="u8")

            box = {}
            for k in np.unique(ks):
                m = ks == k
                box[int(k)] = (float(latok[m].min()), float(latok[m].max()), float(lonok[m].min()), float(lonok[m].max()))

            for k, cell in sorted(set(zip(ks.tolist(), cells.tolist()))):
                assert h3.is_valid_cell(h3.int_to_str(int(cell))), cell
                rows["granule"].append(name); rows["url"].append(url); rows["s3url"].append(s3)
                rows["revision"].append(str(granule["meta"].get("revision-id", ""))); rows["sc_orient"].append(sc_orient)
                rows["sdp_epoch"].append(sdp); rows["beam"].append(beam); rows["strong"].append(beam in strong)
                rows["cycle"].append(info["cycle"]); rows["rgt"].append(info["rgt"])
                rows["chunk_index"].append(int(k)); rows["seg_start"].append(int(k * C)); rows["seg_end"].append(int(min((k + 1) * C, n)))
                rows["h3_cell"].append(int(cell))
                b = box[int(k)]
                rows["lat_min"].append(b[0]); rows["lat_max"].append(b[1]); rows["lon_min"].append(b[2]); rows["lon_max"].append(b[3])
                for d in ATL06_DATASETS:
                    ci = manifests[d][int(k)]
                    fl, dt, nc = meta[d]
                    rows[f"{d}_offset"].append(int(ci.byte_offset)); rows[f"{d}_size"].append(int(ci.size))
                    rows[f"{d}_filters"].append(fl); rows[f"{d}_dtype"].append(dt); rows[f"{d}_ncols"].append(nc); rows[f"{d}_mask"].append(int(ci.filter_mask))

    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in rows.items()})
    tbl = tbl.replace_schema_metadata({"aicesat_atl06_index_version": ATL06_INDEX_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat()})
    d = _index_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.parquet.tmp"
    pq.write_table(tbl, tmp)
    tmp.replace(d / f"{name}.parquet")   # atomic rename: a concurrent reader never sees a partial file
    log.info("indexed ATL06 %s: %d (chunk,cell) rows, %d beams, %.1fs (%d GETs, %.1f MB)",
             name, tbl.num_rows, len({*rows["beam"]}), time.time() - t0, reader.stats.requests, reader.stats.bytes / 1e6)
    return tbl


def _atlas_epoch_years(delta_time: np.ndarray, sdp_epoch_gps_s: float) -> np.ndarray:
    """delta_time (s since the ATLAS SDP epoch) -> datetime64[ms]; sdp epoch is GPS seconds since 1980-01-06."""
    gps0 = np.datetime64("1980-01-06T00:00:00", "ms")
    base = gps0 + np.timedelta64(int(round(sdp_epoch_gps_s * 1000)), "ms")
    return base + (delta_time * 1000.0).astype("timedelta64[ms]")


def fetch_bbox(bbox, window=None, res: int = ATL06_RES, strong_only: bool = True, quality_zero: bool = True) -> tuple[dict, dict]:
    """Index-driven ATL06 fetch: only the chunks whose cells touch the bbox are byte-range fetched and decoded —
    no whole-granule downloads. Returns (arrays, stats). `window` filters granules by their start time (YYYY-MM-DD)."""
    import duckdb

    from . import planner
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, RangeReader, access_url,
                         decode_chunk, pool_size)

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no ATL06 index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res)
    w, s, e, n = bbox

    # DuckDB pushes the cell predicate into the Parquet scan, so only the (granule, beam, chunk) refs whose
    # cell touches the bbox come back — one row per chunk, never a full read of every index file.
    dscols = ", ".join(f"{ds}_offset, {ds}_size, {ds}_dtype, {ds}_filters, {ds}_mask" for ds in ATL06_DATASETS)
    where = f"h3_cell IN ({','.join(str(int(c)) for c in want_cells)})"
    if strong_only:
        where += " AND strong"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND substr(granule, 7, 8) BETWEEN '{lo}' AND '{hi}'"
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT url, s3url, sdp_epoch, beam, chunk_index, seg_start, seg_end, {dscols} "
                           f"FROM read_parquet('{d}/*.parquet') WHERE {where}").fetchall()
    finally:
        con.close()
    if not rows:
        return {k: np.array([]) for k in ("lon", "lat", "h", "t", "quality")}, {}
    cols = ["url", "s3url", "sdp_epoch", "beam", "chunk_index", "seg_start", "seg_end"]
    for ds in ATL06_DATASETS:
        cols += [f"{ds}_offset", f"{ds}_size", f"{ds}_dtype", f"{ds}_filters", f"{ds}_mask"]
    by_url: dict[str, list] = {}
    for r in rows:
        rec = dict(zip(cols, r)); by_url.setdefault(access_url(rec["url"], rec["s3url"]), []).append(rec)

    # In-region the by_url keys are s3:// (S3-direct, no presign); out-of-region they are HTTPS and we presign all
    # granules up front in parallel (avoids the serial ~1.7 s x N), then fetch per-granule with a warm presign.
    reader = RangeReader()
    reader.presign_all([u for u in by_url if not u.startswith("s3://")])

    def _fetch_granule(url) -> dict:
        """Fetch + decode + filter one granule's chunks; returns its sub-arrays in the original within-granule order.
        Independent per granule — the shared reader is concurrency-safe (locked stats, per-URL presign locks)."""
        rs = by_url[url]
        ranges, keys = [], []
        for r in rs:
            for ds in ATL06_DATASETS:
                ranges.append((r[f"{ds}_offset"], r[f"{ds}_size"])); keys.append((r["beam"], r["chunk_index"], ds))
        raws = dict(zip(keys, reader.fetch(url, ranges)))   # warm presign -> just byte-range GETs
        local = {k: [] for k in ("lon", "lat", "h", "t", "quality")}
        for r in rs:
            seg_n = r["seg_end"] - r["seg_start"]; sdp = r["sdp_epoch"]
            dec = {ds: decode_chunk(raws[(r["beam"], r["chunk_index"], ds)], r[f"{ds}_dtype"], r[f"{ds}_filters"], 1, r[f"{ds}_mask"])[:seg_n] for ds in ATL06_DATASETS}
            lat, lon, h, dt, q = dec["latitude"], dec["longitude"], dec["h_li"], dec["delta_time"], dec["atl06_quality_summary"]
            m = np.isfinite(h) & (h < 3.0e38) & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
            if quality_zero:
                m &= (q == 0)
            if not m.any():
                continue
            local["lon"].append(lon[m].astype("f8")); local["lat"].append(lat[m].astype("f8")); local["h"].append(h[m].astype("f8"))
            local["t"].append(_atlas_epoch_years(dt[m], sdp)); local["quality"].append(q[m])
        return local

    # Each granule's fetch+decode is independent and network-I/O-bound, so run several granules concurrently (each
    # still fetches its own ranges concurrently inside reader.fetch). Results are reassembled in the original
    # by_url order below, so the concatenated arrays are byte-identical to the serial version.
    urls = list(by_url)
    nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV)
    if nw == 1:
        parts = [_fetch_granule(u) for u in urls]
    else:
        with ThreadPoolExecutor(nw) as ex:
            parts = list(ex.map(_fetch_granule, urls))   # ex.map preserves urls order
    out = {k: [] for k in ("lon", "lat", "h", "t", "quality")}
    for loc in parts:
        for k in out:
            out[k].extend(loc[k])
    arrays = {k: (np.concatenate(v) if v else np.array([])) for k, v in out.items()}
    return arrays, reader.stats.as_dict()
