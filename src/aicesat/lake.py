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
        pq.write_table(tbl, d / f"{granule}__{beam}.parquet", compression="zstd")
    return [int(c) for c in cells]


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
    q = f"""SELECT native_lon, native_lat, native_height, signal_conf_landice, t, photon_index, source_granule, beam, coreg_lon, coreg_lat
            FROM read_parquet('{glob}', hive_partitioning = true) WHERE {' AND '.join(cond)}"""
    tbl = con.execute(q).fetch_arrow_table()
    gran = tbl["source_granule"].to_pylist(); beams = tbl["beam"].to_pylist()
    glist = sorted(set(gran))
    return {"lon": tbl["native_lon"].to_numpy(), "lat": tbl["native_lat"].to_numpy(), "h": tbl["native_height"].to_numpy(),
            "conf": tbl["signal_conf_landice"].to_numpy(), "t": tbl["t"].to_numpy().astype("datetime64[ms]"),
            "ph_index": tbl["photon_index"].to_numpy(),
            "granule_idx": np.array([glist.index(g) for g in gran], dtype="i2"),
            "beam_idx": np.array([int(b[2]) - 1 for b in beams], dtype="i1"),
            "coreg_lon": tbl["coreg_lon"].to_numpy(), "coreg_lat": tbl["coreg_lat"].to_numpy(),
            "_granules": glist}


def lake_summary(mission: str = "ICESAT2") -> dict:
    files = list(LAKE_DIR.glob(f"mission={mission}/h3_cell=*/*.parquet"))
    if not files:
        return {"files": 0, "rows": 0, "cells": 0, "bytes": 0}
    con = duckdb.connect()
    n = con.execute(f"SELECT count(*) FROM read_parquet('{LAKE_DIR}/mission={mission}/h3_cell=*/*.parquet')").fetchone()[0]
    return {"files": len(files), "rows": int(n), "cells": len({p.parent.name for p in files}), "bytes": sum(p.stat().st_size for p in files)}
