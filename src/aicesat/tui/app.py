"""The REPL: command parsing, dispatch, and rendering.

Rich has no event loop, so this is a prompt loop rather than a full-screen app — which also means it behaves
identically over ssh onto the deployed box, where most of the interesting measurements live.
"""
from __future__ import annotations

import logging
import shlex
import time

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from . import ops, render

log = logging.getLogger(__name__)

BANNER = """[bold]aicesat[/bold] — ATL06 index & lake explorer
Type [bold cyan]help[/bold cyan] for commands, [bold cyan]quit[/bold cyan] to leave."""

HELP = [
    ("region [name]", "show or set the working region (bbox comes from it)"),
    ("bbox W S E N", "set the working bbox directly"),
    ("window [A B|none]", "set or clear the time window (YYYY-MM-DD)"),
    ("", ""),
    ("build [--workers N]", "build the index over the working bbox — resumable, hits NASA"),
    ("index [--full]", "index overview: map, time strip, histograms (--full maps the claim, not the box)"),
    ("index cells [--top N] [--sort g|e|sp]", "per-cell index table"),
    ("index cell <h3>", "one cell in detail"),
    ("index chunks", "per-cell chunk counts, read from the index parquets"),
    ("index verify", "count the index by schema version — the readdir count cannot see stale files"),
    ("", ""),
    ("plan", "dry run: what a query would read from the lake vs fetch from NASA (no network)"),
    ("chunks [--granule G] [--beam B]", "byte-range map of one granule + chunk×cell occupancy"),
    ("query [--force]", "read points over the box, lake first, NASA for the rest"),
    ("mat <h3> [<h3>...]", "materialize cells into the lake"),
    ("", ""),
    ("lake [--rows]", "lake overview: map, histograms (--rows reads every Parquet footer — slow)"),
    ("lake cells [--top N] [--rows]", "per-cell lake table"),
    ("", ""),
    ("log [n]", "tail the library log buffer"),
    ("time", "timings for this session"),
    ("quit", "exit"),
]


def _flags(tokens: list[str], valued: tuple[str, ...] = (), n_valued: tuple[str, ...] = ()) -> tuple[list, dict]:
    """Split `--flag` / `--key value` out of a token list. Returns (positional, flags)."""
    pos, out, i = [], {}, 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:]
            if key in valued or key in n_valued:
                if i + 1 >= len(tokens):
                    raise ValueError(f"--{key} needs a value")
                out[key] = int(tokens[i + 1]) if key in n_valued else tokens[i + 1]
                i += 2
                continue
            out[key] = True
        else:
            pos.append(tok)
        i += 1
    return pos, out


class FetchCounter:
    """Turns fetch_bbox's callbacks into a progress denominator and a count.

    `on_plan` fires exactly once, before any network, and is the only thing on this path that knows how much work
    there is. `on_granule` fires per lake batch (`granule == "lake"`) as well as per fetched granule, and a granule
    can stream more than once, so the count is of DISTINCT fetched names — the same unit the web UI counts. Counting
    every callback would run the bar past its own total.
    """

    def __init__(self, label: str):
        self.label = label
        self.total = 0
        self.cached = 0
        self.chunks = 0
        self.seen: set[str] = set()
        self.planned = False

    def plan(self, p: dict) -> None:
        self.planned = True
        self.total = int(p.get("granules") or 0)
        self.cached = int(p.get("cached") or 0)
        self.chunks = int(p.get("chunks") or 0)

    def granule(self, pts: dict) -> bool:
        """-> True when this callback advanced the count."""
        g = pts.get("granule")
        if not g or g == "lake" or g in self.seen:
            return False
        self.seen.add(g)
        return True

    @property
    def done(self) -> int:
        return len(self.seen)

    @property
    def description(self) -> str:
        if not self.planned:
            return f"{self.label}: planning"
        if not self.total:
            return f"{self.label}: reading lake ({self.cached:,} chunks cached)"
        return f"{self.label}: {self.total:,} granules · {self.chunks:,} chunks to fetch"


