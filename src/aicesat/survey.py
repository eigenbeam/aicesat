"""Level 1 of the demo ladder: which missions passed over each H3 hex, how often, and in which years.

Read from each collection's coverage manifest (DISTINCT h3_cell, granule, ym; coverage._ensure_manifest keeps it
fresh) -- no NASA calls, no lake reads. Index rows are keyed at res 5, so a coarser hex is the roll-up of its res-5
children, and a granule crossing two children counts once. A PASS is a distinct granule; a DAY is a distinct acquisition
date. Days are the count to show: an ILATM2 granule is a ~4-minute slice of a flight, so one flight line over a hex can
span two granules (lower trunk 8506f213fffffff: 117 IceBridge granules on 32 flight days), while for GLAH06 and ATL06 a
granule over a hex is one pass and days == passes.

`claimed` answers "would a build over this hex be accepted for this mission": the same compacted-claim containment
test the build gate uses (index.cells_within), so the globe never offers a hex the build would refuse."""
from __future__ import annotations

import math
import re
import threading

import duckdb
import h3

from . import coverage, index

MISSIONS = ("GLAS", "ICESSN", "ATL06")
LABELS = {"GLAS": "ICESat (GLAS, 2003-09)", "ICESSN": "Operation IceBridge ATM (2009-19)",
          "ATL06": "ICESat-2 land ice ATL06 (2018-)"}
INDEX_RES = 5
MANIFEST_MAX_STALE_S = 300   # the globe may show coverage up to 5 min behind a running build (see _ensure_manifest)
_HEX_MEMO: dict = {}         # (res, bbox, stamps) -> coverage_hexes result
_HEX_MEMO_MAX = 8
_LOCK = threading.Lock()     # one manifest rebuild / GLAS date scan at a time: the page asks for res 3, 4, 5 together
_GLAS_DAYS: dict = {"stamp": None, "days": {}}


def _rows(key: str) -> list[tuple]:
    """(h3_cell int, granule, 'YYYY-MM') for every manifest row of a collection; [] when it has no index."""
    d, _res, ym = coverage._index_for(key)
    if d is None or not d.exists():
        return []
    m = coverage._ensure_manifest(d, ym)
    if m is None:
        return []
    con = duckdb.connect()
    try:
        return con.execute("SELECT h3_cell, granule, ym FROM read_parquet(?)", [str(m)]).fetchall()
    finally:
        con.close()


def _manifest(key: str):
    """A collection's coverage manifest (parquet path) for display, or None when it has no index."""
    d, _res, ym = coverage._index_for(key)
    if d is None or not d.exists():
        return None
    with _LOCK:
        return coverage._ensure_manifest(d, ym, max_stale_s=MANIFEST_MAX_STALE_S)


def _glas_days() -> dict:
    """GLAH06 granule -> 'YYYYMMDD' (the names carry no date), memoised until the GLAS index changes: the scan reads
    every GLAS index file (2.1 s for Greenland) and the answer moves only when a build lands a granule."""
    d = coverage._index_for("GLAS")[0]
    stamp = coverage._dir_mtime_ns(d) if d is not None else None
    with _LOCK:
        if _GLAS_DAYS["stamp"] != stamp or stamp is None:
            srcs = coverage._source_parquets(d) if d is not None and d.exists() else []
            days = {}
            if srcs:
                con = duckdb.connect()
                try:
                    days = dict(con.execute("SELECT DISTINCT granule, gdate FROM read_parquet(?)",
                                            [[str(x) for x in srcs]]).fetchall())
                finally:
                    con.close()
            _GLAS_DAYS.update(stamp=stamp, days=days)
        return _GLAS_DAYS["days"]


def _parent_sql(res: int) -> str:
    """SQL for the res-`res` parent of `h3_cell` (UBIGINT), by H3's bit layout: the 4-bit resolution field at bit 52 set
    to `res`, and every digit finer than `res` set to 7 (unused). A cell already at or above `res` is kept."""
    clear = ~(0xF << 52) & 0xFFFFFFFFFFFFFFFF
    low = (1 << (3 * (15 - res))) - 1
    return (f"CASE WHEN ((h3_cell >> 52) & 15) > {res} "
            f"THEN (h3_cell & {clear}::UBIGINT) | {res << 52}::UBIGINT | {low}::UBIGINT ELSE h3_cell END")


def _aggregate(key: str, res: int) -> dict:
    """{res-`res` cell int: (passes, days, year_min, year_max, n_years)} for one collection, rolled up in DuckDB. A
    granule crossing two children of a hex counts once (DISTINCT over the parent), as the Python loop it replaces did."""
    m = _manifest(key)
    if m is None:
        return {}
    con = duckdb.connect()
    try:
        if key == "GLAS":
            import pyarrow as pa
            days = _glas_days()
            con.register("days", pa.table({"granule": list(days), "day": list(days.values())}))
            src, day = "read_parquet(?) LEFT JOIN days USING (granule)", "day"
        else:
            src, day = "read_parquet(?)", r"NULLIF(regexp_extract(granule, '_(\d{8})', 1), '')"
        rows = con.execute(
            f"SELECT p, count(DISTINCT granule), count(DISTINCT d), min(y), max(y), count(DISTINCT y) FROM "
            f"(SELECT {_parent_sql(res)} AS p, granule, {day} AS d, TRY_CAST(substr(ym, 1, 4) AS INTEGER) AS y "
            f"FROM {src}) GROUP BY p", [str(m)]).fetchall()
    finally:
        con.close()
    return {int(p): (int(g), int(dd), y0, y1, int(ny)) for p, g, dd, y0, y1, ny in rows}


