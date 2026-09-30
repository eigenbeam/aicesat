"""Level 1 of the demo ladder: which missions passed over each H3 hex, how often, and in which years.

Read from each collection's coverage manifest (DISTINCT h3_cell, granule, ym; coverage._ensure_manifest keeps it
fresh) -- no NASA calls, no lake reads. Index rows are keyed at res 5, so a coarser hex is the roll-up of its res-5
children, and a granule crossing two children counts once. A PASS is a distinct granule.

`claimed` answers "would a build over this hex be accepted for this mission": the same compacted-claim containment
test the build gate uses (index.cells_within), so the globe never offers a hex the build would refuse."""
from __future__ import annotations

import math

import duckdb
import h3

from . import coverage, index

MISSIONS = ("GLAS", "ICESSN", "ATL06")
LABELS = {"GLAS": "ICESat (GLAS, 2003-09)", "ICESSN": "Operation IceBridge ATM (2009-19)",
          "ATL06": "ICESat-2 land ice ATL06 (2018-)"}
INDEX_RES = 5


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


def _packed(key: str) -> set[str]:
    """The collection's compacted claim, as h3 strings (index.manifest_cells reads _build.json)."""
    d = coverage._index_for(key)[0]
    return {h3.int_to_str(c) for c in index.manifest_cells(d)} if d is not None else set()


def _in_bbox(lat: float, lon: float, bbox) -> bool:
    w, s, e, n = bbox
    return s <= lat <= n and w <= lon <= e


def _mission_row(granules: set, years: set) -> dict:
    ys = sorted(years)
    return {"passes": len(granules), "year_min": ys[0] if ys else None, "year_max": ys[-1] if ys else None,
            "n_years": len(ys)}


def coverage_hexes(res: int = 5, bbox=None) -> dict:
    res = int(res)
    if not 0 <= res <= INDEX_RES:
        raise ValueError(f"res must be 0..{INDEX_RES}: index rows are keyed at res {INDEX_RES}; got {res}")
    acc: dict[str, dict] = {}
    for key in MISSIONS:
        for cell, granule, ym in _rows(key):
            hx = h3.int_to_str(int(cell))
            if h3.get_resolution(hx) > res:
                hx = h3.cell_to_parent(hx, res)
            m = acc.setdefault(hx, {}).setdefault(key, {"granules": set(), "years": set()})
            m["granules"].add(granule)
            if ym:
                m["years"].add(int(str(ym)[:4]))
    packed = {k: _packed(k) for k in MISSIONS}
    hexes = []
    for hx in sorted(acc):
        lat, lon = h3.cell_to_latlng(hx)
        if bbox is not None and not _in_bbox(lat, lon, bbox):
            continue
        ms = {k: {**_mission_row(v["granules"], v["years"]), "claimed": index.cells_within(packed[k], [hx])}
              for k, v in acc[hx].items()}
        hexes.append({"h3": hx, "lat": round(lat, 5), "lon": round(lon, 5), "missions": ms, "n_missions": len(ms),
                      "claimed": all(v["claimed"] for v in ms.values())})
    return {"res": res, "hexes": hexes}


def area_bbox(lat: float, lon: float, radius_km: float) -> list[float]:
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    return [round(lon - dlon, 5), round(lat - dlat, 5), round(lon + dlon, 5), round(lat + dlat, 5)]


def area_summary(bbox) -> dict:
    """Distinct passes and years per mission over an area. A res-5 index cell counts when its centre lies within the
    area grown by ~one res-5 edge (9 km), so a small radius still reaches the hex it sits in."""
    w, s, e, n = bbox
    kx = 9 / (111.32 * max(math.cos(math.radians((s + n) / 2)), 0.05))
    grown = [w - kx, s - 9 / 111.32, e + kx, n + 9 / 111.32]
    out = {}
    for key in MISSIONS:
        grans, years, cells = set(), set(), set()
        for cell, granule, ym in _rows(key):
            lat, lon = h3.cell_to_latlng(h3.int_to_str(int(cell)))
            if _in_bbox(lat, lon, grown):
                grans.add(granule)
                cells.add(cell)
                if ym:
                    years.add(int(str(ym)[:4]))
        out[key] = {**_mission_row(grans, years), "hexes": len(cells)}
    return out
