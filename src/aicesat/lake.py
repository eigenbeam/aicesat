"""Persistent Parquet lake (spec §7) + coverage metadata (§5.7) + DuckDB queries (§8).

Layout: data/lake/mission=<M>/h3_cell=<int>/<granule>__<beam>.parquet — one file per (cell, granule, beam).
Idempotency is by construction: re-materializing the same (granule, beam) rewrites identical files, and every row
also carries a deterministic surrogate row_id (§7.3) for cross-check. Each photon is written to the cell of its OWN
lat/lon, so a chunk straddling cells never double-counts. Co-registered coordinates are materialized at ingest (§7.4).
"""
from __future__ import annotations

import logging
import zlib
from datetime import datetime, timezone

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import cache
from .index import INDEX_DIR

log = logging.getLogger(__name__)

LAKE_DIR = cache.DATA_DIR / "lake"
META_DB = INDEX_DIR / "meta.duckdb"
COMMON_EPOCH = 2005.0  # materialized coreg epoch; re-epoching = re-run materialize (§7.4)


def row_ids(granule: str, beam: str, photon_index: np.ndarray) -> np.ndarray:
    """Deterministic surrogate key: crc32(granule/beam) in the high 32 bits, photon index in the low 32 bits."""
    hi = np.uint64(zlib.crc32(f"{granule}/{beam}".encode()) & 0xFFFFFFFF) << np.uint64(32)
    return hi | photon_index.astype("u8")


def cell_dir(mission: str, cell: int):
    return LAKE_DIR / f"mission={mission}" / f"h3_cell={int(cell)}"


def write_photons(mission: str, granule: str, beam: str, ph: dict[str, np.ndarray]) -> list[int]:
    """ph: lon, lat, h, conf, t (datetime64[ms]), photon_index, chunk_index, h3_cell, coreg_lon, coreg_lat.
    Writes one file per cell; returns the cells written."""
    cells = np.unique(ph["h3_cell"])
    for cell in cells:
        m = ph["h3_cell"] == cell
        tbl = pa.table({
            "row_id": pa.array(row_ids(granule, beam, ph["photon_index"][m]), type=pa.uint64()),
            "native_lon": ph["lon"][m], "native_lat": ph["lat"][m], "native_height": ph["h"][m].astype("f8"),
            "height_ref": pa.array(["WGS84 ellipsoid"] * int(m.sum())).dictionary_encode(),
            "native_frame": pa.array(["ITRF2014"] * int(m.sum())).dictionary_encode(),
            "t": pa.array(ph["t"][m].astype("datetime64[ms]")),
            "coreg_lon": ph["coreg_lon"][m], "coreg_lat": ph["coreg_lat"][m],
            "coreg_epoch": pa.array(np.full(int(m.sum()), COMMON_EPOCH)),
            "signal_conf_landice": ph["conf"][m].astype("i1"),
            "source_granule": pa.array([granule] * int(m.sum())).dictionary_encode(),
            "beam": pa.array([beam] * int(m.sum())).dictionary_encode(),
            "photon_index": ph["photon_index"][m].astype("i8"),
            "source_chunk_index": ph["chunk_index"][m].astype("i4"),
        })
        d = cell_dir(mission, int(cell)); d.mkdir(parents=True, exist_ok=True)
        # rows are in photon (along-track) order, so ROW_GROUP_ROWS-row groups carry tight lat/lon min/max statistics
        # and DuckDB skips the groups a bbox predicate cannot touch (spec §5.6 sort order, along-track for v1)
        pq.write_table(tbl, d / f"{granule}__{beam}.parquet", compression="zstd", row_group_size=ROW_GROUP_ROWS)
    return [int(c) for c in cells]


ROW_GROUP_ROWS = 65_536


