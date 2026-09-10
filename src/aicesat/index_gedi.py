"""GEDI L2A (GEDI02_A v003) addressing index — the ATL06 pattern, generalised for non-uniform chunking.

Every other collection's index rests on one assumption: all of a beam's datasets share a chunking, so a single
chunk_index addresses every one of them (index_atl06 raises if they differ). GEDI breaks it. Measured on a real
V003 granule, one beam carries:

    lat_lowestmode / lon_lowestmode   2,608 shots per chunk   (64 chunks)
    elev_lowestmode / sensitivity     5,216                   (32)
    l2a_quality_flag_rel3            10,432                   (16)
    degrade_flag                    100,000                   (2)

a 38x spread. So a row is keyed on a SPAN rather than a chunk: cut the beam at the union of every dataset's chunk
boundaries (chunk_spans), and each resulting span lies inside exactly one chunk of each dataset. The row schema
stays flat — one byte range per dataset — with no straddling special case. The segmentation costs almost nothing:
2,608 divides 5,216 and 10,432, so only the misaligned 100,000 adds boundaries, and 64 chunks become ~66 spans.

`delta_time` is deliberately NOT addressed. It is the worst-chunked dataset (100,000/chunk, and 100,000 is not a
multiple of 2,608), and the date is already in the granule name — so the time comes from there, exactly as
index_glas takes gdate from its first shot. A GEDI granule is a quarter orbit, ~15 minutes, which is finer than
any binning the time series does.

Two version traps, both of which bit a hand-rolled read of this same data:
  * V002 and V003 are the SAME acquisitions -- V003 is a reprocessing. Indexing both double-counts every shot, so
    the builder takes V003 only.
  * V003 renamed `quality_flag` to `l2a_quality_flag_rel3`. Keying on the V002 name alone silently drops every
    V003 granule (51 of 98 over the Langtang box) and biases what survives toward 2019.

Heights are `elev_lowestmode`, "elevation of center of lowest mode relative to reference ellipsoid" (the granule's
own attribute) — WGS84 ellipsoidal, directly comparable to ATL06/ATL03 and to the ellipsoid-corrected GLAS, with
no datum conversion.

CAVEAT, measured, not inherited: GEDI's accuracy is strongly slope-dependent. Against the HMA 8 m DEM over
Langtang, MAD by DEM slope ran 2.7 m (0-15 deg), 5.2, 6.6, 7.3, 14.7 m (50 deg+), with the median residual sliding
from -4.0 m to -12.4 m — while ATL06 stayed flat at 2.9-4.5 m over the same ground and the same DEM. That is the
footprint: `elev_lowestmode` is the LOWEST return within ~25 m, which on a steep face is its downhill edge. Below
about 15 degrees GEDI beats ATL06 here; above 30 it should not be trusted for elevation change.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import h3
import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache
from . import index as index_mod
from .index import _chunk_manifest, _filters

log = logging.getLogger(__name__)

GEDI_RES = 5                # scene-sized queries, same rationale as ATL06/GLAS
GEDI_INDEX_VERSION = "1"
GEDI_INDEX_DIR = cache.DATA_DIR / "index" / "gedi"
MISSION = "GEDI"

BEAMS = ("BEAM0000", "BEAM0001", "BEAM0010", "BEAM0011", "BEAM0101", "BEAM0110", "BEAM1000", "BEAM1011")
FULL_POWER = ("BEAM0101", "BEAM0110", "BEAM1000", "BEAM1011")   # the rest are coverage beams

# our key -> the dataset name inside a beam group. The quality flag is resolved per granule (see quality_field).
GEDI_DATASETS = (("lat", "lat_lowestmode"), ("lon", "lon_lowestmode"), ("elev", "elev_lowestmode"),
                 ("sens", "sensitivity"), ("degrade", "degrade_flag"), ("qual", None))
GEDI_KEYS = [k for k, _ in GEDI_DATASETS]
_FLOAT_KEYS = {"lat", "lon", "elev", "sens"}

MIN_SENSITIVITY = 0.9       # GEDI's own guidance for a usable waveform; quality_flag already folds it in, kept explicit
_EMPTY = ("lon", "lat", "h", "t", "quality")

_NAME_RE = re.compile(r"GEDI02_A_(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})_O(\d+)_(\d+)_T(\d+)_\d+_(\d+)_\d+_V(\d+)\.h5")


def _index_dir(res: int):
    return GEDI_INDEX_DIR / f"res{res}"


def parse_granule_name(name: str) -> dict:
    """GEDI02_A_YYYYDDDHHMMSS_Oorbit_sub_Ttrack_ppds_rrr_ver_VVV.h5 -> the fields the index keys and filters on."""
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"unexpected GEDI granule name {name}")
    yr, doy, hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
    d = date(yr, 1, 1) + timedelta(days=doy - 1)          # day-of-year, so leap years take care of themselves
    return {"gdate": d.strftime("%Y%m%d"), "gtime": f"{hh:02d}{mm:02d}{ss:02d}", "orbit": int(m.group(6)),
            "sub": int(m.group(7)), "track": int(m.group(8)), "version": int(m.group(10))}


def quality_field(available) -> str:
    """The name of the L2A quality flag in THIS granule. V003 renamed it; prefer the current release when both exist."""
    for n in ("l2a_quality_flag_rel3", "l2a_quality_flag_rel2", "quality_flag"):
        if n in available:
            return n
    raise KeyError(f"no L2A quality flag among {sorted(available)}")


def chunk_spans(chunk_sizes: dict, n: int) -> list:
    """Cut [0, n) at the UNION of every dataset's chunk boundaries.

    Each returned span therefore lies inside exactly one chunk of every dataset, which is what lets one flat index
    row carry a single byte range per dataset. Returns [(start, end), ...], contiguous and covering [0, n)."""
    if n <= 0:
        return []
    cuts = {0, n}
    for c in chunk_sizes.values():
        c = int(c)
        if c <= 0:
            continue
        cuts.update(range(c, n, c))
    edges = sorted(cuts)
    return [(a, b) for a, b in zip(edges, edges[1:]) if b > a]


def chunk_of(span, chunk_size: int) -> int:
    """Index of the chunk containing `span`. The span is guaranteed (by chunk_spans) not to straddle a boundary."""
    i0, i1 = span
    k = i0 // int(chunk_size)
    assert (i1 - 1) // int(chunk_size) == k, f"span {span} straddles a {chunk_size}-chunk boundary"
    return k


def span_offset(seg_start: int, chunk_size: int) -> int:
    """Where a span begins INSIDE the chunk that contains it.

    Each dataset's chunk is wider than the span (that is the point of chunk_spans), so a decoded chunk has to be
    sliced back to the span before anything is combined. Getting this wrong does not raise -- it silently pairs a
    latitude with another shot's elevation."""
    if chunk_size <= 0:
        return 0
    return int(seg_start) - (int(seg_start) // int(chunk_size)) * int(chunk_size)


def _fill(ds: h5py.Dataset) -> float:
    v = ds.attrs.get("_FillValue")
    try:
        return float(np.asarray(v).ravel()[0]) if v is not None else np.nan
    except Exception:
        return np.nan


def _nan_fill(a: np.ndarray, fill: float) -> np.ndarray:
    a = a.astype("f8")
    if np.isfinite(fill):
        a = np.where(a == fill, np.nan, a)
    return np.where(np.abs(a) > 1e30, np.nan, a)          # GEDI also uses very large sentinels


def indexed_gedi_granules(res: int = GEDI_RES) -> set[str]:
    """Granule stems already indexed at this res with the current schema — for resumable builds."""
    out, stale = set(), False
    d = _index_dir(res)
    for p in (d.glob("*.parquet") if d.exists() else []):
        try:
            meta = pq.read_schema(p).metadata or {}
        except Exception as e:
            log.warning("index %s is unreadable (%s); rebuilding", p.name, e)
            p.unlink(missing_ok=True); stale = True; continue
        if meta.get(b"aicesat_gedi_index_version", b"").decode() == GEDI_INDEX_VERSION:
            out.add(p.stem)
        else:
            log.warning("index %s has an old schema; rebuilding", p.name)
            p.unlink(); stale = True
    if stale:
        index_mod.invalidate_claim(d, "granule files were rebuilt for a new schema version")
    return out


def build_gedi_index(granule, res: int = GEDI_RES, cells=None) -> pa.Table:
    """Parse one GEDI granule's structure into addressing rows (the only time its HDF5 b-trees are read)."""
    auth.login()
    keep = index_mod.cells_filter(cells, res)
    from .access import RangeReader, access_url, cloud_hdf5_file, decode_chunk
    from .coverage import granule_name

    url = granule.data_links()[0]
    name = granule_name(granule)
    info = parse_granule_name(name)
    t0 = time.time()
    s3 = (granule.data_links(access="direct") or [""])[0]

    base_cols = ["granule", "url", "s3url", "gdate", "beam", "full_power", "orbit", "track",
                 "span_index", "seg_start", "seg_end", "h3_cell", "lat_min", "lat_max", "lon_min", "lon_max"]
    rows: dict[str, list] = {k: [] for k in base_cols}
    for key in GEDI_KEYS:
        for suf in ("offset", "size", "filters", "dtype", "mask", "fill"):
            rows[f"{key}_{suf}"] = []

    reader = RangeReader()
    with h5py.File(cloud_hdf5_file(url, s3, reader=reader), "r") as f:
        for beam in BEAMS:
            if beam not in f or "lat_lowestmode" not in f[beam]:
                continue
            qname = quality_field(set(f[beam].keys()))
            names = {k: (qname if k == "qual" else n) for k, n in GEDI_DATASETS}
            if any(names[k] not in f[beam] for k in GEDI_KEYS):
                log.warning("%s %s: missing %s; beam skipped", name, beam,
                            [k for k in GEDI_KEYS if names[k] not in f[beam]])
                continue
            dsets = {k: f[f"{beam}/{names[k]}"] for k in GEDI_KEYS}
            n = int(dsets["lat"].shape[0])
            for k, ds in dsets.items():
                if ds.ndim > 1:
                    raise ValueError(f"{beam}/{names[k]}: unexpected {ds.ndim}-D dataset")
                bad = [x for x in _filters(ds).split(",") if x and x not in ("gzip", "shuffle")]
                if bad:
                    raise ValueError(f"{beam}/{names[k]}: unsupported HDF5 filters {bad}")

            def read_all(ds):
                infos = _chunk_manifest(ds)
                fl = _filters(ds)
                raws = reader.fetch(access_url(url, s3), [(int(ci.byte_offset), int(ci.size)) for ci in infos])
                parts = [decode_chunk(raw, str(ds.dtype), fl, 1, int(ci.filter_mask)) for raw, ci in zip(raws, infos)]
                return np.concatenate(parts)[:ds.shape[0]]

            lat = _nan_fill(read_all(dsets["lat"]), _fill(dsets["lat"]))
            lon = _nan_fill(read_all(dsets["lon"]), _fill(dsets["lon"]))
            lon = np.where(lon > 180, lon - 360, lon)
            sizes = {k: int(ds.chunks[0]) for k, ds in dsets.items() if ds.chunks}
            spans = chunk_spans(sizes, n)
            manifests = {k: _chunk_manifest(ds) for k, ds in dsets.items()}
            meta = {k: (_filters(ds), str(ds.dtype), _fill(ds)) for k, ds in dsets.items()}

            ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90)
            idx = np.flatnonzero(ok)
            if idx.size == 0:
                continue
            try:
                from h3ronpy.vector import coordinates_to_cells
                cell_ids = np.asarray(coordinates_to_cells(lat[idx], lon[idx], res), dtype="u8")
            except Exception:
                cell_ids = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), res))
                                     for a, o in zip(lat[idx], lon[idx])], dtype="u8")
            span_of = np.searchsorted(np.array([s[0] for s in spans]), idx, side="right") - 1

            for si, (i0, i1) in enumerate(spans):
                m = span_of == si
                if not m.any():
                    continue
                sidx, scell = idx[m], cell_ids[m]
                for cell in sorted(set(scell.tolist())):
                    if keep is not None and int(cell) not in keep:
                        continue
                    cm = scell == cell
                    la, lo = lat[sidx[cm]], lon[sidx[cm]]
                    rows["granule"].append(name); rows["url"].append(url); rows["s3url"].append(s3)
                    rows["gdate"].append(info["gdate"]); rows["beam"].append(beam)
                    rows["full_power"].append(beam in FULL_POWER)
                    rows["orbit"].append(info["orbit"]); rows["track"].append(info["track"])
                    rows["span_index"].append(si); rows["seg_start"].append(i0); rows["seg_end"].append(i1)
                    rows["h3_cell"].append(int(cell))
                    rows["lat_min"].append(float(la.min())); rows["lat_max"].append(float(la.max()))
                    rows["lon_min"].append(float(lo.min())); rows["lon_max"].append(float(lo.max()))
                    for k in GEDI_KEYS:
                        ci = manifests[k][chunk_of((i0, i1), sizes[k])]
                        fl, dt, fv = meta[k]
                        rows[f"{k}_offset"].append(int(ci.byte_offset)); rows[f"{k}_size"].append(int(ci.size))
                        rows[f"{k}_filters"].append(fl); rows[f"{k}_dtype"].append(dt)
                        rows[f"{k}_mask"].append(int(ci.filter_mask)); rows[f"{k}_fill"].append(float(fv))

    tbl = index_mod.typed_table(rows)
    tbl = tbl.replace_schema_metadata({"aicesat_gedi_index_version": GEDI_INDEX_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat(),
                                       **index_mod.cells_metadata(cells)})
    d = _index_dir(res); d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.parquet.tmp"
    pq.write_table(tbl, tmp)
    tmp.replace(d / f"{name}.parquet")
    log.info("indexed GEDI %s: %d (span,cell) rows, %d beams, %.1fs (%d GETs, %.1f MB)",
             name, tbl.num_rows, len({*rows["beam"]}), time.time() - t0, reader.stats.requests,
             reader.stats.bytes / 1e6)
    return tbl


