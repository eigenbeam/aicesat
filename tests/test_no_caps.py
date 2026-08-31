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
from aicesat import scene as scene_mod


# --- granules: the planner must hand every searched granule to the index ---------------------------------------
class _Sentinel(Exception):
    """Stops _ensure once it has revealed the granule list, so the test needs no lake, no network, no NASA."""


def test_planner_indexes_every_granule_the_search_returns(monkeypatch):
    found = [{"id": f"g{i:03d}"} for i in range(25)]        # more than the old max_granules default of 8
    seen = {}

    def _capture(granules):
        seen["granules"] = list(granules)
        raise _Sentinel

    monkeypatch.setattr(coverage, "search", lambda *a, **k: found)
    monkeypatch.setattr(coverage, "granule_name", lambda g: g["id"])
    monkeypatch.setattr(index, "ensure_index", _capture)

    with pytest.raises(_Sentinel):
        planner.ensure((-45.5, 71.8, -44.5, 72.2), ("2018-10-01", "2026-01-01"))

    assert seen["granules"] == found, (
        f"planner truncated {len(found)} searched granules to {len(seen['granules'])} — "
        "and CMR is oldest-first, so the survivors are the earliest, not a spread")


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