def _stamp(key: str):
    """What a memoised coverage_hexes answer depends on for one collection: its manifest and its claim file."""
    m = _manifest(key)
    d = coverage._index_for(key)[0]
    out = []
    for f in (m, (d / "_build.json") if d is not None else None):
        try:
            out.append(f.stat().st_mtime_ns if f is not None else None)
        except OSError:
            out.append(None)
    return tuple(out)


def _day_of(key: str):
    """granule -> 'YYYYMMDD'. ILATM2 and ATL06 names carry the date; GLAH06 names do not, so read its index's gdate."""
    if key != "GLAS":
        return lambda g: (m := re.search(r"_(\d{8})", g)) and m.group(1)
    return _glas_days().get


def _packed(key: str) -> set[str]:
    """The collection's compacted claim, as h3 strings (index.manifest_cells reads _build.json)."""
    d = coverage._index_for(key)[0]
    return {h3.int_to_str(c) for c in index.manifest_cells(d)} if d is not None else set()


def _in_bbox(lat: float, lon: float, bbox) -> bool:
    w, s, e, n = bbox
    return s <= lat <= n and w <= lon <= e


def _mission_row(granules: set, years: set, days: set) -> dict:
    ys = sorted(years)
    return {"passes": len(granules), "days": len(days - {None}), "year_min": ys[0] if ys else None, "year_max": ys[-1] if ys else None,
            "n_years": len(ys)}


def coverage_hexes(res: int = 5, bbox=None) -> dict:
    res = int(res)
    if not 0 <= res <= INDEX_RES:
        raise ValueError(f"res must be 0..{INDEX_RES}: index rows are keyed at res {INDEX_RES}; got {res}")
    # Memoised on what the answer depends on. It used to loop every manifest row in Python on every call (1.4M rows
    # for Greenland) and rescan the GLAS index for dates: 15-24 s per resolution on the deployed box, x3 per page load.
    key = (res, tuple(bbox) if bbox is not None else None, tuple(_stamp(k) for k in MISSIONS))
    hit = _HEX_MEMO.get(key)
    if hit is not None:
        return hit
    acc: dict[str, dict] = {}
    for k in MISSIONS:
        for cell, (passes, days, y0, y1, ny) in _aggregate(k, res).items():
            acc.setdefault(h3.int_to_str(cell), {})[k] = {"passes": passes, "days": days, "year_min": y0,
                                                          "year_max": y1, "n_years": ny}
    packed = {k: _packed(k) for k in MISSIONS}
    hexes = []
    for hx in sorted(acc):
        lat, lon = h3.cell_to_latlng(hx)
        if bbox is not None and not _in_bbox(lat, lon, bbox):
            continue
        ms = {k: {**v, "claimed": index.cells_within(packed[k], [hx])} for k, v in acc[hx].items()}
        hexes.append({"h3": hx, "lat": round(lat, 5), "lon": round(lon, 5), "missions": ms, "n_missions": len(ms),
                      "claimed": all(v["claimed"] for v in ms.values())})
    out = {"res": res, "hexes": hexes}
    _HEX_MEMO[key] = out
    while len(_HEX_MEMO) > _HEX_MEMO_MAX:
        _HEX_MEMO.pop(next(iter(_HEX_MEMO)))
    return out


def area_bbox(lat: float, lon: float, radius_km: float) -> list[float]:
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    return [round(lon - dlon, 5), round(lat - dlat, 5), round(lon + dlon, 5), round(lat + dlat, 5)]


def area_summary(bbox) -> dict:
    """Distinct passes, days and years per mission over an area. A res-5 index cell counts when its centre lies within the
    area grown by ~one res-5 edge (9 km), so a small radius still reaches the hex it sits in."""
    w, s, e, n = bbox
    kx = 9 / (111.32 * max(math.cos(math.radians((s + n) / 2)), 0.05))
    grown = [w - kx, s - 9 / 111.32, e + kx, n + 9 / 111.32]
    out = {}
    for key in MISSIONS:
        grans, years, cells, days, day = set(), set(), set(), set(), _day_of(key)
        for cell, granule, ym in _rows(key):
            lat, lon = h3.cell_to_latlng(h3.int_to_str(int(cell)))
            if _in_bbox(lat, lon, grown):
                grans.add(granule)
                days.add(day(granule))
                cells.add(cell)
                if ym:
                    years.add(int(str(ym)[:4]))
        out[key] = {**_mission_row(grans, years, days), "hexes": len(cells)}
    return out
