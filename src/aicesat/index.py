"""Addressing index (spec §5): (mission, h3_cell, time_bucket) -> source chunk references, built ONCE per granule.

For ATL03 every heights/* dataset is chunked identically (100 000 photons per chunk, verified on v007), so one index
row per (granule, beam, chunk, h3_cell) carries the byte range of all five datasets. Cells are assigned from the
20 m segment geolocation: a chunk's photon range maps to the segments it covers, and their lat/lon to H3 cells.
The same chunk appears under every cell it touches (§5.3) — the residual lat/lon predicate is applied at query time.
"""
from __future__ import annotations

import logging
import pathlib
import re
import time
from datetime import datetime, timezone

import duckdb
import h3
import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache

log = logging.getLogger(__name__)

H3_RES = 6
INDEX_SCHEMA_VERSION = "4"  # v4: per-chunk lat/lon bounding boxes  # bump when index columns change; older files are rebuilt, never read
DATASETS = ("lat_ph", "lon_ph", "h_ph", "signal_conf_ph", "delta_time")
BEAMS = ("gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r")
INDEX_DIR = cache.DATA_DIR / "index"
ATL03_INDEX_DIR = INDEX_DIR / "atl03"
_NAME_RE = re.compile(r"ATL03_(\d{14})_(\d{4})(\d{2})(\d{2})_(\d{3})_(\d{2})\.h5")


# --- shared index-table typing -------------------------------------------------------------------------------------
# Every builder assembles python lists and hands them to pa.table(). With a CELL FILTER a granule can legitimately
# contribute zero rows (its track never enters the requested cells), and pa.array([]) is null-typed — a null column
# makes DuckDB's union_by_name reconcile schemas across files and fail. Declaring the type per column makes an empty
# granule's parquet schema-identical to a full one, which also retires the "copy the schema off a sibling file" hack
# (which had no answer for the FIRST granule of a fresh index).
_I64_NAMES = {"chunk_index", "ph_start", "ph_end", "seg_start", "seg_end", "cycle", "rgt", "sc_orient",
              "byte_start", "byte_end", "n_lines"}
_F64_NAMES = {"lat_min", "lat_max", "lon_min", "lon_max", "sdp_epoch"}


def col_type(name: str):
    """The declared type for a known index column, or None when we have no opinion (infer it)."""
    if name == "h3_cell":
        return pa.uint64()          # NEVER inferred: a python int infers as int64 and the cell ids need the full range
    if name == "strong":
        return pa.bool_()
    if name in _F64_NAMES or name.endswith("_fill"):
        return pa.float64()
    if name in _I64_NAMES or name.endswith(("_offset", "_size", "_mask", "_ncols")):
        return pa.int64()
    return None


def typed_table(rows: dict) -> pa.Table:
    """pa.table() that types EMPTY columns explicitly so an empty granule's schema matches a full one's.

    Populated columns keep pyarrow's inference. Declaring a type for them too made an unlisted column a hard failure
    at write time — ICESSN's byte_start/byte_end/n_lines fell through to the string default and every granule died
    with "ArrowTypeError: Expected bytes, got a 'int' object". Inference cannot be wrong about data that is there;
    only the empty case needs a declaration, and test_typed_table_empty_schema_matches_full guards that it agrees."""
    def _col(k, v):
        ty = col_type(k)
        if ty is not None:
            return pa.array(v, type=ty)          # a known column is declared in BOTH cases, or the two drift apart
        return pa.array(v) if len(v) else pa.array(v, type=pa.string())   # unknown: infer, and assume text if empty

    return pa.table({k: _col(k, v) for k, v in rows.items()})


def cells_filter(cells) -> set | None:
    """Normalise a build's cell set (ints/str/None) to a set of int cell ids, or None for 'index everything'."""
    if cells is None:
        return None
    return {int(c) if not isinstance(c, str) else h3.str_to_int(c) for c in cells}


def parse_granule_name(name: str) -> dict:
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"unexpected ATL03 granule name {name}")
    return {"start": m.group(1), "rgt": int(m.group(2)), "cycle": int(m.group(3)), "region": int(m.group(4)),
            "version": int(m.group(5)), "release": int(m.group(6))}


