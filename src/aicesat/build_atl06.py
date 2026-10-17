"""Building the ATL06 sub-granule index — deliberately NOT in index_atl06.

index_atl06 is a QUERY-path module: fetch_bbox and plan_bbox are what a scene or a TUI command call, and
tests/test_index_only.py holds the line that nothing on that path may reach CMR. Discovery is paid once, here, at
build time; an unindexed area is an error at query time, never a slow fallback that searches CMR and succeeds.
Keeping the builder in its own module makes that boundary a fact of the file tree rather than a convention.

The sequence itself (plan, search, resume, pool, claim, rollup) is build.run_build, shared by every collection; this
module is the ATL06 door the TUI and scripts/build_atl06_index.py already know.
"""
from __future__ import annotations

from . import build, index_atl06

PER_GRANULE_TIMEOUT_S = build.collection("ATL06").timeout_s


def build_bbox(bbox, res: int = index_atl06.ATL06_RES, workers: int | None = None, window=None, on_plan=None, on_granule=None) -> dict:
    """Build the ATL06 sub-granule index over `bbox` -- resumable and parallel. See build.run_build."""
    return build.run_build(build.collection("ATL06"), bbox, res=res, workers=workers, window=window,
                           on_plan=on_plan, on_granule=on_granule)
