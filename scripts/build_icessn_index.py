"""Build the ICESSN/ILATM2 line-offset index over a bbox — resumable and parallel (see build_atl06_index.py).

Usage:  uv run python scripts/build_icessn_index.py W S E N [res] [workers]
Example (SW Greenland):  uv run python scripts/build_icessn_index.py -52 62 -44 70 5
"""
import sys

if __name__ == "__main__":   # guard: index workers are spawned processes that re-import this file
    from aicesat import build
    sys.exit(build.cli_main(build.collection("ICESSN"), sys.argv[1:]))