def _filters(ds: h5py.Dataset) -> str:
    dcpl = ds.id.get_create_plist()
    names = []
    for i in range(dcpl.get_nfilters()):
        fid = dcpl.get_filter(i)[0]
        names.append({1: "gzip", 2: "shuffle", 3: "fletcher32", 6: "scaleoffset"}.get(fid, f"filter{fid}"))
    return ",".join(names)


def _chunk_manifest(ds: h5py.Dataset) -> list:
    """All chunks of a dataset in index order. chunk_iter walks the B-tree once (linear); the per-index lookup is O(n^2)."""
    infos = []
    if hasattr(ds.id, "chunk_iter"):
        ds.id.chunk_iter(lambda si: infos.append(si))
        infos.sort(key=lambda si: si.chunk_offset)
    else:
        infos = [ds.id.get_chunk_info(i) for i in range(ds.id.get_num_chunks())]
    return infos


def strong_beams(sc_orient: int) -> set[str]:
    return {"gt1l", "gt2l", "gt3l"} if sc_orient == 0 else ({"gt1r", "gt2r", "gt3r"} if sc_orient == 1 else set())


def build_granule_index(granule, res: int = H3_RES, cells=None) -> pa.Table:
    """Parse one granule's structure (the only time its HDF5 b-trees are ever read) into addressing rows.

    `cells` (opt-in): keep only rows whose H3 cell is in this set — the cells a build was asked for. Without it a
    granule's WHOLE pole-to-pole track is indexed, so a regional build left 99.8% of its cells outside the region it
    claimed to cover, each holding only the granules that happened to cross that region: rows that look like coverage
    and are incomplete by construction."""
    auth.login()
    keep = cells_filter(cells)
    from .coverage import granule_name
    url = granule.data_links()[0]
    name = granule_name(granule)
    info = parse_granule_name(name)
    s3 = (granule.data_links(access="direct") or [""])[0]
    t0 = time.time()
    rows = {k: [] for k in ("granule", "url", "s3url", "revision", "sc_orient", "sdp_epoch", "beam", "strong", "cycle", "rgt",
                            "chunk_index", "ph_start", "ph_end", "h3_cell", "lat_min", "lat_max", "lon_min", "lon_max")}
    for ds in DATASETS:
        for k in ("offset", "size", "filters", "dtype", "ncols", "mask"):
            rows[f"{ds}_{k}"] = []
    # Open with 1 MB blocks: the metadata walk (superblock, groups, ~900 datasets, chunk B-trees) is many small reads,
    # and earthaccess' default 16 MB blocks pull ~10x the bytes. The bulk geolocation arrays are then read through
    # their own chunk map with coalesced range GETs (the NSIDC spike's technique), not through the block cache.
    from .access import RangeReader, access_url, cloud_hdf5_file, decode_chunk

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
            if beam not in f or f"{beam}/heights/h_ph" not in f:
                continue
            geo = f[f"{beam}/geolocation"]
            seg_lat, seg_lon = read_via_chunks(geo["reference_photon_lat"]), read_via_chunks(geo["reference_photon_lon"])
            ph_beg, ph_cnt = read_via_chunks(geo["ph_index_beg"]), read_via_chunks(geo["segment_ph_cnt"])
            dsets = {d: f[f"{beam}/heights/{d}"] for d in DATASETS}
            C = dsets["h_ph"].chunks[0]
            n_ph = dsets["h_ph"].shape[0]
            nchunks = dsets["h_ph"].id.get_num_chunks()
            for d, ds in dsets.items():
                assert ds.chunks[0] == C and ds.id.get_num_chunks() == nchunks, f"{beam}/{d}: chunking differs from h_ph"
                if ds.ndim > 1 and ds.chunks[1] != ds.shape[1]:
                    raise ValueError(f"{beam}/{d}: chunking splits the trailing dimension; refusing to index")
                bad = [f for f in _filters(ds).split(",") if f and f not in ("gzip", "shuffle")]
                if bad:
                    raise ValueError(f"{beam}/{d}: unsupported HDF5 filters {bad}; refusing to index (spec §6.3)")
            manifests = {d: _chunk_manifest(ds) for d, ds in dsets.items()}
            meta = {d: (_filters(ds), str(ds.dtype), int(ds.shape[1]) if ds.ndim > 1 else 1) for d, ds in dsets.items()}
            # chunk -> cells via segments
            ok = ph_beg > 0
            s = ph_beg[ok] - 1
            e = s + ph_cnt[ok]
            try:
                from h3ronpy.vector import coordinates_to_cells
                cells = np.asarray(coordinates_to_cells(seg_lat[ok].astype("f8"), seg_lon[ok].astype("f8"), res), dtype="u8")
            except Exception:
                cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(la), float(lo), res)) for la, lo in zip(seg_lat[ok], seg_lon[ok])], dtype="u8")
            k_lo, k_hi = s // C, np.maximum(s, e - 1) // C
            ks = np.concatenate([k_lo, k_hi[k_hi > k_lo]]).astype("i8")
            cs = np.concatenate([cells, cells[k_hi > k_lo]]).astype("u8")
            # per-chunk bounding box of its segments (fetch-selection precision; cells stay the partition key)
            seg_lat_ok, seg_lon_ok = seg_lat[ok], seg_lon[ok]
            lat_all = np.concatenate([seg_lat_ok, seg_lat_ok[k_hi > k_lo]]); lon_all = np.concatenate([seg_lon_ok, seg_lon_ok[k_hi > k_lo]])
            box = {}
            for k in np.unique(ks):
                m = ks == k
                box[int(k)] = (float(lat_all[m].min()), float(lat_all[m].max()), float(lon_all[m].min()), float(lon_all[m].max()))
            # NB: never np.stack int64 with uint64 -> float64 silently destroys the low bits of the cell id
            pairs = sorted(set(zip(ks.tolist(), cs.tolist())))
            for k, cell in pairs:
                if keep is not None and int(cell) not in keep:
                    continue                      # outside the cells this build was asked for
                assert h3.is_valid_cell(h3.int_to_str(int(cell))), cell
                rows["granule"].append(name); rows["url"].append(url); rows["s3url"].append(s3)
                rows["revision"].append(str(granule["meta"].get("revision-id", ""))); rows["sc_orient"].append(sc_orient)
                rows["sdp_epoch"].append(sdp); rows["beam"].append(beam); rows["strong"].append(beam in strong)
                rows["cycle"].append(info["cycle"]); rows["rgt"].append(info["rgt"])
                rows["chunk_index"].append(k); rows["ph_start"].append(k * C); rows["ph_end"].append(min((k + 1) * C, n_ph))
                rows["h3_cell"].append(int(cell))
                b = box[int(k)]
                rows["lat_min"].append(b[0]); rows["lat_max"].append(b[1]); rows["lon_min"].append(b[2]); rows["lon_max"].append(b[3])
                for d in DATASETS:
                    ci = manifests[d][k]
                    fl, dt, nc = meta[d]
                    rows[f"{d}_offset"].append(int(ci.byte_offset)); rows[f"{d}_size"].append(int(ci.size))
                    rows[f"{d}_filters"].append(fl); rows[f"{d}_dtype"].append(dt); rows[f"{d}_ncols"].append(nc)
                    rows[f"{d}_mask"].append(int(ci.filter_mask))
    tbl = typed_table(rows)
    tbl = tbl.replace_schema_metadata({"aicesat_index_version": INDEX_SCHEMA_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat()})
    ATL03_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, ATL03_INDEX_DIR / f"{name}.parquet")
    log.info("indexed %s: %d (chunk,cell) rows, %d beams, %.1fs (%d geolocation range GETs, %.1f MB)", name, tbl.num_rows,
             len(set(rows["beam"])), time.time() - t0, reader.stats.requests, reader.stats.bytes / 1e6)
    return tbl


