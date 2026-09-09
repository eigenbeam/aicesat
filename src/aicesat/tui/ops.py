"""Timed operations: everything the TUI can do, as functions returning (data, Timing).

Nothing here renders. Nothing here re-implements the index or the lake either — each operation is a thin,
instrumented call into the library, so what the TUI reports is what the app itself would do. The split matters
because the interesting question in this codebase is always "what did that cost", and a wrapper that quietly
took a different path would answer the wrong one.
"""
from __future__ import annotations

import contextlib
import time

MISSION = "ATL06"
COLLECTION = "ATL06"


class Timing:
    """A named operation and its phase breakdown. `phase()` times a block; `add()` records a phase measured
    elsewhere — the library already returns its own `*_seconds`, and re-timing those from outside would double-count
    them against the wall clock."""

    def __init__(self, title: str):
        self.title = title
        self.phases: list[list] = []
        self._t0 = time.perf_counter()
        self.seconds = 0.0

    @contextlib.contextmanager
    def phase(self, name: str):
        row = [name, 0.0, ""]
        self.phases.append(row)
        t = time.perf_counter()
        try:
            yield row                      # the caller sets row[2] to a detail string
        finally:
            row[1] = time.perf_counter() - t
            self.seconds = time.perf_counter() - self._t0

    def add(self, name: str, seconds: float | None, detail: str = "") -> None:
        self.phases.append([name, seconds, detail])

    def stop(self) -> "Timing":
        self.seconds = time.perf_counter() - self._t0
        return self

    def rows(self) -> list[tuple]:
        return [tuple(r) for r in self.phases]


def as_cell_str(token) -> str:
    """An H3 cell as its hex string, from either form the TUI prints.

    `index cells` shows hex ids (8506f283fffffff) because that is what the coverage manifest carries; `lake cells`
    shows integers because that is what the lake partitions on. Both are the same cell, and a user pastes whichever
    one is in front of them — so both are accepted. Passing an all-digit id straight to h3 parses it as hex and
    overflows, which is what this exists to stop.
    """
    import h3

    if isinstance(token, int):
        return h3.int_to_str(token)
    t = str(token).strip()
    cell = h3.int_to_str(int(t)) if t.isdigit() else t
    if not h3.is_valid_cell(cell):
        raise ValueError(f"{token!r} is not an H3 cell id (expected hex like 8506f283fffffff or an integer)")
    return cell


def _manifest_months(res: int) -> list[tuple[str, int]]:
    """Distinct granules per calendar month, from the rolled-up coverage manifest — 4 ms against a single small
    file, versus opening every granule parquet. Empty when the manifest has not been built yet."""
    import duckdb

    from .. import coverage, index_atl06

    _, mf, _ = coverage._manifest_paths(index_atl06._index_dir(res))
    if not mf.exists():
        return []
    con = duckdb.connect()
    try:
        return con.execute("SELECT ym, count(DISTINCT granule) FROM read_parquet(?) GROUP BY ym ORDER BY ym",
                           [str(mf)]).fetchall()
    finally:
        con.close()


def index_overview(res: int = 5) -> tuple[dict, Timing]:
    """Index summary, per-cell coverage, the month histogram, and the claim as stamped on disk."""
    import json

    from .. import api, index_atl06

    t = Timing(f"index · {COLLECTION}")
    with t.phase("status") as p:
        st = api.index_status(COLLECTION)
        p[2] = f"{st['granules']:,} granules · {len(st['cells']):,} cells"
    with t.phase("month rollup") as p:
        months = _manifest_months(res)
        p[2] = f"{len(months)} months"
    d = index_atl06._index_dir(res)
    build = {}
    with t.phase("claim") as p:
        bf = d / "_build.json"
        if bf.exists():
            try:
                doc = json.loads(bf.read_text())
                build = {"bounds": doc.get("bounds"), "target": doc.get("target"),
                         "claim_cells": len(doc.get("cells") or []), "coverage_res": doc.get("coverage_res")}
            except Exception as e:
                build = {"error": f"{type(e).__name__}: {e}"}
        p[2] = f"{build.get('claim_cells', 0):,} claim cells" if build else "no _build.json"
    return {"status": st, "months": months, "build": build, "index_dir": str(d)}, t.stop()