# --- query path ------------------------------------------------------------------------------------------------
def _index_rows(bbox, window, res: int, full_power_only: bool = False, polygon=None) -> tuple[list[int], list[dict]]:
    """The (granule, beam, span, cell) refs whose cell touches the selection. DuckDB pushes the cell predicate into
    the Parquet scan. `window` filters on gdate, which the granule name gave us — no delta_time is addressed."""
    import duckdb

    from . import coverage, planner

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no GEDI index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res, polygon=polygon)
    cols = ["granule", "url", "s3url", "gdate", "beam", "span_index", "seg_start", "seg_end", "h3_cell"]
    for key in GEDI_KEYS:
        cols += [f"{key}_offset", f"{key}_size", f"{key}_dtype", f"{key}_filters", f"{key}_mask", f"{key}_fill"]
    where = f"h3_cell IN ({','.join(str(int(c)) for c in want_cells)})"
    if full_power_only:
        where += " AND full_power"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND gdate BETWEEN '{lo}' AND '{hi}'"
    files = coverage.index_files_for_cells("GEDI", want_cells)
    if files is not None and not files:
        return want_cells, []
    src = coverage.read_parquet_src(d, files)
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT {', '.join(cols)} FROM {src} WHERE {where}").fetchall()
    finally:
        con.close()
    return want_cells, [dict(zip(cols, r)) for r in rows]