def indexed_granules() -> set[str]:
    """Granules with a current-schema index file; stale-schema files are deleted so they are rebuilt."""
    out = set()
    for p in (ATL03_INDEX_DIR.glob("*.parquet") if ATL03_INDEX_DIR.exists() else []):
        meta = pq.read_schema(p).metadata or {}
        if meta.get(b"aicesat_index_version", b"").decode() == INDEX_SCHEMA_VERSION:
            out.add(p.stem)
        else:
            log.warning("index %s has an old schema; rebuilding", p.name)
            p.unlink()
    return out


INDEX_TIMEOUT_S = 240  # a stalled remote open must not wedge a job: time out and retry once
INDEX_WORKERS = 8      # h5py holds a global lock: processes, not threads (spike measured 0x from threads, 5-8x from processes)


def ensure_index(granules, workers: int = INDEX_WORKERS, cells=None) -> dict:
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutTimeout

    have = indexed_granules()
    todo = [g for g in granules if g["meta"]["native-id"] not in have]
    built, t0 = [], time.time()
    for attempt in (1, 2):
        if not todo:
            break
        failed = []
        ex = ProcessPoolExecutor(max_workers=min(workers, len(todo)))
        futs = {ex.submit(build_granule_index, g, H3_RES, cells): g for g in todo}
        # MONOTONIC, not wall clock. A laptop that sleeps mid-build advances time.time() by the whole nap, so every
        # remaining future would blow its deadline the moment the machine woke and a healthy build would report
        # itself failed. On macOS time.monotonic() is mach_absolute_time and stops during sleep; on Linux
        # CLOCK_MONOTONIC likewise excludes suspend. (This is what a 95-minute "hang" turned out to be: the lid.)
        deadline = time.monotonic() + INDEX_TIMEOUT_S * (len(todo) / max(1, min(workers, len(todo))) + 1)
        for f, g in futs.items():
            name = g["meta"]["native-id"]
            try:
                f.result(timeout=max(1.0, deadline - time.monotonic()))
                built.append(name)
            except FutTimeout:
                log.warning("index build of %s timed out (attempt %d)", name, attempt)
                failed.append(g)
            except Exception as e:
                # EVERY worker failure is retryable, not just a timeout. A transient CDN error (503) killed a whole
                # 120-granule build at granule 101 because only FutTimeout was caught — and it surfaced as
                # "can't pickle multidict.CIMultiDictProxy", not as the 503 it was: aiohttp's ClientResponseError
                # carries response headers that ProcessPoolExecutor cannot pickle back to the parent, so the real
                # cause is destroyed in transit. Never let one flaky granule discard the other 119.
                log.warning("index build of %s failed (attempt %d): %s: %s", name, attempt, type(e).__name__, e)
                failed.append(g)
        ex.shutdown(wait=False, cancel_futures=True)
        todo = failed
    out = {"built": built, "skipped": len(granules) - len(built), "seconds": round(time.time() - t0, 1),
           "failed": [g["meta"]["native-id"] for g in todo]}
    if todo:
        # Not raised: the granules that DID index are on disk and the caller still needs to stamp the coverage
        # manifest for them. The caller reports the failures and exits non-zero; a re-run picks up only these.
        log.error("index build failed for %d granule(s) after 2 attempts: %s", len(todo), ", ".join(out["failed"]))
    return out