def index_chunk_rollup(res: int = 5) -> tuple[list[dict], Timing]:
    """Per-cell chunk and granule counts, straight off the index parquets.

    This one reads the whole index directory rather than the manifest, because the manifest holds DISTINCT
    (cell, granule, ym) and so cannot answer "how many chunks". DuckDB's column pruning keeps it to the three
    columns needed — 71 ms over 845 files locally — but the cost still scales with the store, so it is only ever
    run on an explicit command, never on a refresh.
    """
    import duckdb

    from .. import index_atl06

    t = Timing("index chunks")
    d = index_atl06._index_dir(res)
    with t.phase("scan index parquets") as p:
        con = duckdb.connect()
        try:
            rows = con.execute(
                f"SELECT h3_cell, count(*) chunks, count(DISTINCT granule) granules, count(DISTINCT beam) beams "
                f"FROM read_parquet('{d}/*.parquet') GROUP BY h3_cell ORDER BY chunks DESC").fetchall()
        finally:
            con.close()
        p[2] = f"{len(rows):,} cells · {sum(r[1] for r in rows):,} chunk rows"
    return [{"cell": r[0], "chunks": r[1], "granules": r[2], "beams": r[3]} for r in rows], t.stop()


def index_verify(res: int = 5) -> tuple[dict, Timing]:
    """Count the index by SCHEMA VERSION, which the cheap readdir count cannot do.

    index_status reports `granules` from a readdir, so a file left behind by an older schema is counted as built.
    On this machine that made a 57-granule index report "844 of 845, 100% built" — every stale file was serving
    old-semantics rows, and the next build deleted all 787 of them. Reading each footer costs work proportional to
    the store, so this is its own command rather than part of the overview.

    Read-only, unlike indexed_atl06_granules(), which deletes what it finds stale as it scans — correct for a
    build, wrong for a report that must be able to name a problem without also causing it.
    """
    import pyarrow.parquet as pq

    from .. import index_atl06

    t = Timing("index verify")
    d = index_atl06._index_dir(res)
    key, want = b"aicesat_atl06_index_version", index_atl06.ATL06_INDEX_VERSION
    out = {"dir": str(d), "current": 0, "stale": 0, "unreadable": 0, "empty": 0, "want": want, "files": 0}
    with t.phase("read footers") as p:
        for f in (sorted(d.glob("*.parquet")) if d.exists() else []):
            out["files"] += 1
            try:
                md = pq.ParquetFile(f).metadata
                ver = (md.schema.to_arrow_schema().metadata or {}).get(key, b"").decode(errors="replace")
            except Exception:
                out["unreadable"] += 1
                continue
            if ver != want:
                out["stale"] += 1
            else:
                out["current"] += 1
                if md.num_rows == 0:
                    out["empty"] += 1
        p[2] = (f"{out['files']:,} files · {out['current']:,} current · {out['stale']:,} stale · "
                f"{out['unreadable']:,} unreadable")
    return out, t.stop()


def plan(bbox, window=None, res: int = 5, force: bool = False) -> tuple[dict, Timing]:
    """Dry run: what a query over this box would read from the lake and what it would have to fetch. No network."""
    from .. import index_atl06

    t = Timing(f"plan · {COLLECTION}")
    with t.phase("index rows") as p:
        # settle=False: this is an inspection, and it must not block behind the background writer.
        pl = index_atl06.plan_bbox(bbox, window, res, force=force, settle=False)
        p[2] = (f"{len(pl['rows']):,} refs · {len(pl['chunk_cells']):,} chunks · "
                f"{len(pl['names']):,} granules · {len(pl['want_cells'])} cells")
    t.add("from lake", None, f"{pl['n_lake']:,} chunks already materialized")
    t.add("to fetch", None, f"{len(pl['todo']):,} chunks across {len(pl['by_url']):,} granules")
    return pl, t.stop()


