"""ASCII/Rich renderers for the TUI. Pure: every function takes plain data and returns a Rich renderable.

Nothing here imports an aicesat module, opens a file or touches the network — that is what lets the renderers be
tested without an index or a lake, and what keeps the expensive question ("what should I draw?") separate from the
cheap one ("how do I draw it?").
"""
from __future__ import annotations

import math

import h3
from rich.table import Table
from rich.text import Text

# Absent -> present-but-low -> present-and-high. `·` is not the bottom of the ramp, it is "no data at all": a hole in
# coverage and a cell holding one granule must not look alike.
# No blank in the ramp. With `" ░▒▓█"` a cell holding the LOWEST value rendered as a space, which on a map reads as
# a hole in coverage — the one thing the map exists to show. Every present value now gets ink.
EMPTY = "·"
RAMP = "░▒▓█"
RAMP_STYLES = ("cyan", "green", "yellow", "bright_red")


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_num(n: float) -> str:
    n = float(n or 0)
    for suffix, div in (("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:,.1f}{suffix}"
    return f"{n:,.0f}"


def _level(v: float, lo: float, hi: float) -> int:
    """Value -> index into RAMP. A flat field (hi == lo) renders at full strength rather than dividing by zero."""
    if hi <= lo:
        return len(RAMP) - 1
    f = (v - lo) / (hi - lo)
    return max(0, min(len(RAMP) - 1, int(f * (len(RAMP) - 1) + 0.5)))


def _glyph(v: float, lo: float, hi: float) -> Text:
    i = _level(v, lo, hi)
    return Text(RAMP[i], style=RAMP_STYLES[i])


def cell_map(values: dict[str, float], bbox, res: int, width: int = 72, height: int = 22,
             label: str = "", unit: str = "") -> Text:
    """A lon/lat character grid of `values`, keyed by H3 cell id, over `bbox` = (w, s, e, n).

    Shaded by LOOKUP, not by plotting cell centres: each character position asks which H3 cell contains it. That
    renders a hex as the area it actually covers, so a gap in coverage reads as a gap rather than as a sparser
    scatter of dots. A 72x22 grid is ~1,600 h3 calls — microseconds.
    """
    out = Text()
    if not values:
        return Text(f"{label}: no data over this box\n", style="grey37")
    w, s, e, n = (float(x) for x in bbox)
    lo, hi = min(values.values()), max(values.values())
    if label:
        out.append(f"{label}\n", style="bold")
    for row in range(height):
        # Row 0 is the TOP of the box, i.e. the northern edge; sample each cell's centre, not its corner.
        lat = n - (n - s) * (row + 0.5) / height
        out.append(f"{lat:7.3f} ", style="grey37")
        for col in range(width):
            lon = w + (e - w) * (col + 0.5) / width
            v = values.get(h3.latlng_to_cell(lat, lon, res))
            out.append(Text(EMPTY, style="grey30") if v is None else _glyph(v, lo, hi))
        out.append("\n")
    out.append(f"{'':7} {w:.2f}{'':^{max(0, width - 16)}}{e:.2f}\n", style="grey37")
    legend = Text(f"{'':8}{EMPTY} none  ", style="grey37")
    for i, ch in enumerate(RAMP):
        legend.append(ch, style=RAMP_STYLES[i])
    fmt = human_bytes if unit == "bytes" else human_num
    legend.append(f"  {fmt(lo)} → {fmt(hi)}{('' if unit == 'bytes' else ' ' + unit) if unit else ''}\n",
                  style="grey37")
    out.append(legend)
    return out


def time_strip(months: list[tuple[str, float]], width: int = 0, label: str = "") -> Text:
    """One glyph per month over the observed record, shaded by value. `months` is [(YYYY-MM, value)], any order.

    Every month between the first and last is drawn whether or not it has data, so gaps in the record are visible
    as gaps — a strip built only from the months that exist would silently close them up.
    """
    out = Text()
    if not months:
        return Text(f"{label}: no observations\n", style="grey37")
    have = {ym: v for ym, v in months}
    keys = sorted(have)
    (y0, m0), (y1, m1) = (int(keys[0][:4]), int(keys[0][5:7])), (int(keys[-1][:4]), int(keys[-1][5:7]))
    span = [(y0 + (m0 - 1 + i) // 12, (m0 - 1 + i) % 12 + 1) for i in range((y1 - y0) * 12 + (m1 - m0) + 1)]
    series = [have.get(f"{y:04d}-{m:02d}") for y, m in span]
    empty = sum(1 for v in series if v is None)
    # The record is often longer than the terminal is wide (92 months against ~70 usable columns), so fold whole
    # months into buckets rather than letting the strip wrap — a wrapped strip is unreadable as a timeline.
    cols, per = series, 1
    if width and len(series) > width:
        per = math.ceil(len(series) / width)
        cols = [None if all(v is None for v in series[i:i + per]) else sum(v or 0 for v in series[i:i + per])
                for i in range(0, len(series), per)]
    present = [v for v in cols if v is not None]
    lo, hi = min(present), max(present)
    if label:
        out.append(f"{label:<8}", style="bold")
    out.append(f"{y0}", style="grey37")
    out.append("┤", style="grey37")
    for v in cols:
        out.append(Text(EMPTY, style="grey30") if v is None else _glyph(v, lo, hi))
    out.append("├", style="grey37")
    out.append(f"{y1}  ", style="grey37")
    bucket = f", {per} months/col" if per > 1 else ""
    out.append(f"{len(span)} months, {empty} empty{bucket}\n", style="grey37")
    return out


def hist(values: list[float], bins: int = 24, width: int = 0, label: str = "", unit: str = "") -> Text:
    """A block histogram of `values` with p50/p99, one line. `bins` columns wide."""
    out = Text()
    vals = sorted(float(v) for v in values)
    if not vals:
        return Text(f"{label:<14} (empty)\n", style="grey37")
    lo, hi = vals[0], vals[-1]
    counts = [0] * bins
    for v in vals:
        counts[bins - 1 if hi <= lo else min(bins - 1, int((v - lo) / (hi - lo) * bins))] += 1
    cmax = max(counts)
    out.append(f"{label:<14}", style="bold")
    for c in counts:
        out.append(_glyph(c, 0, cmax) if c else Text(EMPTY, style="grey30"))
    p50 = vals[len(vals) // 2]
    p99 = vals[min(len(vals) - 1, int(len(vals) * 0.99))]
    fmt = human_bytes if unit == "bytes" else human_num
    out.append(f"  n {len(vals):,}  min {fmt(lo)}  p50 {fmt(p50)}  p99 {fmt(p99)}  max {fmt(hi)}\n", style="grey37")
    return out


def chunk_byte_strip(ranges: list[tuple[int, int]], spans: list[tuple[int, int]], width: int = 60,
                     title: str = "", file_size: int | None = None) -> Text:
    """The byte layout of ONE granule: which of its bytes a query needs, and what the coalescer will actually GET.

    `█` bytes a chunk needs, `▒` bytes pulled in only because coalescing merged across a small gap, `·` bytes
    skipped entirely. Coalescing is per URL — passing ranges from more than one granule here is meaningless, since
    their offsets index different files.
    """
    out = Text()
    if not ranges:
        return Text(f"{title}: no byte ranges\n", style="grey37")
    lo = min(o for o, _ in ranges)
    hi = max(o + s for o, s in ranges)
    step = max(1, math.ceil((hi - lo) / width))

    def _hits(col, rs):
        a, b = lo + col * step, lo + (col + 1) * step
        return any(o < b and (o + s) > a for o, s in rs)

    if title:
        out.append(f"{title}\n", style="bold")
    out.append(f" {human_bytes(lo):>11} ┤", style="grey37")
    for col in range(width):
        if _hits(col, ranges):
            out.append("█", style="bright_red")
        elif _hits(col, spans):
            out.append("▒", style="yellow")
        else:
            out.append(EMPTY, style="grey30")
    out.append(f"├ {human_bytes(hi)}\n", style="grey37")
    need = sum(s for _, s in ranges)
    got = sum(s for _, s in spans)
    over = (got - need) / need * 100 if need else 0.0
    tail = f" of a {human_bytes(file_size)} file" if file_size else ""
    out.append(f" {len(ranges)} ranges → {len(spans)} GETs · {human_bytes(got)} fetched "
               f"({human_bytes(need)} needed, {over:+.1f}% over-fetch) over a {human_bytes(hi - lo)} extent{tail}\n",
               style="grey37")
    return out


def chunk_cell_matrix(chunk_cells: dict, cells: list[int], max_rows: int = 24, title: str = "") -> Text:
    """Chunks down, H3 cells across, `█` where a chunk touches a cell.

    `chunk_cells` maps (granule, beam, chunk_index) -> set of cells. The band down the diagonal is the point: a
    granule sweeps through cells in along-track order, which is exactly why addressing by cell prunes so hard.
    """
    out = Text()
    if not chunk_cells or not cells:
        return Text(f"{title}: nothing to plot\n", style="grey37")
    cols = sorted(int(c) for c in cells)
    keys = sorted(chunk_cells, key=lambda k: (k[0], k[1], k[2]))
    shown = keys[:max_rows]
    if title:
        out.append(f"{title}\n", style="bold")
    for k in shown:
        touched = {int(c) for c in chunk_cells[k]}
        out.append(f" {k[1]} c{k[2]:<4} ", style="grey37")
        for c in cols:
            out.append(Text("█", style="cyan") if c in touched else Text(EMPTY, style="grey30"))
        out.append(f" {len(touched)}\n", style="grey37")
    out.append(f" {'':11}{'^' * len(cols)}  {len(cols)} cells\n", style="grey37")
    if len(keys) > max_rows:
        out.append(f" … {len(keys) - max_rows:,} more chunks not shown\n", style="grey37")
    return out


def timing_table(title: str, seconds: float, phases: list[tuple[str, float, str]]) -> Table:
    """The phase breakdown every command ends with. `phases` is [(name, seconds, detail)]."""
    t = Table(box=None, pad_edge=False, show_header=False)
    t.add_column(style="bold", no_wrap=True)
    t.add_column(justify="right", style="bright_cyan", no_wrap=True)
    t.add_column(style="grey62")
    t.add_row(title, f"{seconds:.2f} s", "")
    for name, sec, detail in phases:
        t.add_row(f"  {name}", f"{sec:.2f} s" if sec is not None else "", detail)
    return t
