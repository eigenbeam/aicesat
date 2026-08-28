"""IceBridge ATM L2 icessn (ILATM2 v2) addressing index — the sub-granule idea applied to a CSV.

ILATM2 granules are comma-delimited text with variable-length lines, so there is no HDF5 chunk to address: instead we
build a LINE-OFFSET index. At build we scan each file once, assign every nadir platelet (track == 0) to an H3 cell,
and record the byte span of the lines in each cell. At query time we byte-range GET only those spans, re-split into
whole lines, and re-parse/filter — no whole-file download, and no CMR round trip (the index is the discovery layer).

The date comes from the filename (ILATM2_YYYYMMDD_...). Height is `elevation` (WGS84 ellipsoid, no datum conversion),
kept where track == 0 and plane-fit RMS < 50 cm, exactly as icessn.py.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache
from .icessn import MAX_RMS_CM, _NAME_RE

log = logging.getLogger(__name__)

ICESSN_RES = 5   # match ATL06/GLAS so a query cell maps to the same index cells across missions
ICESSN_INDEX_VERSION = "1"
ICESSN_INDEX_DIR = cache.DATA_DIR / "index" / "icessn"


def _index_dir(res: int):
    return ICESSN_INDEX_DIR / f"res{res}"


def indexed_icessn_granules(res: int = ICESSN_RES) -> set[str]:
    out = set()
    d = _index_dir(res)
    for p in (d.glob("*.parquet") if d.exists() else []):
        meta = pq.read_schema(p).metadata or {}
        if meta.get(b"aicesat_icessn_index_version", b"").decode() == ICESSN_INDEX_VERSION:
            out.add(p.stem)
    return out


def _parse_fields(ln: bytes):
    """(lat, lon_180, elev, rms, track) for a data line, or None if it is a comment/short/unparseable line."""
    if not ln or ln[:1] == b"#":
        return None
    f = ln.split(b",")
    if len(f) < 11:
        return None
    try:
        lat = float(f[1]); lon = float(f[2]); elev = float(f[3]); rms = float(f[6]); track = float(f[10])
    except ValueError:
        return None
    lon = ((lon + 180.0) % 360.0) - 180.0
    return lat, lon, elev, rms, track


def build_icessn_index(granule, res: int = ICESSN_RES, bbox=None) -> pa.Table:
    """Scan one ILATM2 CSV once (the only full read) into per-(cell) byte-span rows. Pass `bbox` to index only the
    nadir platelets inside it (a regional index)."""
    from .access import RangeReader, access_url

    auth.login()
    from .coverage import granule_name

    url = granule.data_links()[0]
    name = granule_name(granule)
    s3 = (granule.data_links(access="direct") or [""])[0]
    m = _NAME_RE.search(name)
    gdate = m.group(1) if m else "00000000"
    t0 = time.time()

    data = RangeReader().read_all(access_url(url, s3))   # in-region: S3-direct whole-file GET; else cloud presign+GET
    size = len(data)

    lats, lons, starts, ends = [], [], [], []
    pos = 0
    for ln in data.split(b"\n"):
        start = pos; pos = pos + len(ln) + 1        # +1 for the stripped newline
        p = _parse_fields(ln)
        if p is None:
            continue
        lat, lon, _elev, _rms, track = p
        if track != 0 or not (np.isfinite(lat) and np.isfinite(lon)):
            continue                                # index every nadir platelet; the rms<50 cut is re-applied at fetch
        if bbox is not None and not (bbox[1] <= lat <= bbox[3] and bbox[0] <= lon <= bbox[2]):
            continue                                # regional index: only platelets inside the build bbox
        lats.append(lat); lons.append(lon)
        starts.append(start); ends.append(min(pos, size))
    if not lats:   # no platelets inside bbox: write a schema-matched EMPTY parquet so the granule counts as done
        d = _index_dir(res)
        ref = next(iter(d.glob("*.parquet")), None)
        if ref is None:
            return pa.table({"granule": pa.array([], type=pa.string())})   # no sibling to match the schema yet; skip
        empty = pq.read_schema(ref).empty_table()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.parquet.tmp"
        pq.write_table(empty, tmp)
        tmp.replace(d / f"{name}.parquet")
        log.info("indexed ICESSN %s: 0 platelets in bbox -> empty parquet", name)
        return empty
    lat_a = np.asarray(lats, "f8"); lon_a = np.asarray(lons, "f8")
    st_a = np.asarray(starts, "i8"); en_a = np.asarray(ends, "i8")
    try:
        from h3ronpy.vector import coordinates_to_cells
        cell_a = np.asarray(coordinates_to_cells(lat_a, lon_a, res), dtype="u8")
    except Exception:
        import h3
        cell_a = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), res)) for a, o in zip(lat_a, lon_a)], dtype="u8")

    base = {k: [] for k in ("granule", "url", "s3url", "gdate", "h3_cell",
                            "byte_start", "byte_end", "n_lines", "lat_min", "lat_max", "lon_min", "lon_max")}
    for c in np.unique(cell_a):
        mk = cell_a == c
        base["granule"].append(name); base["url"].append(url); base["s3url"].append(s3); base["gdate"].append(gdate)
        base["h3_cell"].append(int(c))
        base["byte_start"].append(int(st_a[mk].min())); base["byte_end"].append(int(en_a[mk].max()))
        base["n_lines"].append(int(mk.sum()))
        base["lat_min"].append(float(lat_a[mk].min())); base["lat_max"].append(float(lat_a[mk].max()))
        base["lon_min"].append(float(lon_a[mk].min())); base["lon_max"].append(float(lon_a[mk].max()))

    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in base.items()})
    tbl = tbl.replace_schema_metadata({"aicesat_icessn_index_version": ICESSN_INDEX_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat()})
    d = _index_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.parquet.tmp"
    pq.write_table(tbl, tmp)
    tmp.replace(d / f"{name}.parquet")
    log.info("indexed ICESSN %s: %d cells over %d nadir platelets (%.1f KB scanned, %.1fs)",
             name, tbl.num_rows, len(lats), size / 1e3, time.time() - t0)
    return tbl


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union overlapping/adjacent [start, end) byte spans so each physical line is fetched and parsed exactly once."""
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def fetch_bbox(bbox, window=None, res: int = ICESSN_RES) -> tuple[dict, dict]:
    """Index-driven ILATM2 fetch: byte-range GET only the line spans whose cells touch the bbox, then re-parse/filter.
    Returns (arrays, stats). `window` filters granules by their filename date (YYYY-MM-DD)."""
    import duckdb

    from . import planner
    from .access import RangeReader, access_url

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no ICESSN index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res)
    w, s, e, n = bbox

    where = f"h3_cell IN ({','.join(str(int(c)) for c in want_cells)})"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND gdate BETWEEN '{lo}' AND '{hi}'"
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT url, s3url, gdate, byte_start, byte_end "
                           f"FROM read_parquet('{d}/*.parquet') WHERE {where}").fetchall()
    finally:
        con.close()
    empty = {k: np.array([]) for k in ("lon", "lat", "h", "t")}
    if not rows:
        return empty, {}
    by_url: dict[str, dict] = {}
    for url, s3url, gdate, bs, be in rows:
        u = by_url.setdefault(access_url(url, s3url), {"gdate": gdate, "spans": []})
        u["spans"].append((int(bs), int(be)))

    reader = RangeReader()   # in-region: s3:// keys (S3-direct); else HTTPS presigned up front
    reader.presign_all([u for u in by_url if not u.startswith("s3://")])
    out = {k: [] for k in ("lon", "lat", "h", "t")}
    for url, u in by_url.items():
        merged = _merge(u["spans"])                       # disjoint, line-aligned -> every line parsed once
        blobs = reader.fetch(url, [(a, b - a) for a, b in merged])
        t0 = np.datetime64(datetime.strptime(u["gdate"], "%Y%m%d").isoformat(), "ms")
        for blob in blobs:
            for ln in blob.split(b"\n"):
                p = _parse_fields(ln)
                if p is None:
                    continue
                lat, lon, elev, rms, track = p
                if track != 0 or not (np.isfinite(elev) and np.isfinite(lat) and np.isfinite(lon)) or rms >= MAX_RMS_CM:
                    continue
                if not (s <= lat <= n and w <= lon <= e):
                    continue
                sec = float(ln.split(b",")[0])
                out["lon"].append(lon); out["lat"].append(lat); out["h"].append(elev)
                out["t"].append(t0 + np.timedelta64(int(sec * 1000), "ms"))
    arrays = {"lon": np.asarray(out["lon"], "f8"), "lat": np.asarray(out["lat"], "f8"),
              "h": np.asarray(out["h"], "f8"), "t": np.asarray(out["t"], "datetime64[ms]") if out["t"] else np.array([])}
    return arrays, reader.stats.as_dict()
