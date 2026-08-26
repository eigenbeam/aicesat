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

import threading
import time

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


SETTINGS_PATH = INDEX_DIR / "settings.json"
EVICTION_LOG = INDEX_DIR / "evictions.jsonl"
DEFAULT_MAX_BYTES = 5 << 30  # 5 GB


_META_LOCK = threading.RLock()  # DuckDB takes an exclusive file lock; serialise all opens in this process


class _Meta:
    """Context manager: hold the process lock for the whole open->query->close, with a short retry if another
    process (a stray CLI, a second server) still holds the OS lock."""

    def __enter__(self) -> duckdb.DuckDBPyConnection:
        _META_LOCK.acquire()
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        for attempt in range(20):
            try:
                self.con = _open_meta()
                return self.con
            except duckdb.IOException:
                if attempt == 19:
                    _META_LOCK.release(); raise
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            self.con.close()
        finally:
            _META_LOCK.release()


def meta_db() -> "_Meta":
    return _Meta()


def _open_meta() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(META_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS coverage_cells (
        mission VARCHAR, granule VARCHAR, beam VARCHAR, chunk_index INTEGER, h3_cell UBIGINT, ingested_at TIMESTAMP,
        PRIMARY KEY (mission, granule, beam, chunk_index, h3_cell))""")
    # one-time migration from the list-valued table
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'coverage'").fetchone()[0]:
        con.execute("""INSERT OR IGNORE INTO coverage_cells
                       SELECT mission, granule, beam, chunk_index, unnest(h3_cells), ingested_at FROM coverage""")
        con.execute("DROP TABLE coverage")
    return con


def meta_conn() -> duckdb.DuckDBPyConnection:  # legacy: unlocked open (tests/CLI); server paths use meta_db()
    return _open_meta()


def mark_ingested(mission: str, granule: str, beam: str, chunk_cells: dict[int, list[int]]) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with meta_db() as con:
        con.executemany("INSERT OR REPLACE INTO coverage_cells VALUES (?, ?, ?, ?, ?, ?)",
                        [(mission, granule, beam, int(k), int(c), now) for k, cells in chunk_cells.items() for c in cells])


def ingested_chunk_cells(mission: str, granules: list[str]) -> set[tuple[str, str, int, int]]:
    """(granule, beam, chunk_index, h3_cell) tuples already materialized."""
    if not META_DB.exists() or not granules:
        return set()
    with meta_db() as con:
        rows = con.execute("SELECT granule, beam, chunk_index, h3_cell FROM coverage_cells WHERE mission = ? AND granule IN (" +
                           ",".join("?" * len(granules)) + ")", [mission, *granules]).fetchall()
    return {(g, b, int(k), int(c)) for g, b, k, c in rows}


def ingested_chunks(mission: str, granules: list[str]) -> set[tuple[str, str, int]]:
    """Chunks with at least one materialized cell (kept for callers that only need chunk identity)."""
    return {(g, b, k) for g, b, k, _ in ingested_chunk_cells(mission, granules)}


# ----------------------------------------------------------------------------- per-cell stats, settings, eviction

def cell_stats(mission: str = "ICESAT2") -> dict[int, dict]:
    """Per materialized cell: granules, beams, chunks, rows, bytes, first/last ingested. Files are the source of truth
    for bytes/rows (Parquet footers), the coverage table for provenance and age."""
    out: dict[int, dict] = {}
    if not LAKE_DIR.exists():
        return out
    for cdir in LAKE_DIR.glob(f"mission={mission}/h3_cell=*"):
        cell = int(cdir.name.split("=")[1])
        files = list(cdir.glob("*.parquet"))
        if not files:
            continue
        rows = 0
        for f in files:
            try:
                rows += pq.read_metadata(f).num_rows
            except Exception:
                pass
        out[cell] = {"cell": cell, "files": len(files), "rows": rows, "bytes": sum(f.stat().st_size for f in files),
                     "granules": sorted({f.name.split("__")[0] for f in files}), "beams": sorted({f.stem.split("__")[1] for f in files}),
                     "chunks": 0, "first_ingested": None, "last_ingested": None}
    if META_DB.exists() and out:
        with meta_db() as con:
            grp = con.execute("SELECT h3_cell, count(*), min(ingested_at), max(ingested_at) FROM coverage_cells "
                              "WHERE mission = ? GROUP BY h3_cell", [mission]).fetchall()
        for cell, n, first, last in grp:
            if int(cell) in out:
                out[int(cell)].update(chunks=int(n), first_ingested=first.isoformat() if first else None,
                                      last_ingested=last.isoformat() if last else None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for st in out.values():
        st["age_s"] = (now - datetime.fromisoformat(st["last_ingested"])).total_seconds() if st["last_ingested"] else None
    return out


def get_settings() -> dict:
    import json
    d = {"max_bytes": DEFAULT_MAX_BYTES}
    if SETTINGS_PATH.exists():
        try:
            d.update(json.loads(SETTINGS_PATH.read_text()))
        except Exception:
            pass
    return d


def set_settings(**kw) -> dict:
    import json
    d = get_settings(); d.update({k: v for k, v in kw.items() if v is not None})
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(d))
    return d


def evict_cells(cells, mission: str = "ICESAT2", reason: str = "manual") -> list[dict]:
    """Delete the cells' Parquet files and coverage rows (the index is untouched); returns what was evicted."""
    import json
    import shutil

    stats = cell_stats(mission)
    evicted = []
    with meta_db() as con:
        for c in cells:
            c = int(c)
            st = stats.get(c)
            if st is None:
                continue
            shutil.rmtree(cell_dir(mission, c), ignore_errors=True)
            con.execute("DELETE FROM coverage_cells WHERE mission = ? AND h3_cell = ?", [mission, c])
            evicted.append({"cell": c, "bytes": st["bytes"], "rows": st["rows"], "last_ingested": st["last_ingested"], "reason": reason,
                            "evicted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if evicted:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        with EVICTION_LOG.open("a") as f:
            for rec in evicted:
                f.write(json.dumps(rec) + "\n")
    return evicted


def enforce_limit(protect=(), mission: str = "ICESAT2") -> list[dict]:
    """Evict least-recently-ingested cells until the lake is under max_bytes; never touches `protect` cells."""
    limit = int(get_settings()["max_bytes"])
    stats = cell_stats(mission)
    total = sum(s["bytes"] for s in stats.values())
    if total <= limit:
        return []
    protect = {int(c) for c in protect}
    victims = sorted((s for c, s in stats.items() if c not in protect), key=lambda s: (s["last_ingested"] or "", s["cell"]))
    chosen = []
    for s in victims:
        if total <= limit:
            break
        chosen.append(s["cell"]); total -= s["bytes"]
    return evict_cells(chosen, mission, reason=f"limit {limit} bytes") if chosen else []


def recent_evictions(n: int = 50) -> list[dict]:
    import json
    if not EVICTION_LOG.exists():
        return []
    lines = EVICTION_LOG.read_text().splitlines()[-n:]
    return [json.loads(l) for l in lines if l.strip()]


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
    stats = cell_stats(mission)
    total = sum(s["bytes"] for s in stats.values())
    settings = get_settings()
    return {"files": sum(s["files"] for s in stats.values()), "rows": sum(s["rows"] for s in stats.values()), "cells": len(stats),
            "bytes": total, "max_bytes": int(settings["max_bytes"]), "usage": (total / settings["max_bytes"]) if settings["max_bytes"] else None,
            "granules": len({g for s in stats.values() for g in s["granules"]}),
            "oldest_ingested": min((s["last_ingested"] for s in stats.values() if s["last_ingested"]), default=None),
            "newest_ingested": max((s["last_ingested"] for s in stats.values() if s["last_ingested"]), default=None),
            "evictions_recent": recent_evictions(10)}
