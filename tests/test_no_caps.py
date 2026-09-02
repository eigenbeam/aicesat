"""No cap, no subsample: every collection reads what the request selects (issue #24, tier 1).

Three caps used to silently shrink the answer:
  * planner._ensure sliced the CMR result to the first `max_granules` — and CMR returns oldest-first, so a scene's
    ATL03 was the EARLIEST 8 granules, never a sample across the record. A bias, not just a cap.
  * timeseries._load_all decimated each mission to 80k points. The median is robust to that; the count thresholds
    around it (_MIN_BIN_PTS, _MIN_REF_PTS, and n_ref feeding the confidence score) are not, so thinning moved which
    candidate cells existed at all.
  * timeseries.candidates truncated the result list to 60.

These tests fail against the capped code, which is the point.
"""
import h3
import numpy as np
import pytest

from aicesat import coreg, coverage, index, planner, timeseries

BOX = (-45.5, 71.8, -44.5, 72.2)
from aicesat import scene as scene_mod


# --- granules: the planner must use every indexed granule in the window ------------------------------------------
def _ref_rows(names):
    """Minimal chunk_refs rows: one chunk per granule, one cell."""
    return [{"granule": n, "url": f"https://x/{n}", "s3url": None, "beam": "gt1l", "sdp_epoch": 0.0, "cycle": 1,
             "chunk_index": 0, "h3_cell": 603599455437979647, "ph_start": 0, "ph_end": 10} for n in names]


class _FakeStats:
    requests = 0
    presigns = 0


class _FakeReader:
    """Nothing is fetched in these tests; the real RangeReader would reach for EDL credentials on construction."""
    def __init__(self, **kw):
        self.stats = _FakeStats()

    def presign_all(self, urls):
        return {u: u for u in urls}


class _FakeRefs:
    def __init__(self, rows):
        self._rows = rows
        self.num_rows = len(rows)

    def to_pylist(self):
        return list(self._rows)


def test_planner_uses_every_indexed_granule_in_the_window(monkeypatch):
    names = [f"ATL03_2019{m:02d}01000000_0235{m:02d}03_007_01.h5" for m in range(1, 13)]   # 12 > the old cap of 8
    rows = _ref_rows(names)

    monkeypatch.setattr(index, "manifest_cells", lambda d: set(planner.cells_for_bbox(BOX)))
    monkeypatch.setattr(index, "chunk_refs", lambda cells, **kw: _FakeRefs(rows))
    # everything already materialized -> nothing to fetch, so the test needs no network and no lake
    monkeypatch.setattr(planner.lake, "ingested_chunk_cells",
                        lambda mission, ns: {(r["granule"], r["beam"], r["chunk_index"], int(r["h3_cell"])) for r in rows})
    monkeypatch.setattr(planner.lake, "mark_ingested_many", lambda *a, **k: None)
    monkeypatch.setattr(planner, "RangeReader", _FakeReader)

    plan = planner.ensure(BOX, ("2018-10-01", "2026-01-01"))

    assert plan["stats"]["granules"] == sorted(names), (
        f"planner used {len(plan['stats']['granules'])} of {len(names)} indexed granules")


def test_planner_window_filter_reads_the_granule_name(monkeypatch):
    """Window selection used to come from the CMR search; it now comes from the granule name in the index."""
    names = ["ATL03_20190601000000_02350103_007_01.h5", "ATL03_20230601000000_02350203_007_01.h5"]
    rows = _ref_rows(names)
    monkeypatch.setattr(index, "manifest_cells", lambda d: set(planner.cells_for_bbox(BOX)))
    monkeypatch.setattr(index, "chunk_refs", lambda cells, **kw: _FakeRefs(rows))
    monkeypatch.setattr(planner.lake, "ingested_chunk_cells",
                        lambda mission, ns: {(r["granule"], r["beam"], r["chunk_index"], int(r["h3_cell"])) for r in rows})
    monkeypatch.setattr(planner.lake, "mark_ingested_many", lambda *a, **k: None)
    monkeypatch.setattr(planner, "RangeReader", _FakeReader)

    plan = planner.ensure(BOX, ("2019-01-01", "2019-12-31"))
    assert plan["stats"]["granules"] == [names[0]]
    assert plan["stats"]["granules_indexed_for_cells"] == 2      # the other one is indexed, just out of window


def test_planner_takes_no_granule_cap_argument():
    """An accepted-but-ignored cap is worse than a cap: it reads as a working knob."""
    import inspect
    for fn in (planner.ensure, planner.ensure_cells, planner._ensure):
        assert "max_granules" not in inspect.signature(fn).parameters, f"{fn.__name__} still takes max_granules"


