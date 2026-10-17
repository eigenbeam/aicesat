"""Building the GEDI sub-granule index — deliberately NOT in index_gedi, for the reason build_atl06 gives.

index_gedi is a QUERY-path module: fetch_bbox and plan_bbox are what a scene calls, and tests/test_index_only.py
holds the line that nothing on that path may reach CMR. Discovery is paid once, here, at build time.

V003 ONLY (build.collection("GEDI")). V002 and V003 are the same acquisitions (V003 is a reprocessing), so indexing
both would double every shot in the lake and correlate anything computed from them.
"""
from __future__ import annotations

from . import build, index_gedi

PER_GRANULE_TIMEOUT_S = build.collection("GEDI").timeout_s


def build_bbox(bbox, res: int = index_gedi.GEDI_RES, workers: int | None = None, window=None,
               on_plan=None, on_granule=None) -> dict:
    """Build the GEDI index over `bbox` — resumable and parallel. See build.run_build."""
    return build.run_build(build.collection("GEDI"), bbox, res=res, workers=workers, window=window,
                           on_plan=on_plan, on_granule=on_granule)