COVERAGE_RES = 9   # the resolution a build CLAIMS at. Addressing stays coarse (H3_RES / each collection's res):
# that is about file layout and query pushdown. The claim is a different job — it says which ground a build actually
# searched — and a coarse claim is a lie at the edges, because a coarse cell juts up to ~10 km past the drawn shape
# and nothing searched out there. Claiming at res 9 (~200 m edge) makes the assertion match the selection, and cuts
# the CMR search area ~10x, which is the dominant build cost. Stored COMPACTED (h3.compact_cells), so a solid region
# collapses to a handful of parents: a 3,930-cell corridor stores as 1,188.


def write_build_manifest(d, bbox, res: int | None = None, window=None, n_granules: int | None = None,
                         cells=None, coverage_res: int = COVERAGE_RES) -> dict:
    """Record WHICH GROUND an index was built over, as a compacted H3 cell set at `coverage_res`.

    `cells` are the fine (claim-resolution) cells the build searched and indexed. Sets from repeated builds are
    UNIONED: indexing a neighbouring region adds ground, never retracts it. `bbox` is provenance only — nothing
    reads it for coverage.
    """
    import json

    d = pathlib.Path(d)
    d.mkdir(parents=True, exist_ok=True)
    mf = d / "_build.json"
    prev, have = {}, set()
    if mf.exists():
        try:
            prev = json.loads(mf.read_text())
            have = {h3.int_to_str(int(c)) for c in (prev.get("cells") or [])}
        except Exception:
            log.warning("unreadable %s; replacing", mf)
    # Union WITHOUT uncompacting: a coarse claim would explode into millions of ids, and covers_cells matches by
    # ancestry, so a set holding mixed resolutions is answered correctly as it stands.
    new = {h3.int_to_str(int(c)) if not isinstance(c, str) else c for c in (cells or [])}
    by_res: dict[int, set] = {}
    for c in have | new:
        by_res.setdefault(h3.get_resolution(c), set()).add(c)
    packed = sorted(c for r, cs in by_res.items() for c in h3.compact_cells(sorted(cs)))
    bounds = None
    if packed:
        bs = [h3.cell_to_boundary(c) for c in packed]
        las = [la for b in bs for la, _ in b]; los = [lo for b in bs for _, lo in b]
        bounds = [min(los), min(las), max(los), max(las)]
    doc = {"cells": sorted(h3.str_to_int(c) for c in packed), "coverage_res": coverage_res, "bounds": bounds,
           "res": res if res is not None else prev.get("res"),
           "window": list(window) if window else prev.get("window"),
           "target": n_granules if n_granules is not None else prev.get("target"),
           "requested": (list(prev.get("requested") or []) + [list(bbox)]) if bbox is not None else prev.get("requested")}
    mf.write_text(json.dumps(doc))
    return doc