# --- points: the time-series must bin every point, not a thinned sample ------------------------------------------
def test_load_all_keeps_every_point(monkeypatch):
    """_load_all used to decimate any mission over 80k points via np.linspace."""
    n = 90_000
    rng = np.random.default_rng(0)
    arrays = {"lon": np.full(n, -45.0) + rng.uniform(-1e-3, 1e-3, n),
              "lat": np.full(n, 72.0) + rng.uniform(-1e-3, 1e-3, n),
              "h": 1000.0 + rng.normal(0, 0.5, n),
              "t": np.full(n, np.datetime64("2019-06-01"), dtype="datetime64[ms]")}
    meta = {"native_frame": "ITRF2014"}
    monkeypatch.setattr(coreg, "_reload_arrays", lambda s: (arrays, meta))

    doc = {"frame": scene_mod.local_frame((-45.5, 71.8, -44.5, 72.2)),
           "series": {"ATL06": {"cache_key": "irrelevant"}}}
    recs = timeseries._load_all(doc, 2005.0)

    assert len(recs) == 1
    assert recs[0]["x"].size == n, f"thinned {n} points to {recs[0]['x'].size}"


# --- results: the candidate list must not be truncated -----------------------------------------------------------
def _many_cell_recs(n_cells: int):
    """Points in `n_cells` distinct res-9 cells, each with enough reference points and time windows to qualify."""
    centre = h3.latlng_to_cell(72.0, -45.0, 9)
    cells = list(h3.grid_disk(centre, 6))[:n_cells]
    assert len(cells) == n_cells, f"grid_disk gave {len(cells)} cells"
    rng = np.random.default_rng(1)
    frame = scene_mod.local_frame((-45.5, 71.5, -44.5, 72.5))
    out = []
    for mission, years in (("GLAS", [2005.05]), ("ATL06", [2019.5, 2020.5, 2021.5])):
        lats, lons, yrs = [], [], []
        for c in cells:
            clat, clon = h3.cell_to_latlng(c)
            per = 10                                    # >= _MIN_REF_PTS for the plane, >= _MIN_BIN_PTS per window
            for y in years:
                lats.append(clat + rng.uniform(-1e-4, 1e-4, per))
                lons.append(clon + rng.uniform(-3e-4, 3e-4, per))
                yrs.append(np.full(per, y) + rng.uniform(-0.04, 0.04, per))
        lat = np.concatenate(lats); lon = np.concatenate(lons); yr = np.concatenate(yrs)
        x, y = scene_mod.to_local(frame, lon, lat)
        h = 1000.0 + 0.01 * x + rng.normal(0, 0.02, x.size)
        out.append({"mission": mission, "lat": lat, "lon": lon, "x": x, "y": y, "h": h, "yr": yr})
    return frame, out


def test_candidates_are_not_truncated(monkeypatch):
    """The old default kept only the 60 highest-confidence cells; the rest vanished with no indication."""
    n_cells = 85                                        # comfortably past the retired max_candidates=60
    frame, recs = _many_cell_recs(n_cells)
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    out = timeseries.candidates({"frame": frame, "z0": 1000.0}, h3_res=9, delta_t=1.0)
    assert len(out["candidates"]) == n_cells, f"{n_cells} qualifying cells -> {len(out['candidates'])} reported"


def test_candidates_takes_no_result_cap_argument():
    import inspect
    assert "max_candidates" not in inspect.signature(timeseries.candidates).parameters


# --- regression guard: none of this may come back ------------------------------------------------------------------
def test_no_granule_slice_or_point_decimation_in_the_source():
    """A grep, deliberately. Each of these was added for performance and each silently changed an answer; the next
    one will look just as reasonable in review. Extend the list as tier 2 lands."""
    import pathlib
    import re

    forbidden = {
        r"\[:\s*max_granules\s*\]": "granule-list truncation",
        r"_MAX_PTS_PER_MISSION": "per-mission point decimation",
        r"\bmax_candidates\b": "candidate-list truncation",
        r"\bmax_points\b": "display-stride cap on stored points (#25)",
        r"stride: int = ": "a stride parameter on the transport (#25)",
        # Deleted with the pull transport. Both existed ONLY to make a PREFIX of a growing array a fair sample, and
        # nothing takes a prefix any more: the stream delivers every point. If either comes back, so has a cap.
        r"PARTIAL_PREVIEW_CAP": "the preview cap and its power-of-2 thinning loop",
        r"_sample_order": "the write-time shuffle that existed only for prefix fetches",
    }
    def _enclosing_def(text: str, pos: int) -> str:
        before = text[:pos]
        starts = [ln for ln in before.split("\n") if ln.startswith("def ")]
        return starts[-1][4:].split("(")[0] if starts else ""

    src = pathlib.Path(planner.__file__).parent
    hits = []
    for path in sorted(src.glob("*.py")):
        text = path.read_text()
        for pattern, what in forbidden.items():
            for m in re.finditer(pattern, text):
                if _enclosing_def(text, m.start()).endswith("_legacy"):
                    continue          # atl03.extract_legacy: unreferenced, deleted in tier 2 of #24
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{path.name}:{line}: {what} ({m.group(0)!r})")
    assert not hits, "caps reintroduced:\n  " + "\n  ".join(hits)
