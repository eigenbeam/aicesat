"""Did the index-pruning / lake-scoping optimisations drop ATL06 data?

Compares the NEW pruned paths against the OLD full-scan equivalents for one scene's bbox, and reports where the
ATL06 data actually sits relative to that bbox. A mismatch means an optimisation silently dropped data.

    uv run python scripts/verify_prune.py <scene_id>
"""
import glob
import sys

import duckdb

from aicesat import auth, cache, coverage, index_atl06, lake, planner


def main(sid: str) -> None:
    auth.login()
    bbox = cache.load_scene(sid)["bbox"]
    R = index_atl06.ATL06_RES
    d = index_atl06._index_dir(R)
    want = planner.cells_for_bbox(bbox, res=R)
    print("bbox:", bbox)
    print(f"cells at res {R}: {len(want)}")

    # 1. index rows: pruned (manifest) vs full directory scan
    cols = ["granule", "url", "s3url", "sdp_epoch", "beam", "chunk_index", "seg_start", "seg_end", "h3_cell"]
    for ds in index_atl06.ATL06_DATASETS:
        cols += [f"{ds}_offset", f"{ds}_size", f"{ds}_dtype", f"{ds}_filters", f"{ds}_mask"]
    where = f"h3_cell IN ({','.join(str(int(c)) for c in want)})"
    con = duckdb.connect()
    full = con.execute(f"SELECT DISTINCT {', '.join(cols)} FROM read_parquet('{d}/*.parquet') WHERE {where}").fetchall()
    con.close()
    files = coverage.index_files_for_cells("ATL06", want)
    n_idx = len(glob.glob(f"{d}/*.parquet"))
    print(f"manifest named {len(files) if files is not None else 'None'} of {n_idx} index files")
    _w, pruned = index_atl06._index_rows(bbox, None, R, strong_only=False)
    print(f"index rows  full-scan={len(full)}  pruned={len(pruned)}  MATCH={len(full) == len(pruned)}")
    gran_full = {r[0] for r in full}
    gran_pruned = {r["granule"] for r in pruned}
    print(f"granules    full-scan={len(gran_full)}  pruned={len(gran_pruned)}  missing={sorted(gran_full - gran_pruned)[:5]}")

    # 2. lake read: per-cell globs vs whole-mission glob
    allg = f"{lake.LAKE_DIR}/mission=ATL06/h3_cell=*/*__c*.parquet"
    pred = f"h3_cell IN ({','.join(str(int(c)) for c in want)})"
    con = duckdb.connect()
    n_old = con.execute(f"SELECT count(*) FROM read_parquet('{allg}', hive_partitioning=true, union_by_name=true) "
                        f"WHERE {pred} AND quality = 0").fetchone()[0]
    con.close()
    arr = lake.query_points(bbox, want, "ATL06", extra_cols=("quality",), quality_zero=True, clip_cells=True)
    print(f"lake points whole-glob={n_old:,}  scoped={arr['lon'].size:,}  MATCH={n_old == arr['lon'].size}")

    # 3. where does the ATL06 data actually sit?
    if arr["lon"].size:
        print(f"ATL06 extent: lon {arr['lon'].min():.3f}..{arr['lon'].max():.3f}  lat {arr['lat'].min():.3f}..{arr['lat'].max():.3f}")
    print(f"scene   bbox: lon {bbox[0]:.3f}..{bbox[2]:.3f}  lat {bbox[1]:.3f}..{bbox[3]:.3f}")
    per_cell = {}
    for c in want:
        cd = lake.cell_dir("ATL06", int(c))
        per_cell[int(c)] = len(list(cd.glob("*__c*.parquet"))) if cd.is_dir() else 0
    print("lake chunk files per requested cell:", per_cell)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "a430193db4")
