"""The TUI's renderers are pure, so they are tested without an index, a lake or a terminal."""
import h3
import pytest

from aicesat.tui import render

BBOX = (-51.0, 69.0, -49.0, 69.4)


def _cells(bbox, res=5, n=4):
    """A handful of real H3 cells inside `bbox`, so the map has something to look up."""
    w, s, e, n_ = bbox
    out = []
    for i in range(n):
        lat = s + (n_ - s) * (i + 0.5) / n
        out.append(h3.latlng_to_cell(lat, (w + e) / 2, res))
    return list(dict.fromkeys(out))


def plain(renderable) -> str:
    return renderable.plain


# --- the ramp -------------------------------------------------------------------------------------------------

def test_ramp_has_no_blank():
    """A blank in the ramp made the lowest-value cell look like a hole in coverage — the one thing the map is for."""
    assert " " not in render.RAMP
    assert len(render.RAMP) == len(render.RAMP_STYLES)


def test_level_is_monotonic_and_survives_a_flat_field():
    assert render._level(0, 0, 10) < render._level(5, 0, 10) < render._level(10, 0, 10)
    assert render._level(7, 7, 7) == len(render.RAMP) - 1      # hi == lo must not divide by zero


# --- cell_map -------------------------------------------------------------------------------------------------

def test_cell_map_empty_says_so():
    assert "no data over this box" in plain(render.cell_map({}, BBOX, 5))


def test_cell_map_has_one_row_per_height_plus_axis_and_legend():
    cells = _cells(BBOX)
    out = plain(render.cell_map({c: 1 for c in cells}, BBOX, 5, width=20, height=6)).splitlines()
    assert len(out) == 6 + 2          # 6 data rows, then the lon axis and the legend


def test_cell_map_distinguishes_absent_from_lowest_value():
    """The regression the ramp fix was for: a present cell must always leave ink."""
    cells = _cells(BBOX)
    body = "".join(plain(render.cell_map({cells[0]: 1, cells[-1]: 999}, BBOX, 5, width=40, height=8)).splitlines()[:8])
    assert render.RAMP[0] in body                  # the low cell rendered as ink...
    assert render.RAMP[-1] in body                 # ...the high one too
    assert render.EMPTY in body                    # ...and the unlisted cells are visibly absent


def test_cell_map_north_is_the_top_row():
    rows = plain(render.cell_map({c: 1 for c in _cells(BBOX)}, BBOX, 5, width=10, height=4)).splitlines()
    lats = [float(r.split()[0]) for r in rows[:4]]
    assert lats == sorted(lats, reverse=True)
    assert BBOX[1] < lats[-1] and lats[0] < BBOX[3]


# --- time_strip -----------------------------------------------------------------------------------------------

def test_time_strip_empty():
    assert "no observations" in plain(render.time_strip([]))


def test_time_strip_keeps_gaps():
    """Months with no data are drawn, not closed up — a strip built only from the months that exist is a lie."""
    out = plain(render.time_strip([("2020-01", 5), ("2020-04", 5)]))
    assert "4 months, 2 empty" in out
    assert render.EMPTY in out


def test_time_strip_buckets_rather_than_wrapping():
    months = [(f"{2000 + i // 12:04d}-{i % 12 + 1:02d}", 1) for i in range(240)]
    out = plain(render.time_strip(months, width=40))
    body = out[out.index("┤") + 1: out.index("├")]
    assert len(body) <= 40
    assert "months/col" in out


def test_time_strip_single_month():
    assert "1 months, 0 empty" in plain(render.time_strip([("2020-01", 3)]))


# --- hist -----------------------------------------------------------------------------------------------------

def test_hist_empty():
    assert "(empty)" in plain(render.hist([], label="x"))


def test_hist_reports_percentiles_and_bins():
    out = plain(render.hist(list(range(100)), bins=10, label="x"))
    assert "n 100" in out and "p50" in out and "p99" in out


def test_hist_of_a_single_repeated_value_does_not_divide_by_zero():
    out = plain(render.hist([4, 4, 4], bins=8, label="x"))
    assert "n 3" in out and "min 4" in out and "max 4" in out


def test_hist_bytes_are_formatted_as_bytes():
    assert "MB" in plain(render.hist([2 << 20, 8 << 20], label="b", unit="bytes"))


# --- chunk_byte_strip -----------------------------------------------------------------------------------------

def test_byte_strip_empty():
    assert "no byte ranges" in plain(render.chunk_byte_strip([], []))


def test_byte_strip_contiguous_ranges_have_no_over_fetch():
    ranges = [(0, 1000), (1000, 1000)]
    out = plain(render.chunk_byte_strip(ranges, [(0, 2000)], width=20))
    assert "2 ranges → 1 GETs" in out
    assert "+0.0% over-fetch" in out


def test_byte_strip_shows_coalesced_bytes_as_over_fetch():
    """A gap the coalescer bridges is fetched but not needed; the strip must distinguish it from needed bytes."""
    ranges = [(0, 100), (900, 100)]
    out = plain(render.chunk_byte_strip(ranges, [(0, 1000)], width=40))
    assert "▒" in out and "█" in out
    assert "+400.0% over-fetch" in out


def test_byte_strip_single_range():
    out = plain(render.chunk_byte_strip([(10, 50)], [(10, 50)], width=12))
    assert "1 ranges → 1 GETs" in out
    assert "█" in out


# --- chunk_cell_matrix ----------------------------------------------------------------------------------------

def test_matrix_empty():
    assert "nothing to plot" in plain(render.chunk_cell_matrix({}, []))


def test_matrix_marks_only_touched_cells():
    cc = {("g.h5", "gt1l", 0): {10}, ("g.h5", "gt1l", 1): {11}}
    out = plain(render.chunk_cell_matrix(cc, [10, 11, 12]))
    assert out.count("█") == 2
    assert "3 cells" in out


def test_matrix_caps_rows_and_says_how_many_it_hid():
    cc = {("g.h5", "gt1l", i): {10} for i in range(50)}
    out = plain(render.chunk_cell_matrix(cc, [10], max_rows=5))
    assert "45 more chunks not shown" in out


# --- formatting -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("n,want", [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 << 20, "5.0 MB")])
def test_human_bytes(n, want):
    assert render.human_bytes(n) == want


@pytest.mark.parametrize("n,want", [(0, "0"), (999, "999"), (1500, "1.5k"), (2_500_000, "2.5M")])
def test_human_num(n, want):
    assert render.human_num(n) == want


def test_timing_table_lists_every_phase():
    t = render.timing_table("op", 1.25, [("a", 0.5, "detail"), ("b", None, "note")])
    assert t.row_count == 3      # the title row plus one per phase