def _decode_span(raws: dict, r: dict) -> dict:
    """Decode one span's arrays for every dataset, fill-clean them, and reconstruct the display fields (pre-mask).

    Each dataset's chunk covers a WIDER range than the span (that is the whole point of the span segmentation), so
    every array is sliced back to the span's own offset within its chunk before anything is combined."""
    from .access import decode_chunk

    out = {}
    for key in GEDI_KEYS:
        a = decode_chunk(raws[(r["beam"], r["span_index"], key)], r[f"{key}_dtype"], r[f"{key}_filters"], 1,
                         r[f"{key}_mask"])
        # where does this span sit inside the chunk we just decoded?
        off = span_offset(r["seg_start"], len(a))
        a = a[off:off + (r["seg_end"] - r["seg_start"])]
        out[key] = _nan_fill(a, r[f"{key}_fill"]) if key in _FLOAT_KEYS else a
    lon = np.where(out["lon"] > 180, out["lon"] - 360, out["lon"])
    t = np.full(out["lat"].shape, np.datetime64(f"{r['gdate'][:4]}-{r['gdate'][4:6]}-{r['gdate'][6:]}", "ms"))
    valid = (np.isfinite(out["elev"]) & np.isfinite(out["lat"]) & np.isfinite(lon)
             & (out["qual"] == 1) & (out["degrade"] == 0))
    return {"lat": out["lat"], "lon": lon, "h": out["elev"], "t": t,
            "quality": np.where(out["qual"] == 1, 0, 1).astype("i1"), "sens": out["sens"], "valid": valid}


