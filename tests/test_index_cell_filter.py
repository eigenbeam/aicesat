"""The row filter must be at the resolution the rows are keyed at, or it matches nothing.

Regression: scripts/build_index.py passed the CLAIM cells to the ATL03 granule builder. Those equal the addressing
cells only when the claim resolution happens to equal index.H3_RES, which is true for a large region and false for a
small one — a ~5 km bbox claims at res 9 while ATL03 addresses at res 6. Membership in cells_filter is exact, with no
ancestry walk, so every granule of a tight-box build matched zero rows and reported success: "0 (chunk,cell) rows,
0 beams" for all 42 granules, an index that claimed coverage and held nothing.

The other three collections already passed addressing cells. This pins the invariant for all of them.
"""
import h3
import pytest

from aicesat import index, planner

TIGHT = (86.90, 27.88, 86.95, 27.92)      # Imja Tsho: small enough to claim finer than it addresses
WIDE = (-52.0, 62.0, -44.0, 70.0)         # large enough that claim res == addressing res


def test_a_small_bbox_claims_finer_than_it_addresses():
    """The premise. Without this asymmetry the bug is invisible, which is why it survived."""
    fine = planner.coverage_cells(TIGHT)
    claim_res = {h3.get_resolution(h3.int_to_str(int(c))) for c in fine}
    assert claim_res == {9}
    assert index.H3_RES == 6
    assert claim_res != {index.H3_RES}


def test_a_large_bbox_hides_the_bug():
    """Why it was never noticed: for a big region the two sets coincide exactly."""
    fine = planner.coverage_cells(WIDE)
    assert {h3.get_resolution(h3.int_to_str(int(c))) for c in fine} == {index.H3_RES}
    assert set(index.cells_filter(fine, index.H3_RES)) == {int(c) for c in fine}


def test_claim_cells_are_refused_for_a_small_bbox():
    fine = planner.coverage_cells(TIGHT)
    with pytest.raises(ValueError, match="nothing could match"):
        index.cells_filter(fine, index.H3_RES)


def test_addressing_cells_are_accepted():
    fine = planner.coverage_cells(TIGHT)
    addr = planner.addressing_cells(fine, index.H3_RES)
    got = index.cells_filter(addr, index.H3_RES)
    assert got and len(got) == len(addr)


def test_no_res_argument_keeps_the_old_permissive_behaviour():
    """Callers that do not say what resolution their rows use are not policed."""
    fine = planner.coverage_cells(TIGHT)
    assert len(index.cells_filter(fine)) == len(fine)


def test_none_still_means_index_everything():
    assert index.cells_filter(None, index.H3_RES) is None


def test_the_build_script_passes_addressing_cells_not_claim_cells():
    """The fix itself. Reading the source is crude, but the alternative is a full network build."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "scripts" / "build_index.py"
    t = src.read_text()
    assert "cells = planner.addressing_cells(fine, index.H3_RES)" in t
    assert "ensure_index(granules, workers=a.workers, cells=cells)" in t
    assert "ensure_index(granules, workers=a.workers, cells=fine)" not in t
    assert "cells=fine)" in t, "the CLAIM must still be written at the claim resolution"
