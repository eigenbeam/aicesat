"""Where a build will be accepted, shown and explained: the claim outline, the refusal message, and "fit to indexed area".

The report that motivated this: a box drawn ~1 km too wide at Jakobshavn failed every collection ("outside the claimed
extent [-49.91, 27.791, 85.71, 72.651] (west by 0.028deg)"). Nothing on the map showed the indexed area, the message
printed one bounding box around Nepal AND Greenland, and the only fix it offered was a rebuild.
"""
import pathlib
import re
import shutil
import subprocess

import h3
import pytest

from aicesat import coverage, geom, index, planner

BUILD = (-49.90, 69.10, -49.30, 69.25)             # the Jakobshavn index build box
USER = (-49.9382, 69.113, -49.2659, 69.2297)       # the box that was refused: ~1.1 km past the west edge
FAR = (85.46, 28.24, 85.57, 28.35)                 # a second, disjoint claim (Nepal)


def _claim(d, *boxes):
    for b in boxes:
        index.write_build_manifest(d, b, 5, cells=planner.coverage_cells(b))
    return d


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Two collections indexed over the same Jakobshavn box, one of them also over Nepal."""
    dirs = {"GLAS": _claim(tmp_path / "glas", BUILD, FAR), "ICESSN": _claim(tmp_path / "icessn", BUILD)}
    monkeypatch.setattr(coverage, "_index_for", lambda k: (dirs.get(k), 5, None))
    return dirs


# ---- 1. the outline IS the acceptable ground -----------------------------------------------------------------------
def test_the_outline_is_one_piece_per_disjoint_claim_and_contains_the_build_box(tmp_path):
    d = _claim(tmp_path / "x", BUILD)
    pieces = coverage.claim_regions(d)
    assert len(pieces) == 1
    w, s, e, n = pieces[0]["bbox"]
    # The claim is every coverage cell OVERLAPPING the build box, so it reaches past the box by at most one cell.
    assert w <= BUILD[0] and s <= BUILD[1] and e >= BUILD[2] and n >= BUILD[3]
    km = coverage.overhang_km(pieces[0]["bbox"], BUILD)
    assert km and max(km.values()) < 0.5, km
    ring = pieces[0]["outer"]
    assert len(ring) > 20 and all(len(p) == 2 for p in ring), "a hex-edged ring as [lon, lat]"

    _claim(d, FAR)                                 # a second build elsewhere: the cache must see the new manifest
    assert len(coverage.claim_regions(d)) == 2


def test_an_empty_or_missing_claim_has_no_outline(tmp_path):
    assert coverage.claim_regions(tmp_path / "never-built") == []
    d = _claim(tmp_path / "x", BUILD)
    index.invalidate_claim(d, "test")
    assert coverage.claim_regions(d) == []


# ---- 2. the refusal names the overhang against the piece the selection is about ------------------------------------
def test_the_refusal_says_how_far_past_which_edge_in_km(two):
    gap = coverage.coverage_gap(two["GLAS"], USER)
    # 0.028 deg of longitude at 69.2 N is 1.1 km.
    assert "1.1 km past its west edge" in gap, gap
    assert "Fit to indexed area" in gap
    # The Nepal claim is irrelevant here and must not leak in as one continent-spanning box.
    assert "85." not in gap and "27." not in gap, gap
    # Measured against that box, the east overhang VANISHED: its east edge is at 85.7 E, so the old message said
    # "west by 0.028deg" alone for GLAS and ATL06, while the box also ran ~950 m past the Jakobshavn piece's east edge.
    assert re.search(r"9\d\d m past its east edge", gap), gap
    assert gap == coverage.coverage_gap(two["ICESSN"], USER), "same piece, same answer, whatever else is indexed"


def test_a_selection_nowhere_near_a_claim_says_how_far_the_nearest_one_is(two):
    gap = coverage.coverage_gap(two["ICESSN"], (-1.0, 1.0, -0.9, 1.1))
    assert re.search(r"nearest indexed area, \[.*\], is [\d,]+ km away", gap), gap


def test_the_coverage_check_tells_partly_indexed_from_not_indexed_here(two, tmp_path, monkeypatch):
    """"reaches outside index" was shown for a collection with no index anywhere near (ATL03 at Jakobshavn: Nepal
    only), and only a fit found out. The row, and the Build gate, need the difference up front."""
    dirs = dict(two, ATL06=_claim(tmp_path / "atl06", FAR))
    monkeypatch.setattr(coverage, "_index_for", lambda k: (dirs.get(k), 5, None))
    rows = {c["key"]: c for c in coverage.check_coverage(USER)["collections"]}
    assert rows["ICESSN"]["covered"] is False and rows["ICESSN"]["claim_overlap"] is True
    assert rows["ATL06"]["covered"] is False and rows["ATL06"]["claim_overlap"] is False
    assert rows["GLAS"]["claim_overlap"] is True
    assert {c["key"]: c["claim_overlap"] for c in coverage.check_coverage(BUILD)["collections"]}["GLAS"] is True


def test_a_covered_selection_has_no_gap(two):
    assert coverage.coverage_gap(two["GLAS"], BUILD) is None


# ---- 3. fit: shrink only the overhanging sides, and verify with the gate itself -------------------------------------
def test_fit_shrinks_only_the_overhanging_sides_until_the_gate_accepts_it(two):
    r = coverage.fit_to_coverage(USER, ["GLAS", "ICESSN"])
    b = r["bbox"]
    assert b is not None, r
    for d in two.values():
        assert coverage.index_covers_area(d, b), "the fitted box must pass the gate a build will apply"
    assert b[1] == USER[1] and b[3] == USER[3], "south and north were inside the claim; they must not move"
    assert b[0] > USER[0] and b[2] < USER[2]
    assert set(r["moved"]) == {"west", "east"}
    # Not over-shrunk: the tightest straight edge the gate accepts is the build box's own (its claim is every cell
    # overlapping it, so an edge past it touches an unclaimed cell). Fit steps 100 m at a time, so within ~2 steps.
    kx = 111.32 * 0.3551                                        # km per degree of longitude at 69.2 N
    assert abs(b[0] - BUILD[0]) * kx < 0.25 and abs(b[2] - BUILD[2]) * kx < 0.25, (b, BUILD)
    assert all(round(v, 4) == v for v in b), "4 decimals, as the map keeps them — rounded INWARD, then re-checked"


def test_fit_ignores_what_never_flew_there_and_names_what_is_not_indexed_here(two, tmp_path, monkeypatch):
    dirs = dict(two, ATL06=_claim(tmp_path / "atl06", FAR))           # ATL06 indexed, but only over Nepal
    monkeypatch.setattr(coverage, "_index_for", lambda k: (dirs.get(k), 5, None))
    r = coverage.fit_to_coverage(USER, ["GLAS", "ICESSN", "ATL06", "GEDI"])   # GEDI never reaches 69 N
    assert r["bbox"] is not None and r["unindexed"] == ["ATL06"], r
    none = coverage.fit_to_coverage(USER, ["ATL06"])
    assert none["bbox"] is None and "ATL06" in none["reason"], none


def test_fit_gives_up_on_a_hole_it_cannot_shrink_around(tmp_path, monkeypatch):
    """A claim with a gap in the middle: edges cannot shrink around it, and fit must say so rather than loop or
    return a box the gate will refuse."""
    d = tmp_path / "holed"
    cells = [h3.int_to_str(c) for c in planner.coverage_cells(BUILD)]
    centre = h3.latlng_to_cell(69.175, -49.60, h3.get_resolution(cells[0]))
    hole = set(h3.grid_disk(centre, 3))
    index.write_build_manifest(d, BUILD, 5, cells=[c for c in cells if c not in hole])
    monkeypatch.setattr(coverage, "_index_for", lambda k: (d, 5, None))
    r = coverage.fit_to_coverage(BUILD, ["GLAS"])
    assert r["bbox"] is None and "outline" in r["reason"], r


# ---- 4. polygons: judged by their own ground, and fitted without redrawing them --------------------------------------
# The refused selection was in fact a POLYGON (these are its vertices). Its bounding box is what the message printed.
USER_POLY = [[-49.9301, 69.2297], [-49.9382, 69.1164], [-49.2659, 69.113], [-49.2695, 69.2251]]
# An L-shaped claim, and an L-shaped polygon inside it whose bounding box takes in the missing corner.
L_BUILDS = [(-49.90, 69.10, -49.60, 69.25), (-49.90, 69.10, -49.30, 69.15)]
L_POLY = [[-49.89, 69.24], [-49.62, 69.24], [-49.62, 69.14], [-49.31, 69.14], [-49.31, 69.11], [-49.89, 69.11]]


def test_explore_judges_a_polygon_by_its_own_ground_not_its_bounding_box(tmp_path, monkeypatch):
    """Explore disables Build when no checked collection is `covered`, so `covered` must be the answer the build gate
    gives. For a polygon the gate tests the polygon; judging its bounding box would block a polygon that builds."""
    d = _claim(tmp_path / "L", *L_BUILDS)
    monkeypatch.setattr(coverage, "_index_for", lambda k: (d, 5, None) if k == "GLAS" else (None, None, None))
    bb = (-49.89, 69.11, -49.31, 69.24)
    assert coverage.index_covers_area(d, bb, L_POLY) and not coverage.index_covers_area(d, bb), "fixture"
    glas = lambda r: next(c for c in r["collections"] if c["key"] == "GLAS")
    assert glas(coverage.check_coverage(bb, polygon=L_POLY))["covered"] is True
    assert glas(coverage.check_coverage(bb))["covered"] is False


def test_fit_cuts_a_polygon_down_to_the_index_and_never_adds_ground(two):
    from shapely.geometry import Polygon

    r = coverage.fit_to_coverage(tuple(geom.polygon_bbox(USER_POLY)), ["GLAS", "ICESSN"], polygon=USER_POLY)
    poly = r["polygon"]
    assert poly and r["bbox"] == list(geom.polygon_bbox(poly)), r
    for d in two.values():
        assert coverage.index_covers_area(d, r["bbox"], poly), "the fitted polygon must pass the gate"
    drawn, fitted = Polygon(USER_POLY), Polygon(poly)
    # Never adds ground, to the 4 decimals (~5 m) the map keeps a polygon's vertices at: the fit returns them at that
    # precision so the gate is checked on the polygon a build will actually receive.
    assert all(round(v, 4) == v for p in poly for v in p)
    assert drawn.buffer(1e-4).contains(fitted), "a fit only removes ground; it must never add any"
    inside = drawn.intersection(Polygon([(BUILD[0], BUILD[1]), (BUILD[2], BUILD[1]), (BUILD[2], BUILD[3]), (BUILD[0], BUILD[3])]))
    assert fitted.area > 0.97 * inside.area, "and not much more than it has to"
    assert set(r["moved"]) == {"west", "east"}


def test_fit_refuses_to_split_a_polygon(two):
    """Arms inside the index joined by a bridge outside it: cutting the bridge leaves two pieces, which is not the
    shape that was drawn. Say so; do not pick one."""
    arch = [[-49.80, 69.12], [-49.70, 69.12], [-49.70, 69.27], [-49.50, 69.27], [-49.50, 69.12], [-49.40, 69.12],
            [-49.40, 69.30], [-49.80, 69.30]]
    r = coverage.fit_to_coverage(tuple(geom.polygon_bbox(arch)), ["ICESSN"], polygon=arch)
    assert r["bbox"] is None and "split" in r["reason"], r


# ---- 5. the two UI surfaces stay in step ----------------------------------------------------------------------------
def test_every_app_adapter_call_is_a_registered_ui_tool():
    """Inside Claude Desktop every UI call is a tools/call of a ui_* tool; a name the server does not register fails
    only there, never in the browser dev loop."""
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "aicesat"
    called = set(re.findall(r"call\('(ui_\w+)'", (src / "ui" / "adapter.js").read_text()))
    registered = set(re.findall(r'@apps\.tool\(name="(ui_\w+)"', (src / "server.py").read_text()))
    assert {"ui_claims", "ui_fit"} <= called
    assert called <= registered, f"called but not registered: {sorted(called - registered)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_explore_build_gate():
    r = subprocess.run(["node", "tests/test_coverage_gate.js"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
