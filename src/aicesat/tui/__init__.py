"""aicesat-tui — a Rich REPL over the ATL06 sub-granule index and the lake.

Build the index for a box, explore what it holds, materialize cells, query across the lake/NASA boundary, and see
what every step cost. `main()` deliberately parses --data-dir BEFORE importing any aicesat module: cache.DATA_DIR
binds at import time, so a later assignment would be ignored by every module that already read it.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

DEFAULT_REGION = "jakobshavn_margin"   # not regions.DEFAULT_REGION: this is the box the local ATL06 index covers


def setup_logging(verbose: bool = False) -> None:
    """Route the library's INFO stream to logbuf's ring buffer without scribbling over the prompt.

    The pipeline loggers must sit at INFO or logbuf's handler never sees a record — and logbuf IS what the `log`
    command reads. Their records still PROPAGATE to the root handler, and a logger's level does not gate its
    ancestors' handlers, so the terminal is kept quiet by raising the HANDLER's level rather than the loggers'.
    Setting the loggers to WARNING instead (which this did) silenced the terminal AND emptied the buffer.

    `verbose` lowers the handler so the same stream is mirrored to stderr live. Third-party loggers go to ERROR,
    not WARNING: urllib3 warns on every recycled connection ("Connection pool is full"), which is normal for the
    fetch pool and would cover the prompt for the length of a build.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.setLevel(logging.INFO if verbose else logging.WARNING)
    for noisy in ("fsspec", "urllib3", "earthaccess", "botocore", "aiobotocore", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    logging.getLogger("aicesat").setLevel(logging.INFO)


def main() -> int:
    ap = argparse.ArgumentParser(prog="aicesat-tui", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=None, help="override AICESAT_DATA_DIR (default: <repo>/data)")
    ap.add_argument("--region", default=DEFAULT_REGION, help=f"starting region (default {DEFAULT_REGION})")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="starting bbox, overrides --region")
    ap.add_argument("--res", type=int, default=None, help="H3 addressing resolution (default: the collection's own)")
    ap.add_argument("-v", "--verbose", action="store_true", help="library logs to stderr as well as the buffer")
    a = ap.parse_args()

    if a.data_dir:                      # must precede the first aicesat import — see the module docstring
        os.environ["AICESAT_DATA_DIR"] = a.data_dir

    setup_logging(a.verbose)

    from rich.console import Console

    from . import app as app_mod
    from .. import auth, index_atl06, logbuf, regions

    logbuf.install()
    console = Console()

    try:
        bbox = regions.resolve_bbox(None, tuple(a.bbox)) if a.bbox else regions.resolve_bbox(a.region)
    except (KeyError, ValueError) as e:
        print(f"aicesat-tui: {e}", file=sys.stderr)
        return 2
    region = "(custom)" if a.bbox else a.region

    # Say so NOW rather than three commands in, halfway through a build. login() must stay non-interactive: a
    # prompt on stdin would fight the REPL for the terminal (auth.py explains why the MCP server bans it too).
    try:
        missing = not auth.EDL_FILE.exists()
    except OSError as e:      # unreadable is not absent, and neither is fatal: never let a CHECK take down startup
        missing, e_msg = True, f" ({type(e).__name__})"
    else:
        e_msg = ""
    if missing:
        console.print(f"[yellow]no readable Earthdata token at {auth.EDL_FILE}{e_msg}[/yellow] — "
                      f"read-only commands work; [bold]build[/bold] and a cold [bold]query[/bold] will fail.")

    return app_mod.run(console, region, bbox, a.res or index_atl06.ATL06_RES)