def relayout(mission: str = "ICESAT2") -> dict:
    """Rewrite existing lake files with ROW_GROUP_ROWS-row groups (no network). Idempotent."""
    import time

    t0, n_files, n_rows = time.time(), 0, 0
    for f in LAKE_DIR.glob(f"mission={mission}/h3_cell=*/*.parquet"):
        md = pq.read_metadata(f)
        if md.num_row_groups >= max(1, md.num_rows // ROW_GROUP_ROWS):
            continue
        tbl = pq.read_table(f)
        tmp = f.with_suffix(".tmp")
        pq.write_table(tbl, tmp, compression="zstd", row_group_size=ROW_GROUP_ROWS)
        tmp.replace(f)
        n_files += 1; n_rows += tbl.num_rows
    return {"files_rewritten": n_files, "rows": n_rows, "seconds": round(time.time() - t0, 1)}


def meta_conn() -> duckdb.DuckDBPyConnection:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(META_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS coverage (
        mission VARCHAR, granule VARCHAR, beam VARCHAR, chunk_index INTEGER, h3_cells UBIGINT[], ingested_at TIMESTAMP,
        PRIMARY KEY (mission, granule, beam, chunk_index))""")
    return con


def mark_ingested(mission: str, granule: str, beam: str, chunk_cells: dict[int, list[int]]) -> None:
    con = meta_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    con.executemany("INSERT OR REPLACE INTO coverage VALUES (?, ?, ?, ?, ?, ?)",
                    [(mission, granule, beam, int(k), [int(c) for c in cells], now) for k, cells in chunk_cells.items()])
    con.close()


def ingested_chunks(mission: str, granules: list[str]) -> set[tuple[str, str, int]]:
    if not META_DB.exists() or not granules:
        return set()
    con = meta_conn()
    rows = con.execute("SELECT granule, beam, chunk_index FROM coverage WHERE mission = ? AND granule IN (" +
                       ",".join("?" * len(granules)) + ")", [mission, *granules]).fetchall()
    con.close()
    return {(g, b, int(k)) for g, b, k in rows}


def query_photons(bbox, cells: list[int], min_conf: int, granules: list[str] | None = None, mission: str = "ICESAT2") -> dict:
    """The query path (§8): DuckDB over the hive-partitioned lake with cell + bbox predicate pushdown."""
    w, s, e, n = bbox
    glob = f"{LAKE_DIR}/mission={mission}/h3_cell=*/*.parquet"
    if not any(LAKE_DIR.glob(f"mission={mission}/h3_cell=*/*.parquet")):
        raise RuntimeError("lake is empty for " + mission)
    con = duckdb.connect()
    cond = [f"h3_cell IN ({','.join(str(int(c)) for c in cells)})",
            f"native_lat BETWEEN {s} AND {n}", f"native_lon BETWEEN {w} AND {e}", f"signal_conf_landice >= {min_conf}"]
    if granules:
        cond.append("source_granule IN (" + ",".join("'" + g + "'" for g in granules) + ")")
    src = f"read_parquet('{glob}', hive_partitioning = true)"
    where = " AND ".join(cond)
    # Integer codes are computed in SQL and the result comes back as numpy directly: string columns crossing into
    # Python cost ~1.5 s per 6M rows (measured); this path is ~0.8 s for the same rows.
    glist = [r[0] for r in con.execute(f"SELECT DISTINCT source_granule FROM {src} WHERE {where} ORDER BY 1").fetchall()]
    q = f"""SELECT native_lon, native_lat, native_height, signal_conf_landice, t, photon_index,
                   dense_rank() OVER (ORDER BY source_granule) - 1 AS granule_idx, CAST(beam[3] AS INTEGER) - 1 AS beam_idx,
                   coreg_lon, coreg_lat
            FROM {src} WHERE {where}"""
    r = con.execute(q).fetchnumpy()
    return {"lon": np.asarray(r["native_lon"]), "lat": np.asarray(r["native_lat"]), "h": np.asarray(r["native_height"]),
            "conf": np.asarray(r["signal_conf_landice"]), "t": np.asarray(r["t"]).astype("datetime64[ms]"),
            "ph_index": np.asarray(r["photon_index"]),
            "granule_idx": np.asarray(r["granule_idx"]).astype("i2"), "beam_idx": np.asarray(r["beam_idx"]).astype("i1"),
            "coreg_lon": np.asarray(r["coreg_lon"]), "coreg_lat": np.asarray(r["coreg_lat"]),
            "_granules": glist}


def lake_summary(mission: str = "ICESAT2") -> dict:
    files = list(LAKE_DIR.glob(f"mission={mission}/h3_cell=*/*.parquet"))
    if not files:
        return {"files": 0, "rows": 0, "cells": 0, "bytes": 0}
    con = duckdb.connect()
    n = con.execute(f"SELECT count(*) FROM read_parquet('{LAKE_DIR}/mission={mission}/h3_cell=*/*.parquet')").fetchone()[0]
    return {"files": len(files), "rows": int(n), "cells": len({p.parent.name for p in files}), "bytes": sum(p.stat().st_size for p in files)}
