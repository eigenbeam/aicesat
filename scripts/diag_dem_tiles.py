"""Diagnose the repeated-DEM-tile artifact: does the same tile content land in more than one place?

Instruments dem._read_tile_window to record, per tile, the URL, the tile's own georeferenced bounds, and the
destination slice it writes into. Then checks for (a) duplicate URLs, (b) destination slices that overlap, and
(c) blocks of the final grid that are numerically identical — which is what "the same tile repeated in the next
row, offset" would look like.

    uv run python scripts/diag_dem_tiles.py <scene_id>
"""
import sys

import numpy as np

from aicesat import cache, dem, scene


def main(sid: str, refetch: bool = False) -> None:
    doc = cache.load_scene(sid)
    frame, bbox = doc["frame"], doc["bbox"]
    extent = scene.bbox_extent(frame)
    print(f"scene {sid}  bbox={bbox}\nframe crs={frame['crs']}  extent={[round(v) for v in extent]}")
    if refetch:
        # the polar backend caches the merged grid as an npz, so tile reads are skipped on a warm run —
        # clear it so the per-tile placement logic is actually exercised
        n = 0
        for p in dem.DEM_DIR.glob("*.npz"):
            p.unlink(); n += 1
        print(f"cleared {n} cached DEM grids (forcing a re-read of the tiles)")

    seen = []
    real = dem._read_tile_window

    def spy(url, bounds, shape):
        out = real(url, bounds, shape)
        if out is None:
            seen.append({"url": url, "placed": None})
            return out
        fin = np.isfinite(out)
        if not fin.any():
            seen.append({"url": url, "placed": None})
            return out
        rows = np.where(fin.any(axis=1))[0]
        cols = np.where(fin.any(axis=0))[0]
        box = (int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1)
        seen.append({"url": url, "placed": box,
                     "checksum": float(np.nansum(out[fin])), "n": int(fin.sum())})
        return out

    dem._read_tile_window = spy
    try:
        surf = dem.surface_for_frame(frame, extent, 0.0)
    finally:
        dem._read_tile_window = real

    print(f"\ntiles requested: {len(seen)}")
    urls = [s["url"] for s in seen]
    dupe_urls = {u for u in urls if urls.count(u) > 1}
    print(f"duplicate URLs requested: {sorted(dupe_urls) if dupe_urls else 'none'}")
    for s in seen:
        name = s["url"].rsplit("/", 1)[-1]
        print(f"  {name:48s} -> {s['placed']}" + (f"  n={s['n']}" if s["placed"] else "  (no overlap)"))

    # identical content landing in different places
    placed = [s for s in seen if s["placed"]]
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            a, b = placed[i], placed[j]
            if a["n"] == b["n"] and abs(a["checksum"] - b["checksum"]) < 1e-6:
                print(f"\n!! IDENTICAL CONTENT: {a['url'].rsplit('/',1)[-1]} @ {a['placed']}"
                      f" == {b['url'].rsplit('/',1)[-1]} @ {b['placed']}")

    if not surf:
        print("\nno surface returned"); return
    z = np.asarray([np.nan if v is None else v for v in surf["z"]], dtype="f8").reshape(surf["ny"], surf["nx"])
    print(f"\ngrid {z.shape}  cell={surf['cell']}m  finite={np.isfinite(z).sum()}")
    # Duplicate ROWS / COLUMNS catch a repeat at ANY offset (block-aligned comparison misses a shift that is not a
    # multiple of the block size). Real terrain essentially never produces two identical 272-wide rows.
    def dup_lines(a, axis_name):
        seen, dups = {}, []
        for i, line in enumerate(a):
            if not np.isfinite(line).all():
                continue
            key = (round(float(line.sum()), 4), round(float(line[0]), 4), round(float(line[-1]), 4))
            j = seen.get(key)
            if j is not None and np.array_equal(line, a[j]):
                dups.append((j, i))
            else:
                seen[key] = i
        if dups:
            offs = sorted({b - a_ for a_, b in dups})
            print(f"!! {len(dups)} duplicate {axis_name} pairs; offsets={offs[:10]}  e.g. {dups[:5]}")
        else:
            print(f"   no duplicate {axis_name}")
    dup_lines(z, "rows")
    dup_lines(z.T, "columns")

    # Flat "shelf" regions: rows whose values are (near) constant — what a nodata fill or a bad tile read looks like
    flat = [i for i, r in enumerate(z) if np.isfinite(r).all() and float(np.nanstd(r)) < 1e-6]
    print(f"   near-constant rows: {len(flat)}" + (f" e.g. {flat[:10]}" if flat else ""))
    # Big vertical steps between adjacent rows/cols = seams
    dr = np.abs(np.diff(z, axis=0)); dc = np.abs(np.diff(z, axis=1))
    for name, d in (("row", dr), ("col", dc)):
        if np.isfinite(d).any():
            big = np.nanmax(d)
            where = np.unravel_index(int(np.nanargmax(np.nan_to_num(d, nan=-1))), d.shape)
            print(f"   largest {name}-to-{name} step: {big:.1f} m at {where}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    main(args[0] if args else "2ce7444e9a", refetch="--refetch" in sys.argv)
