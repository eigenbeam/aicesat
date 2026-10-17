"""Pre-build the GEDI L2A sub-granule index over an area, uncapped and in parallel (V003 only).
usage: uv run scripts/build_gedi_index.py <W> <S> <E> <N> [res] [workers]
"""
import sys

if __name__ == "__main__":   # guard: index workers are spawned processes that re-import this file
    from aicesat import build
    sys.exit(build.cli_main(build.collection("GEDI"), sys.argv[1:]))