def chunk_map(bbox, window=None, res: int = 5, granule: str | None = None,
              beam: str | None = None) -> tuple[dict, Timing]:
    """The byte-range layout one granule contributes to this query, and the chunk-by-cell occupancy grid.

    Byte ranges are coalesced PER URL. Pooling ranges across granules is meaningless — their offsets index
    different files — so this is always scoped to a single granule, and to one beam when asked.
    """
    from .. import access, index_atl06

    t = Timing(f"chunks · {COLLECTION}")
    with t.phase("index rows") as p:
        pl = index_atl06.plan_bbox(bbox, window, res, settle=False)
        p[2] = f"{len(pl['rows']):,} refs · {len(pl['chunk_cells']):,} chunks"
    rows = pl["rows"]
    if not rows:
        return {"plan": pl, "granule": None, "beam": None, "ranges": [], "spans": [], "chunk_cells": {}}, t.stop()

    with t.phase("pick granule") as p:
        by_gb: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            by_gb.setdefault((r["granule"], r["beam"]), []).append(r)
        # substring match on the granule, so a partial id is enough to type; either filter may be given alone.
        cand = [k for k in by_gb
                if (granule is None or granule in k[0]) and (beam is None or k[1] == beam)]
        if not cand:
            what = " and ".join(x for x in (f"granule matching {granule!r}" if granule else "",
                                            f"beam {beam!r}" if beam else "") if x)
            raise ValueError(f"no {what} over this box ({len({k[0] for k in by_gb})} granules, "
                             f"beams {', '.join(sorted({k[1] for k in by_gb}))} do)")
        gb = max(cand, key=lambda k: len({r["chunk_index"] for r in by_gb[k]}))
        sel = by_gb[gb]
        p[2] = f"{gb[0]} · {gb[1]}"

    with t.phase("coalesce") as p:
        ranges = sorted({(int(r[f"{ds}_offset"]), int(r[f"{ds}_size"]))
                         for r in sel for ds in index_atl06.ATL06_DATASETS})
        spans = access.coalesce(ranges)
        p[2] = (f"{len(ranges)} ranges → {len(spans)} GETs "
                f"(max_gap {access.MAX_GAP_BYTES // 1024} KB)")

    chunk_cells = {k: v for k, v in pl["chunk_cells"].items() if k[0] == gb[0]}
    return {"plan": pl, "granule": gb[0], "beam": gb[1], "ranges": ranges, "spans": spans,
            "chunk_cells": chunk_cells, "chunks": len({r["chunk_index"] for r in sel})}, t.stop()


def lake_overview(with_rows: bool = False) -> tuple[dict, Timing]:
    """Lake summary plus per-cell stats.

    `with_rows` reads every Parquet footer in the mission — 91 s against 22,844 files locally, versus 1 s without.
    That is the "work proportional to the store rather than to the request" trap this codebase has hit five times,
    so row counts are opt-in and never on a path that repeats.
    """
    from .. import lake

    t = Timing(f"lake · {MISSION}")
    summ = None
    if with_rows:
        # lake_summary walks every cell WITH row counts; without --rows the caller derives the same totals from
        # cell_stats below, so calling it here would buy nothing and cost the whole footer walk.
        with t.phase("summary") as p:
            summ = lake.lake_summary(MISSION)
            p[2] = f"{summ['cells']} cells · {summ['files']:,} files"
    with t.phase("cell stats") as p:
        cells = lake.cell_stats(MISSION, with_rows=with_rows)
        p[2] = (f"{len(cells)} cells · rows {'read' if with_rows else 'SKIPPED (--rows to read them)'}")
    with t.phase("missions") as p:
        miss = lake.missions()
        p[2] = ", ".join(f"{m['mission']} {m['cells']}" for m in miss)
    return {"summary": summ, "cells": cells, "missions": miss}, t.stop()