def plan_bbox(bbox, window=None, res: int = GEDI_RES, full_power_only: bool = False, force: bool = False,
              polygon=None, settle: bool = True) -> dict:
    """What a fetch over (bbox|polygon, window) WOULD read, decided without touching the network. Mirrors
    index_atl06.plan_bbox key for key, with `span` where ATL06 has `chunk`."""
    from . import lake
    from .access import access_url

    want_cells, rows = _index_rows(bbox, window, res, full_power_only, polygon=polygon)
    out = {"want_cells": want_cells, "rows": rows,
           "names": sorted({r["granule"] for r in rows}), "beams": sorted({r["beam"] for r in rows}),
           "chunk_cells": {}, "chunk_row": {}, "have": set(), "todo": [], "n_lake": 0, "by_url": {},
           "want_only": tuple(sorted(int(c) for c in want_cells))}
    if not rows:
        return out
    if settle:
        lake.drain_writes(MISSION, want_cells)
    have = set() if force else lake.ingested_chunk_cells(MISSION, out["names"])
    chunk_cells, chunk_row = {}, {}
    for r in rows:
        k = (r["granule"], r["beam"], r["span_index"])
        chunk_cells.setdefault(k, set()).add(int(r["h3_cell"])); chunk_row.setdefault(k, r)
    todo = [k for k, cs in chunk_cells.items() if any((k[0], k[1], k[2], c) not in have for c in cs)]
    by_url: dict[str, list] = {}
    for k in todo:
        r = chunk_row[k]; by_url.setdefault(access_url(r["url"], r["s3url"]), []).append(r)
    out.update(chunk_cells=chunk_cells, chunk_row=chunk_row, have=have, todo=todo,
               n_lake=len(chunk_cells) - len(todo), by_url=by_url)
    return out


