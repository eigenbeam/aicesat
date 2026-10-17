"""Build the IS2TGPSSS (Summit GPS traverse) line-offset index over a bbox — resumable and parallel, like
build_icessn_index.py.

The traverse metadata file is read ONCE, in the parent before the worker pool starts (build._gps_prepare), and every
survey's sled geometry is resolved from it and stored on that survey's index rows (a fetch never sees the metadata
file). The collection's RINEX files are never read.

Usage:  uv run python scripts/build_gpstruth_index.py W S E N [res] [workers]
Example (the transect):  uv run python scripts/build_gpstruth_index.py -38.585667 72.578616 -38.474193 72.648222 5
"""
import sys

if __name__ == "__main__":   # guard: index workers are spawned processes that re-import this file
    from aicesat import build
    sys.exit(build.cli_main(build.collection("GPSTRUTH"), sys.argv[1:]))