def materialize(cells: list[int], window=None, res: int = 5, on_plan=None, on_granule=None) -> tuple[dict, Timing]:
    """Fetch and materialize the given H3 cells into the lake.

    fetch_bbox addresses by bbox, so a cell is converted to its bounding box and the read-back narrowed with
    `clip_cells`. The bbox of a hexagon necessarily overlaps its neighbours, so neighbouring cells are fetched too
    — that is reported rather than hidden, by diffing the lake's cell set across the call.
    """
    import h3

    from .. import index_atl06, lake

    t = Timing(f"materialize · {len(cells)} cell(s)")
    with t.phase("cell → bbox") as p:
        lats, lons = [], []
        for c in cells:
            for la, lo in h3.cell_to_boundary(as_cell_str(c)):
                lats.append(la); lons.append(lo)
        bbox = (min(lons), min(lats), max(lons), max(lats))
        p[2] = f"{bbox[0]:.3f} {bbox[1]:.3f} {bbox[2]:.3f} {bbox[3]:.3f}"
    before = set(lake.cell_stats(MISSION, with_rows=False))
    with t.phase("fetch + materialize") as p:
        arrays, stats = index_atl06.fetch_bbox(bbox, window, res, clip_cells=True,
                                               on_plan=on_plan, on_granule=on_granule)
        p[2] = _stats_detail(stats)
    with t.phase("settle writes") as p:
        lake.drain_writes(MISSION)
        after = set(lake.cell_stats(MISSION, with_rows=False))
        gained = sorted(after - before)
        p[2] = f"{len(gained)} new cell(s)" if gained else "no new cells (already materialized)"
    _add_library_phases(t, stats)
    return {"bbox": bbox, "stats": stats, "points": int(arrays["lat"].size) if arrays.get("lat") is not None else 0,
            "gained": gained, "requested": list(cells)}, t.stop()


def query(bbox, window=None, res: int = 5, force: bool = False,
          on_plan=None, on_granule=None) -> tuple[dict, Timing]:
    """Read points over the box — from the lake where they are already materialized, from NASA by byte range where
    they are not, in one call. The lake/NASA split is the whole point, so it is what the timing reports."""
    from .. import index_atl06

    t = Timing(f"query · {COLLECTION}")
    with t.phase("fetch (lake + nasa)") as p:
        arrays, stats = index_atl06.fetch_bbox(bbox, window, res, force=force,
                                               on_plan=on_plan, on_granule=on_granule)
        p[2] = _stats_detail(stats)
    _add_library_phases(t, stats)
    n = int(arrays["lat"].size) if arrays.get("lat") is not None else 0
    return {"arrays": arrays, "stats": stats, "points": n}, t.stop()


def build(bbox, res: int = 5, workers: int = 8, window=None, on_plan=None, on_granule=None) -> tuple[dict, Timing]:
    """Build the index over the box. Resumable: a re-run indexes only what is missing or cannot prove it covers
    newly claimed ground."""
    from .. import build_atl06

    t = Timing(f"build · {COLLECTION}")
    with t.phase("build") as p:
        out = build_atl06.build_bbox(bbox, res=res, workers=workers, window=window,
                                     on_plan=on_plan, on_granule=on_granule)
        p[2] = (f"{out['ok']} ok · {out['err']} err · {out['rows']:,} rows "
                f"({out['already_indexed']} already indexed)")
    t.add("claim", None, "stamped" if out["claimed"] else "WITHHELD — re-run to finish")
    return out, t.stop()


def _stats_detail(stats: dict) -> str:
    from .render import human_bytes
    bits = [f"{stats.get('chunks_from_lake', 0):,} from lake", f"{stats.get('chunks_from_nasa', 0):,} from nasa"]
    if stats.get("requests"):
        bits.append(f"{stats['requests']:,} GETs · {human_bytes(stats.get('bytes', 0))}")
    return " · ".join(bits)


def _add_library_phases(t: Timing, stats: dict) -> None:
    """Fold in the phase timings the library already measured. These are sub-phases of the call above, not extra
    wall time, so they are reported as a breakdown of it rather than appended to the total."""
    for key, label, note in (("presign_seconds", "  presign", ""),
                             ("fetch_seconds", "  nasa fetch", ""),
                             ("materialize_seconds", "  materialize", ""),
                             ("decode_materialize_seconds", "  materialize", ""),
                             # AccessStats sums each request's duration across the fetch pool, so this routinely
                             # EXCEEDS the wall time of the phase it sits under. Labelled, not hidden: the ratio to
                             # wall time is how much overlap the pool actually achieved.
                             ("seconds", "  range reads", "summed across threads, not wall")):
        if stats.get(key):
            t.add(label, float(stats[key]), note)