def manifest_cells(d) -> set:
    """The COMPACTED claim set (as int cell ids), or an empty set when the index has no manifest."""
    import json

    mf = pathlib.Path(d) / "_build.json"
    if not mf.exists():
        return set()
    try:
        return {int(c) for c in (json.loads(mf.read_text()).get("cells") or [])}
    except Exception:
        return set()


def covers_cells(d, cells) -> bool:
    """True if every cell in `cells` is inside the claim set — tested by walking UP to each ancestor.

    The set is stored compacted, so a claimed res-9 cell may be represented by a res-6 parent. Uncompacting to
    compare would materialise millions of ids for a large region; ancestor lookup is a handful of hashes per query
    cell and never allocates."""
    packed = manifest_cells(d)
    if not packed:
        return False
    packed_str = {h3.int_to_str(c) for c in packed}
    for c in cells:
        s = h3.int_to_str(int(c)) if not isinstance(c, str) else c
        r = h3.get_resolution(s)
        if not any(h3.cell_to_parent(s, rr) in packed_str for rr in range(r, -1, -1)):
            return False
    return True


def chunk_refs(cells: list[int], granules: list[str] | None = None, strong_only: bool = True, bbox=None, per_cell: bool = False) -> pa.Table:
    """Distinct chunk references for a set of cells (one row per (granule, beam, chunk)); the addressing role of §5.1.
    bbox (W,S,E,N) additionally prunes chunks whose own segment bounding box misses the query — cells are coarse
    (a res-6 cell reaches ~3.7 km outside the box), per-chunk boxes are not."""
    if not ATL03_INDEX_DIR.exists() or not any(ATL03_INDEX_DIR.glob("*.parquet")):
        return pa.table({})
    con = duckdb.connect()
    cond = [f"h3_cell IN ({','.join(str(int(c)) for c in cells)})"]
    if granules:
        cond.append("granule IN (" + ",".join("'" + g + "'" for g in granules) + ")")
    if strong_only:
        cond.append("strong")
    if bbox is not None:
        w, s_, e, n = bbox
        cond.append(f"lat_max >= {s_} AND lat_min <= {n} AND lon_max >= {w} AND lon_min <= {e}")
    cols = ", ".join(f"{d}_{k}" for d in DATASETS for k in ("offset", "size", "filters", "dtype", "ncols", "mask"))
    cell_col = "h3_cell, " if per_cell else ""
    q = f"""SELECT DISTINCT granule, url, s3url, beam, sdp_epoch, cycle, chunk_index, {cell_col}ph_start, ph_end, {cols}
            FROM read_parquet('{ATL03_INDEX_DIR}/*.parquet') WHERE {' AND '.join(cond)} ORDER BY granule, beam, chunk_index"""
    return con.execute(q).to_arrow_table()
