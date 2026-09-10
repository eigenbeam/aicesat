"""GEDI L2A addressing index — the parts that are decidable without a network.

GEDI breaks the assumption every other collection's index rests on: that all of a beam's datasets share one
chunking, so a single chunk_index addresses every one of them. Measured on a real V003 granule, one beam has
lat/lon at 2,608 shots per chunk, elev/sensitivity at 5,216, the quality flag at 10,432 and degrade_flag at
100,000 — a 38x spread. index_atl06 raises on exactly this. These pin the generalisation that replaces it.
"""
import numpy as np
import pytest

from aicesat import index_gedi


# --------------------------------------------------------------------------- granule names
def test_parse_granule_name_reads_the_acquisition_date():
    # GEDI02_A_YYYYDDDHHMMSS_Oorbit_sub_Ttrack_ppds_rrr_VVV.h5
    info = index_gedi.parse_granule_name("GEDI02_A_2019118212421_O02128_02_T01324_02_004_02_V003.h5")
    assert info["gdate"] == "20190428"          # 2019 day-of-year 118
    assert info["orbit"] == 2128
    assert info["track"] == 1324
    assert info["version"] == 3


def test_parse_granule_name_handles_a_leap_year_day_number():
    assert index_gedi.parse_granule_name(
        "GEDI02_A_2020366000000_O11000_01_T00001_02_003_01_V002.h5")["gdate"] == "20201231"


def test_parse_granule_name_rejects_a_foreign_name():
    with pytest.raises(ValueError):
        index_gedi.parse_granule_name("ATL06_20181016214403_02790106_007_01.h5")


# --------------------------------------------------------------------------- chunk-span segmentation
def test_spans_lie_inside_exactly_one_chunk_of_every_dataset():
    """The invariant the whole design rests on: cut the beam at the UNION of every dataset's chunk boundaries, and
    each resulting span sits inside a single chunk of each dataset -- so one flat row can carry one byte range per
    dataset, with no 'this span straddles two chunks' special case."""
    sizes = {"latlon": 2608, "elev": 5216, "qual": 10432, "degrade": 100000}
    n = 166900
    spans = index_gedi.chunk_spans(sizes, n)
    assert spans, "no spans produced"
    for i0, i1 in spans:
        assert i0 < i1
        for c in sizes.values():
            assert i0 // c == (i1 - 1) // c, f"span [{i0},{i1}) straddles a {c}-chunk boundary"


def test_spans_tile_the_beam_exactly():
    sizes = {"latlon": 2608, "elev": 5216, "qual": 10432, "degrade": 100000}
    n = 166900
    spans = index_gedi.chunk_spans(sizes, n)
    assert spans[0][0] == 0 and spans[-1][1] == n
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a1 == b0, "spans must be contiguous with no gap and no overlap"


def test_spans_collapse_to_the_chunking_when_every_dataset_agrees():
    """The uniform case (what ATL06 has) must not be made worse: identical chunking gives exactly the chunks."""
    spans = index_gedi.chunk_spans({"a": 1000, "b": 1000, "c": 1000}, 3500)
    assert spans == [(0, 1000), (1000, 2000), (2000, 3000), (3000, 3500)]


def test_spans_handle_a_beam_shorter_than_one_chunk():
    assert index_gedi.chunk_spans({"a": 2608, "b": 100000}, 42) == [(0, 42)]


def test_spans_are_not_absurdly_many():
    """Cost guard: one row per span per cell, so the segmentation must stay near the finest chunking, not the
    product of all of them. 166,900 shots at 2,608 is 64 chunks; the misaligned 100,000 adds at most a couple."""
    spans = index_gedi.chunk_spans({"latlon": 2608, "elev": 5216, "qual": 10432, "degrade": 100000}, 166900)
    assert 64 <= len(spans) <= 70, len(spans)


def test_chunk_of_maps_a_span_to_the_right_chunk_index():
    assert index_gedi.chunk_of((0, 2608), 2608) == 0
    assert index_gedi.chunk_of((2608, 5216), 2608) == 1
    assert index_gedi.chunk_of((2608, 5216), 100000) == 0
    assert index_gedi.chunk_of((100000, 102608), 100000) == 1


# --------------------------------------------------------------------------- version handling
def test_quality_field_resolves_across_versions():
    """V003 renamed quality_flag to l2a_quality_flag_rel3. Keying on the V002 name alone silently drops every V003
    granule -- which over the Langtang box is 51 of 98, and biases what survives toward 2019."""
    assert index_gedi.quality_field({"quality_flag", "degrade_flag"}) == "quality_flag"
    assert index_gedi.quality_field({"l2a_quality_flag_rel3", "degrade_flag"}) == "l2a_quality_flag_rel3"
    # both present -> prefer the current release
    assert index_gedi.quality_field({"quality_flag", "l2a_quality_flag_rel3"}) == "l2a_quality_flag_rel3"
    with pytest.raises(KeyError):
        index_gedi.quality_field({"degrade_flag"})


def test_beams_are_all_eight_and_power_is_labelled():
    assert len(index_gedi.BEAMS) == 8
    assert set(index_gedi.FULL_POWER) < set(index_gedi.BEAMS)
    assert len(index_gedi.FULL_POWER) == 4          # 4 full-power, 4 coverage


