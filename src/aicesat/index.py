"""Addressing index (spec §5): (mission, h3_cell, time_bucket) -> source chunk references, built ONCE per granule.

For ATL03 every heights/* dataset is chunked identically (100 000 photons per chunk, verified on v007), so one index
row per (granule, beam, chunk, h3_cell) carries the byte range of all five datasets. Cells are assigned from the
20 m segment geolocation: a chunk's photon range maps to the segments it covers, and their lat/lon to H3 cells.
The same chunk appears under every cell it touches (§5.3) — the residual lat/lon predicate is applied at query time.
"""
from __future__ import annotations

import logging
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
INDEX_SCHEMA_VERSION = "3"  # bump when index columns change; older files are rebuilt, never read
DATASETS = ("lat_ph", "lon_ph", "h_ph", "signal_conf_ph", "delta_time")
BEAMS = ("gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r")
INDEX_DIR = cache.DATA_DIR / "index"
ATL03_INDEX_DIR = INDEX_DIR / "atl03"
_NAME_RE = re.compile(r"ATL03_(\d{14})_(\d{4})(\d{2})(\d{2})_(\d{3})_(\d{2})\.h5")


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


def build_granule_index(granule, res: int = H3_RES) -> pa.Table:
    """Parse one granule's structure (the only time its HDF5 b-trees are ever read) into addressing rows."""
    import earthaccess

    auth.login()
    name = granule["meta"]["native-id"]
    info = parse_granule_name(name)
    url = granule.data_links()[0]
    s3 = (granule.data_links(access="direct") or [""])[0]
    t0 = time.time()
    rows = {k: [] for k in ("granule", "url", "s3url", "revision", "sc_orient", "sdp_epoch", "beam", "strong", "cycle", "rgt",
                            "chunk_index", "ph_start", "ph_end", "h3_cell")}
    for ds in DATASETS:
        for k in ("offset", "size", "filters", "dtype", "ncols", "mask"):
            rows[f"{ds}_{k}"] = []
    with h5py.File(earthaccess.open([granule], show_progress=False)[0], "r") as f:
        sc_orient = int(f["orbit_info/sc_orient"][0])
        sdp = float(f["ancillary_data/atlas_sdp_gps_epoch"][0])
        strong = strong_beams(sc_orient)
        for beam in BEAMS:
            if beam not in f or f"{beam}/heights/h_ph" not in f:
                continue
            geo = f[f"{beam}/geolocation"]
            seg_lat, seg_lon = geo["reference_photon_lat"][:], geo["reference_photon_lon"][:]
            ph_beg, ph_cnt = geo["ph_index_beg"][:], geo["segment_ph_cnt"][:]
            dsets = {d: f[f"{beam}/heights/{d}"] for d in DATASETS}
            C = dsets["h_ph"].chunks[0]
            n_ph = dsets["h_ph"].shape[0]
            nchunks = dsets["h_ph"].id.get_num_chunks()
            for d, ds in dsets.items():
                assert ds.chunks[0] == C and ds.id.get_num_chunks() == nchunks, f"{beam}/{d}: chunking differs from h_ph"
            manifests = {d: _chunk_manifest(ds) for d, ds in dsets.items()}
            meta = {d: (_filters(ds), str(ds.dtype), int(ds.shape[1]) if ds.ndim > 1 else 1) for d, ds in dsets.items()}
            # chunk -> cells via segments
            ok = ph_beg > 0
            s = ph_beg[ok] - 1
            e = s + ph_cnt[ok]
            cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(la), float(lo), res)) for la, lo in zip(seg_lat[ok], seg_lon[ok])], dtype="u8")
            k_lo, k_hi = s // C, np.maximum(s, e - 1) // C
            ks = np.concatenate([k_lo, k_hi[k_hi > k_lo]]).astype("i8")
            cs = np.concatenate([cells, cells[k_hi > k_lo]]).astype("u8")
            # NB: never np.stack int64 with uint64 -> float64 silently destroys the low bits of the cell id
            pairs = sorted(set(zip(ks.tolist(), cs.tolist())))
            for k, cell in pairs:
                assert h3.is_valid_cell(h3.int_to_str(int(cell))), cell
                rows["granule"].append(name); rows["url"].append(url); rows["s3url"].append(s3)
                rows["revision"].append(str(granule["meta"].get("revision-id", ""))); rows["sc_orient"].append(sc_orient)
                rows["sdp_epoch"].append(sdp); rows["beam"].append(beam); rows["strong"].append(beam in strong)
                rows["cycle"].append(info["cycle"]); rows["rgt"].append(info["rgt"])
                rows["chunk_index"].append(k); rows["ph_start"].append(k * C); rows["ph_end"].append(min((k + 1) * C, n_ph))
                rows["h3_cell"].append(int(cell))
                for d in DATASETS:
                    ci = manifests[d][k]
                    fl, dt, nc = meta[d]
                    rows[f"{d}_offset"].append(int(ci.byte_offset)); rows[f"{d}_size"].append(int(ci.size))
                    rows[f"{d}_filters"].append(fl); rows[f"{d}_dtype"].append(dt); rows[f"{d}_ncols"].append(nc)
                    rows[f"{d}_mask"].append(int(ci.filter_mask))
    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in rows.items()})
    tbl = tbl.replace_schema_metadata({"aicesat_index_version": INDEX_SCHEMA_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat()})
    ATL03_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, ATL03_INDEX_DIR / f"{name}.parquet")
    log.info("indexed %s: %d (chunk,cell) rows, %d beams, %.1fs", name, tbl.num_rows, len(set(rows["beam"])), time.time() - t0)
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


def ensure_index(granules) -> dict:
    have = indexed_granules()
    built, t0 = [], time.time()
    for g in granules:
        name = g["meta"]["native-id"]
        if name not in have:
            build_granule_index(g)
            built.append(name)
    return {"built": built, "skipped": len(granules) - len(built), "seconds": round(time.time() - t0, 1)}


def chunk_refs(cells: list[int], granules: list[str] | None = None, strong_only: bool = True) -> pa.Table:
    """Distinct chunk references for a set of cells (one row per (granule, beam, chunk)); the addressing role of §5.1."""
    if not ATL03_INDEX_DIR.exists() or not any(ATL03_INDEX_DIR.glob("*.parquet")):
        return pa.table({})
    con = duckdb.connect()
    cond = [f"h3_cell IN ({','.join(str(int(c)) for c in cells)})"]
    if granules:
        cond.append("granule IN (" + ",".join("'" + g + "'" for g in granules) + ")")
    if strong_only:
        cond.append("strong")
    cols = ", ".join(f"{d}_{k}" for d in DATASETS for k in ("offset", "size", "filters", "dtype", "ncols", "mask"))
    q = f"""SELECT DISTINCT granule, url, beam, sdp_epoch, cycle, chunk_index, ph_start, ph_end, {cols}
            FROM read_parquet('{ATL03_INDEX_DIR}/*.parquet') WHERE {' AND '.join(cond)} ORDER BY granule, beam, chunk_index"""
    return con.execute(q).fetch_arrow_table()