class App:
    def __init__(self, console: Console, region: str, bbox, res: int = 5):
        self.c = console
        self.region = region
        self.bbox = tuple(float(x) for x in bbox)
        self.res = res
        self.window = None
        self.history: list[tuple[str, object]] = []

    # ---------- plumbing ----------

    def run(self) -> int:
        self.c.print(BANNER)
        self.c.print(f"region [bold]{self.region}[/bold]  bbox {self._bbox_str()}  res {self.res}\n")
        while True:
            try:
                line = self.c.input("[bold green]aicesat[/bold green]> ").strip()
            except EOFError:
                self.c.print()
                return 0
            except KeyboardInterrupt:
                self.c.print()
                continue
            if not line:
                continue
            if line in ("quit", "exit", "q"):
                return 0
            try:
                self.dispatch(line)
            except KeyboardInterrupt:
                self.c.print("[yellow]interrupted[/yellow] — partial work is on disk; re-run to resume")
            except Exception as e:
                self.c.print(f"[bold red]{type(e).__name__}[/bold red]: {e}")
                log.debug("command failed", exc_info=True)

    def dispatch(self, line: str) -> None:
        tokens = shlex.split(line)
        verb, rest = tokens[0], tokens[1:]
        sub = rest[0] if rest and not rest[0].startswith("--") else None
        for name in (f"cmd_{verb}_{sub}" if sub else None, f"cmd_{verb}"):
            if name and hasattr(self, name):
                fn = getattr(self, name)
                return fn(rest[1:] if name.endswith(f"_{sub}") and sub else rest)
        self.c.print(f"[red]unknown command[/red] {verb!r} — try [cyan]help[/cyan]")

    def _bbox_str(self) -> str:
        return " ".join(f"{v:g}" for v in self.bbox)

    def _record(self, t) -> None:
        self.history.append((time.strftime("%H:%M:%S"), t))
        self.c.print(render.timing_table(t.title, t.seconds, t.rows()))

    def _width(self, margin: int = 14) -> int:
        return max(20, min(96, self.c.width - margin))

    # ---------- state ----------

    def cmd_help(self, args) -> None:
        t = Table(box=None, pad_edge=False, show_header=False)
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column(style="grey62")
        for name, desc in HELP:
            t.add_row(Text(name), Text(desc))   # Text(), not markup: "log [n]" would parse [n] as a style tag
        self.c.print(t)

    def cmd_region(self, args) -> None:
        from .. import regions

        if not args:
            t = Table(box=None, show_header=True, header_style="bold")
            t.add_column("region"); t.add_column("bbox"); t.add_column("note", style="grey62")
            for name, doc in regions.REGIONS.items():
                mark = " [green]*[/green]" if name == self.region else ""
                t.add_row(name + mark, " ".join(f"{v:g}" for v in doc["bbox"]), doc["note"].split(".")[0])
            self.c.print(t)
            return
        name = args[0]
        if name not in regions.REGIONS:
            raise ValueError(f"unknown region {name!r} — one of {', '.join(regions.REGIONS)}")
        self.region, self.bbox = name, regions.resolve_bbox(name)
        self.c.print(f"region [bold]{name}[/bold]  bbox {self._bbox_str()}")

    def cmd_bbox(self, args) -> None:
        if not args:
            self.c.print(f"bbox {self._bbox_str()}  (region {self.region})")
            return
        if len(args) != 4:
            raise ValueError("bbox needs four numbers: W S E N")
        from .. import regions

        self.bbox = regions.resolve_bbox(None, tuple(float(x) for x in args))
        self.region = "(custom)"
        self.c.print(f"bbox {self._bbox_str()}")

    def cmd_window(self, args) -> None:
        if not args:
            self.c.print(f"window {self.window or 'none (full record)'}")
            return
        if args[0] == "none":
            self.window = None
        elif len(args) == 2:
            self.window = (args[0], args[1])
        else:
            raise ValueError("window needs two dates (YYYY-MM-DD YYYY-MM-DD) or 'none'")
        self.c.print(f"window {self.window or 'none (full record)'}")

    def cmd_time(self, args) -> None:
        if not self.history:
            self.c.print("[grey62]nothing timed yet[/grey62]")
            return
        t = Table(box=None, show_header=True, header_style="bold")
        t.add_column("at", style="grey62"); t.add_column("operation"); t.add_column("wall", justify="right",
                                                                                   style="bright_cyan")
        for at, tm in self.history:
            t.add_row(at, tm.title, f"{tm.seconds:.2f} s")
        self.c.print(t)

    def cmd_log(self, args) -> None:
        from .. import logbuf

        n = int(args[0]) if args else 20
        ents = logbuf.entries(0)["entries"][-n:]
        if not ents:
            self.c.print("[grey62]log buffer empty[/grey62]")
            return
        t = Table(box=None, show_header=False, pad_edge=False)
        t.add_column(style="grey62", no_wrap=True); t.add_column(no_wrap=True); t.add_column()
        for e in ents:
            style = {"WARNING": "yellow", "ERROR": "red"}.get(e["level"], "grey62")
            t.add_row(e["t"][11:19] if len(e.get("t", "")) > 19 else "", Text(e["name"], style=style), e["msg"])
        self.c.print(t)

    # ---------- index ----------

    def cmd_index(self, args) -> None:
        _, fl = _flags(args)
        data, t = ops.index_overview(self.res)
        st, build = data["status"], data["build"]
        head = Table(box=None, show_header=False, pad_edge=False)
        head.add_column(style="grey62", no_wrap=True); head.add_column()
        head.add_row("collection", f"[bold]{st['collection']}[/bold]  res {st['res']}")
        target = st.get("target")
        head.add_row("granules", f"{st['granules']:,} of {target:,} ({st.get('pct', 0)}% built)" if target
                     else f"{st['granules']:,} [grey62](no claim to compare against)[/grey62]")
        head.add_row("cells", f"{len(st['cells']):,} at res {st['res']}")
        head.add_row("claim", f"{build['claim_cells']:,} cells at res {build.get('coverage_res')}"
                              f"  ·  span {st.get('span_max')} yr" if build.get("claim_cells")
                     else "[grey62]none stamped[/grey62]")
        bounds = build.get("bounds")
        if bounds:
            head.add_row("claim bounds", " ".join(f"{v:.3f}" for v in bounds))
        head.add_row("dir", f"[grey62]{data['index_dir']}[/grey62]")
        self.c.print(Panel(head, title="index", title_align="left", border_style="grey37"))
        if not st["cells"]:
            self.c.print(f"[yellow]no {st['collection']} index here yet[/yellow] — "
                         f"run [cyan]build[/cyan] over the working bbox to create one.")
            return self._record(t)

        area = self.bbox
        if fl.get("full") and bounds:
            area = tuple(bounds)
        elif bounds and not self._overlaps(self.bbox, bounds):
            self.c.print(f"[yellow]note[/yellow] the working bbox lies outside the claim "
                         f"({' '.join(f'{v:.2f}' for v in bounds)}) — nothing will show. "
                         f"Try [cyan]index --full[/cyan] or a region inside it.")
        vals = {c["h"]: c["g"] for c in st["cells"]}
        self.c.print(render.cell_map(vals, area, st["res"], width=self._width(), height=16,
                                     label=f"granules per cell over {' '.join(f'{v:g}' for v in area)}",
                                     unit="granules"))
        self.c.print(render.time_strip(data["months"], width=self._width(24), label=st["collection"]))
        self.c.print(render.hist([c["g"] for c in st["cells"]], label="granules/cell"))
        self.c.print(render.hist([c["e"] for c in st["cells"]], label="epochs/cell"))
        self.c.print(render.hist([c["sp"] for c in st["cells"]], label="span (yr)"))
        self._record(t)

    @staticmethod
    def _overlaps(a, b) -> bool:
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    def cmd_index_cells(self, args) -> None:
        _, fl = _flags(args, n_valued=("top",))
        data, t = ops.index_overview(self.res)
        cells = data["status"]["cells"]
        key = {"g": "g", "e": "e", "sp": "sp"}.get(str(fl.get("sort", "g")), "g")
        t.title = "index cells"
        cells = sorted(cells, key=lambda c: c[key], reverse=True)[: fl.get("top", 20)]
        tb = Table(box=None, show_header=True, header_style="bold")
        tb.add_column("h3 cell"); tb.add_column("granules", justify="right"); tb.add_column("epochs", justify="right")
        tb.add_column("span yr", justify="right"); tb.add_column("first", justify="right")
        tb.add_column("last", justify="right")
        for c in cells:
            tb.add_row(c["h"], f"{c['g']:,}", f"{c['e']:,}", f"{c['sp']:g}", str(c["y0"]), str(c["y1"]))
        self.c.print(tb)
        self._record(t)

    def cmd_index_cell(self, args) -> None:
        if not args:
            raise ValueError("index cell needs an h3 cell id")
        want = args[0]
        data, t = ops.index_overview(self.res)
        t.title = f"index cell {want}"
        hit = next((c for c in data["status"]["cells"] if c["h"] == want), None)
        if hit is None:
            raise ValueError(f"cell {want!r} is not in the {data['status']['collection']} index")
        tb = Table(box=None, show_header=False, pad_edge=False)
        tb.add_column(style="grey62"); tb.add_column()
        import h3
        lat, lon = h3.cell_to_latlng(want)
        tb.add_row("cell", f"[bold]{want}[/bold]  (int {h3.str_to_int(want)})")
        tb.add_row("centre", f"{lat:.4f} N  {lon:.4f} E")
        tb.add_row("granules", f"{hit['g']:,}")
        tb.add_row("epochs", f"{hit['e']:,}")
        tb.add_row("span", f"{hit['sp']:g} yr  ({hit['y0']} → {hit['y1']})")
        self.c.print(Panel(tb, title="index cell", title_align="left", border_style="grey37"))
        self._record(t)

    def cmd_index_chunks(self, args) -> None:
        _, fl = _flags(args, n_valued=("top",))
        rows, t = ops.index_chunk_rollup(self.res)
        tb = Table(box=None, show_header=True, header_style="bold")
        tb.add_column("h3 cell"); tb.add_column("chunks", justify="right"); tb.add_column("granules", justify="right")
        tb.add_column("beams", justify="right")
        for r in rows[: fl.get("top", 20)]:
            tb.add_row(str(r["cell"]), f"{r['chunks']:,}", f"{r['granules']:,}", str(r["beams"]))
        self.c.print(tb)
        self.c.print(render.hist([r["chunks"] for r in rows], label="chunks/cell"))
        self._record(t)

    def cmd_index_verify(self, args) -> None:
        out, t = ops.index_verify(self.res)
        tb = Table(box=None, show_header=False, pad_edge=False)
        tb.add_column(style="grey62"); tb.add_column()
        tb.add_row("files on disk", f"{out['files']:,}")
        tb.add_row(f"at schema v{out['want']}", f"[green]{out['current']:,}[/green]")
        tb.add_row("stale schema", f"[yellow]{out['stale']:,}[/yellow]" if out["stale"] else "0")
        tb.add_row("unreadable", f"[red]{out['unreadable']:,}[/red]" if out["unreadable"] else "0")
        tb.add_row("zero-row", f"[yellow]{out['empty']:,}[/yellow]" if out["empty"] else "0")
        self.c.print(Panel(tb, title="index verify", title_align="left", border_style="grey37"))
        if out["stale"] or out["unreadable"]:
            self.c.print(f"[yellow]{out['stale'] + out['unreadable']:,} file(s) do not count as built[/yellow] — "
                         f"[cyan]index[/cyan] reports the readdir count, which cannot see this. The next "
                         f"[cyan]build[/cyan] deletes them, drops the claim, and re-indexes those granules.")
        if out["empty"]:
            self.c.print("[grey62]zero-row files at the current schema — not necessarily a fault: a granule can "
                         "genuinely miss the cells a build asked for. It IS a fault when it was caused by a wrong "
                         "cell filter, because a re-run then skips those granules and the index stays empty there. "
                         "`uv run scripts/check_index.py` tells the two apart.[/grey62]")
        self._record(t)

    # ---------- plan / chunks / query ----------

    def cmd_plan(self, args) -> None:
        _, fl = _flags(args)
        pl, t = ops.plan(self.bbox, self.window, self.res, force=bool(fl.get("force")))
        tb = Table(box=None, show_header=False, pad_edge=False)
        tb.add_column(style="grey62"); tb.add_column()
        tb.add_row("box", f"{self._bbox_str()}   window {self.window or 'full record'}")
        tb.add_row("cells", f"{len(pl['want_cells'])} at res {self.res}")
        tb.add_row("index rows", f"{len(pl['rows']):,} refs over {len(pl['names']):,} granules")
        tb.add_row("chunks", f"{len(pl['chunk_cells']):,} total")
        tb.add_row("from lake", f"[green]{pl['n_lake']:,}[/green] already materialized")
        tb.add_row("to fetch", f"[yellow]{len(pl['todo']):,}[/yellow] chunks across "
                               f"{len(pl['by_url']):,} granules")
        self.c.print(Panel(tb, title="plan (no network)", title_align="left", border_style="grey37"))
        self._record(t)

    def cmd_chunks(self, args) -> None:
        _, fl = _flags(args, valued=("granule", "beam"))
        data, t = ops.chunk_map(self.bbox, self.window, self.res, fl.get("granule"), fl.get("beam"))
        if data["granule"] is None:
            self.c.print("[yellow]no index rows over this box[/yellow]")
            return self._record(t)
        pl = data["plan"]
        self.c.print(render.chunk_byte_strip(
            data["ranges"], data["spans"], width=self._width(38),
            title=f"{data['granule']} · {data['beam']} · {data['chunks']} chunk(s) of "
                  f"{len(pl['chunk_cells']):,} over this box"))
        self.c.print()
        self.c.print(render.chunk_cell_matrix(
            data["chunk_cells"], pl["want_cells"], max_rows=16,
            title=f"chunk × cell occupancy · {data['granule']}"))
        self._record(t)

    def cmd_query(self, args) -> None:
        _, fl = _flags(args)
        with self._fetch_progress("query") as cbs:
            data, t = ops.query(self.bbox, self.window, self.res, force=bool(fl.get("force")), **cbs)
        st = data["stats"]
        tb = Table(box=None, show_header=False, pad_edge=False)
        tb.add_column(style="grey62"); tb.add_column()
        tb.add_row("points", f"[bold]{data['points']:,}[/bold]")
        tb.add_row("chunks", f"[green]{st.get('chunks_from_lake', 0):,}[/green] from lake · "
                             f"[yellow]{st.get('chunks_from_nasa', 0):,}[/yellow] from nasa")
        if st.get("requests"):
            tb.add_row("network", f"{st['requests']:,} GETs · {render.human_bytes(st.get('bytes', 0))}"
                                  f" · {st.get('presigns', 0)} presigns")
        self.c.print(Panel(tb, title="query", title_align="left", border_style="grey37"))
        self._record(t)

    def cmd_mat(self, args) -> None:
        pos, _ = _flags(args)
        if not pos:
            raise ValueError("mat needs one or more h3 cell ids (see `index cells`)")
        with self._fetch_progress("materialize") as cbs:
            data, t = ops.materialize(pos, self.window, self.res, **cbs)
        tb = Table(box=None, show_header=False, pad_edge=False)
        tb.add_column(style="grey62"); tb.add_column()
        tb.add_row("requested", ", ".join(str(c) for c in data["requested"]))
        tb.add_row("bbox used", " ".join(f"{v:.4f}" for v in data["bbox"]))
        tb.add_row("points", f"{data['points']:,}")
        tb.add_row("new lake cells", f"{len(data['gained'])}"
                                     + (f" — {', '.join(str(c) for c in data['gained'][:6])}" if data["gained"] else ""))
        self.c.print(Panel(tb, title="materialize", title_align="left", border_style="grey37"))
        if len(data["gained"]) > 1:
            self.c.print("[grey62]A hexagon's bounding box overlaps its neighbours, so neighbouring cells are "
                         "fetched too — that is why more cells landed than were asked for.[/grey62]")
        self._record(t)

    # ---------- lake ----------

    def cmd_lake(self, args) -> None:
        _, fl = _flags(args)
        with_rows = bool(fl.get("rows"))
        if with_rows:
            self.c.print("[yellow]--rows reads every Parquet footer in the mission[/yellow] "
                         "[grey62](~90 s over 23k files locally). Without it, row counts read 0.[/grey62]")
        data, t = ops.lake_overview(with_rows)
        cells = data["cells"]
        from .. import lake as lake_mod

        settings = lake_mod.get_settings()
        n_bytes = sum(c["bytes"] for c in cells.values())
        gran = {g for c in cells.values() for g in (c.get("granules") or [])}
        tb = Table(box=None, show_header=False, pad_edge=False)
        tb.add_column(style="grey62"); tb.add_column()
        tb.add_row("mission", f"[bold]{ops.MISSION}[/bold]  ({len(cells)} cells at res {self.res})")
        tb.add_row("size", f"{render.human_bytes(n_bytes)} of "
                           f"{render.human_bytes(settings.get('max_bytes', 0))} "
                           f"({100 * n_bytes / max(1, settings.get('max_bytes', 1)):.1f}%)")
        tb.add_row("files", f"{sum(c['files'] for c in cells.values()):,}")
        tb.add_row("rows", f"{sum(c['rows'] for c in cells.values()):,}" if with_rows
                   else "[grey62]not read (--rows)[/grey62]")
        tb.add_row("granules", f"{len(gran):,}")
        tb.add_row("all missions", "  ".join(f"{m['mission']} {m['cells']}c/{render.human_bytes(m['bytes'])}"
                                             for m in data["missions"]))
        self.c.print(Panel(tb, title="lake", title_align="left", border_style="grey37"))
        import h3
        vals = {h3.int_to_str(int(c)): v["bytes"] for c, v in cells.items()}
        self.c.print(render.cell_map(vals, self.bbox, self.res, width=self._width(), height=14,
                                     label="lake bytes per cell", unit="bytes"))
        self.c.print(render.hist([c["files"] for c in cells.values()], label="files/cell"))
        self.c.print(render.hist([c["bytes"] for c in cells.values()], label="bytes/cell", unit="bytes"))
        if with_rows:
            self.c.print(render.hist([c["rows"] for c in cells.values()], label="rows/cell"))
        self._record(t)

    def cmd_lake_cells(self, args) -> None:
        _, fl = _flags(args, n_valued=("top",))
        with_rows = bool(fl.get("rows"))
        if with_rows:
            self.c.print("[yellow]--rows reads every Parquet footer in the mission[/yellow] "
                         "[grey62](~90 s over 23k files locally).[/grey62]")
        data, t = ops.lake_overview(with_rows)
        t.title = "lake cells"
        cells = sorted(data["cells"].values(), key=lambda c: c["bytes"], reverse=True)[: fl.get("top", 20)]
        tb = Table(box=None, show_header=True, header_style="bold")
        tb.add_column("h3 cell"); tb.add_column("files", justify="right"); tb.add_column("bytes", justify="right")
        tb.add_column("rows", justify="right"); tb.add_column("granules", justify="right")
        tb.add_column("chunks", justify="right"); tb.add_column("age", justify="right", style="grey62")
        for c in cells:
            tb.add_row(str(c["cell"]), f"{c['files']:,}", render.human_bytes(c["bytes"]),
                       f"{c['rows']:,}" if with_rows else "—", f"{len(c.get('granules') or []):,}",
                       f"{c.get('chunks', 0):,}", f"{(c.get('age_s') or 0) / 3600:.1f} h")
        self.c.print(tb)
        self._record(t)

    # ---------- build ----------

    def cmd_build(self, args) -> None:
        _, fl = _flags(args, n_valued=("workers",))
        workers = fl.get("workers", 8)
        self.c.print(f"building [bold]{ops.COLLECTION}[/bold] over {self._bbox_str()} "
                     f"(res {self.res}, {workers} workers) — this hits NASA")
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(),
                      MofNCompleteColumn(), TimeElapsedColumn(), console=self.c, transient=True) as prog:
            task = prog.add_task("planning", total=None)
            state = {"ok": 0, "err": 0, "rows": 0}

            def on_plan(p):
                prog.update(task, total=p["todo"], completed=0,
                            description=f"{p['already_indexed']:,} indexed · {p['todo']:,} to build")

            def on_granule(name, nrows, err):
                state["err" if err else "ok"] += 1
                state["rows"] += nrows
                prog.update(task, advance=1,
                            description=f"{state['ok']} ok · {state['err']} err · {state['rows']:,} rows")

            out, t = ops.build(self.bbox, self.res, workers, self.window, on_plan, on_granule)
        tb = Table(box=None, show_header=False, pad_edge=False)
        tb.add_column(style="grey62"); tb.add_column()
        tb.add_row("granules", f"{out['granules']:,} found · {out['already_indexed']:,} already indexed · "
                               f"{out['todo']:,} to build")
        tb.add_row("built", f"[green]{out['ok']}[/green] ok · "
                            f"{'[red]' if out['err'] else ''}{out['err']}{'[/red]' if out['err'] else ''} err · "
                            f"{out['rows']:,} rows")
        tb.add_row("claim", "[green]stamped[/green]" if out["claimed"]
                   else "[yellow]WITHHELD[/yellow] — re-run to finish")
        if out["timed_out"]:
            tb.add_row("", "[red]timed out[/red] — finished granules are on disk; re-run to resume")
        self.c.print(Panel(tb, title="build", title_align="left", border_style="grey37"))
        self._record(t)

    # ---------- shared progress ----------

    def _fetch_progress(self, label: str):
        """A Rich bar over fetch_bbox's on_plan / on_granule. The counting lives in FetchCounter so it can be
        tested without a terminal — a transient Progress erases its own output, which makes it untestable."""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            ctr = FetchCounter(label)
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(),
                          MofNCompleteColumn(), TimeElapsedColumn(), console=self.c, transient=True) as prog:
                task = prog.add_task(ctr.description, total=None)

                def on_plan(p):
                    ctr.plan(p)
                    prog.update(task, total=ctr.total, completed=0, description=ctr.description)

                def on_granule(pts):
                    if ctr.granule(pts):
                        prog.update(task, completed=ctr.done)

                yield {"on_plan": on_plan, "on_granule": on_granule}

        return _cm()


def run(console: Console, region: str, bbox, res: int = 5) -> int:
    return App(console, region, bbox, res).run()