# --------------------------------------------------------------------------- span -> chunk slicing
def test_span_offset_locates_the_span_inside_its_chunk():
    """Every dataset's chunk is WIDER than the span, so the decoded chunk must be sliced back to the span. A wrong
    offset does not raise -- it pairs a latitude with a different shot's elevation."""
    assert index_gedi.span_offset(0, 2608) == 0
    assert index_gedi.span_offset(2608, 2608) == 0          # span starts exactly on a chunk boundary
    assert index_gedi.span_offset(2608, 5216) == 2608       # ... but is mid-chunk in a coarser dataset
    assert index_gedi.span_offset(2608, 10432) == 2608
    assert index_gedi.span_offset(100000, 100000) == 0
    assert index_gedi.span_offset(102608, 100000) == 2608


def test_span_offset_plus_length_never_runs_past_the_chunk():
    """The slice [off, off+len) has to stay inside the chunk, or numpy silently returns a short array."""
    sizes = {"latlon": 2608, "elev": 5216, "qual": 10432, "degrade": 100000}
    for i0, i1 in index_gedi.chunk_spans(sizes, 166900):
        for c in sizes.values():
            off = index_gedi.span_offset(i0, c)
            assert off + (i1 - i0) <= c, f"span [{i0},{i1}) overruns a {c}-chunk (offset {off})"


def test_span_offset_is_zero_for_a_degenerate_chunk_size():
    assert index_gedi.span_offset(1234, 0) == 0


# --------------------------------------------------------------------------- plan_bbox (offline)
DS = index_gedi.GEDI_KEYS


def _row(granule, beam, span, cell, off=0, seg=(0, 10)):
    r = {"granule": granule, "beam": beam, "span_index": span, "h3_cell": cell, "gdate": "20190428",
         "url": f"https://x/{granule}", "s3url": f"s3://b/{granule}",
         "seg_start": seg[0], "seg_end": seg[1]}
    for i, k in enumerate(DS):
        r.update({f"{k}_offset": off + i * 100, f"{k}_size": 100, f"{k}_dtype": "f8",
                  f"{k}_filters": "", f"{k}_mask": 0, f"{k}_fill": float("nan")})
    return r


@pytest.fixture
def rows():
    # one granule, one beam, two spans; span 0 touches cells 10 and 11, span 1 touches cell 11 only
    return [_row("g.h5", "BEAM0101", 0, 10), _row("g.h5", "BEAM0101", 0, 11),
            _row("g.h5", "BEAM0101", 1, 11, off=1000)]


def _patch(monkeypatch, rows, have):
    monkeypatch.setattr(index_gedi, "_index_rows", lambda *a, **k: ([10, 11], rows))
    from aicesat import access, lake
    monkeypatch.setattr(lake, "drain_writes", lambda *a, **k: True)
    monkeypatch.setattr(lake, "ingested_chunk_cells", lambda *a, **k: have)
    monkeypatch.setattr(access, "access_url", lambda url, s3: url)


def test_plan_marks_nothing_todo_when_every_cell_is_materialized(monkeypatch, rows):
    _patch(monkeypatch, rows, {("g.h5", "BEAM0101", 0, 10), ("g.h5", "BEAM0101", 0, 11),
                               ("g.h5", "BEAM0101", 1, 11)})
    p = index_gedi.plan_bbox((0, 0, 1, 1))
    assert p["todo"] == [] and p["n_lake"] == 2 and p["by_url"] == {}


def test_plan_refetches_a_span_when_ANY_of_its_cells_is_missing(monkeypatch, rows):
    """The cell-aware skip: a partial eviction pulls the whole span back rather than being silently accepted."""
    _patch(monkeypatch, rows, {("g.h5", "BEAM0101", 0, 10), ("g.h5", "BEAM0101", 1, 11)})
    p = index_gedi.plan_bbox((0, 0, 1, 1))
    assert p["todo"] == [("g.h5", "BEAM0101", 0)] and p["n_lake"] == 1


def test_plan_force_ignores_what_is_already_materialized(monkeypatch, rows):
    _patch(monkeypatch, rows, {("g.h5", "BEAM0101", 0, 10), ("g.h5", "BEAM0101", 0, 11),
                               ("g.h5", "BEAM0101", 1, 11)})
    p = index_gedi.plan_bbox((0, 0, 1, 1), force=True)
    assert len(p["todo"]) == 2 and p["n_lake"] == 0 and p["have"] == set()


def test_plan_groups_todo_by_url_never_across_granules(monkeypatch):
    rows = [_row("a.h5", "BEAM0101", 0, 10), _row("b.h5", "BEAM0110", 0, 10)]
    _patch(monkeypatch, rows, set())
    p = index_gedi.plan_bbox((0, 0, 1, 1))
    assert set(p["by_url"]) == {"https://x/a.h5", "https://x/b.h5"}
    assert all(len(v) == 1 for v in p["by_url"].values()), "byte ranges must never coalesce across granules"


def test_an_unbuilt_index_is_an_error_not_an_empty_answer(monkeypatch, tmp_path):
    """Index-only means a missing index is loud. Returning nothing would look like 'no GEDI here'."""
    monkeypatch.setattr(index_gedi, "GEDI_INDEX_DIR", tmp_path / "nope")
    with pytest.raises(RuntimeError, match="no GEDI index"):
        index_gedi._index_rows((0, 0, 1, 1), None, index_gedi.GEDI_RES)