def fetch_bbox(bbox, window=None, res: int = GEDI_RES, full_power_only: bool = False, quality_only: bool = True,
               force: bool = False, clip_cells: bool = False, polygon=None, on_granule=None,
               on_plan=None) -> tuple[dict, dict]:
    """Lake-first index-driven GEDI fetch — the same contract as index_atl06.fetch_bbox (read the lake BEFORE any
    new write, return fresh points from memory, queue the Parquet write to the background writer)."""
    from . import lake, planner
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, AccessStats, RangeReader,
                         access_url, pool_size)

    plan = plan_bbox(bbox, window, res, full_power_only, force=force, polygon=polygon)
    want_cells, rows = plan["want_cells"], plan["rows"]
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {"chunks_from_lake": 0, "chunks_from_nasa": 0,
                                                   "cells": len(want_cells)}
    names, beams, have = plan["names"], plan["beams"], plan["have"]
    want_arr = np.asarray(sorted(int(c) for c in want_cells), dtype="u8")
    _stream = (lambda r: on_granule({"granule": "lake", **r})) if on_granule is not None else None
    cached = None if force else lake.query_points(
        bbox, want_cells, MISSION, granules=names, beams=beams, extra_cols=("quality",),
        quality_zero=quality_only, clip_cells=clip_cells, on_batch=_stream)

    chunk_cells, chunk_row = plan["chunk_cells"], plan["chunk_row"]
    todo, n_lake, want_only = plan["todo"], plan["n_lake"], plan["want_only"]
    reader, fresh_parts = None, []
    if todo:
        reader = RangeReader()
        by_url = plan["by_url"]
        reader.presign_all([u for u in by_url if not u.startswith("s3://")])
        if on_plan is not None:
            on_plan({"granules": len(by_url), "chunks": len(todo), "cached": n_lake})

        def _keep(mats, dup_cells):
            lon, lat = mats["lon"], mats["lat"]
            if lon.size == 0:
                return np.zeros(0, bool)
            pcell = planner._cells_vectorized(lat, lon, res)
            keep = np.isin(pcell, want_arr)
            if dup_cells:
                keep &= ~np.isin(pcell, np.asarray(sorted(dup_cells), dtype="u8"))
            if not clip_cells:
                w, s, e, n = bbox
                keep &= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
            if quality_only:
                keep &= (mats["quality"] == 0)
            return keep

        def _ingest_granule(url) -> dict:
            rs = by_url[url]
            ranges, keys = [], []
            for r in rs:
                for key in GEDI_KEYS:
                    ranges.append((r[f"{key}_offset"], r[f"{key}_size"]))
                    keys.append((r["beam"], r["span_index"], key))
            raws = dict(zip(keys, reader.fetch(url, ranges)))
            writes, out = [], {}
            for r in rs:
                dec = _decode_span(raws, r)
                v = dec["valid"]
                mats = {"lon": dec["lon"][v].astype("f8"), "lat": dec["lat"][v].astype("f8"),
                        "h": dec["h"][v].astype("f8"), "t": dec["t"][v], "quality": dec["quality"][v]}
                k = (r["granule"], r["beam"], r["span_index"])
                writes.append(lake.ChunkWrite(r["granule"], r["beam"], r["span_index"], mats,
                                              only_cells=want_only, mark_cells=tuple(sorted(chunk_cells[k]))))
                keep = _keep(mats, {c for c in chunk_cells[k] if (k[0], k[1], k[2], c) in have})
                g = out.setdefault(r["granule"], {kk: [] for kk in _EMPTY})
                for kk in _EMPTY:
                    g[kk].append(mats[kk][keep])
            lake.submit_writes(MISSION, res, writes, want_cells, extras=("quality",))
            return {g: {kk: np.concatenate(v) for kk, v in dd.items()} for g, dd in out.items()}

        urls = list(by_url)
        nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV,
                       cpu_bound=False)
        parts = [_ingest_granule(u) for u in urls] if nw == 1 else list(ThreadPoolExecutor(nw).map(_ingest_granule, urls))
        for loc in parts:
            for g, dd in loc.items():
                fresh_parts.append(dd)
                if on_granule is not None:
                    on_granule({"granule": g, **{kk: dd[kk] for kk in ("lon", "lat", "h", "t")}})
        if not lake.async_writes_enabled():
            lake.drain_writes(MISSION, want_cells)
    elif on_plan is not None:
        on_plan({"granules": 0, "chunks": 0, "cached": n_lake})

    arrays = lake.concat_arrays([cached, *fresh_parts], _EMPTY)
    if reader:
        lake.enforce_global_limit_async(protect=want_cells, reason="limit (GEDI fetch)")
    st = reader.stats.as_dict() if reader else AccessStats().as_dict()
    st.update({"chunks_from_lake": n_lake, "chunks_from_nasa": len(todo), "chunks_fetched": len(todo),
               "cells": len(want_cells), "evicted_for_limit": [], "res": res})
    return arrays, st
