"""Build the ATL06 sub-granule H3 index over a bbox — resumable and parallel. Re-run to resume (each granule
writes its own Parquet under data/index/atl06/res<R>/, so finished granules are skipped on a re-run).

Usage:  uv run python scripts/build_atl06_index.py W S E N [res] [workers]
Example (SW Greenland incl. Jakobshavn + K-transect):
        uv run python scripts/build_atl06_index.py -52 62 -44 70 5

The build itself is aicesat.build.run_build, shared by every collection; this is its ATL06 door.
"""
import sys

if __name__ == "__main__":   # guard: index workers are spawned processes that re-import this file
    from aicesat import build
    sys.exit(build.cli_main(build.collection("ATL06"), sys.argv[1:]))
