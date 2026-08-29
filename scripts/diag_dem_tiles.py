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


def main(sid: str) -> None:
    doc = cache.load_scene(sid)
    frame, bbox = doc["frame"], doc["bbox"]
    extent = scene.bbox_extent(frame)
    print(f"scene {sid}  bbox={bbox}\nframe crs={frame['crs']}  extent={[round(v) for v in extent]}")

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
    # look for repeated blocks: compare each 64x64 block against every other
    B = 64
    blocks = {}
    for r in range(0, z.shape[0] - B, B):
        for c in range(0, z.shape[1] - B, B):
            blk = z[r:r + B, c:c + B]
            if not np.isfinite(blk).all():
                continue
            key = round(float(blk.sum()), 3)
            if key in blocks:
                prev = blocks[key]
                if np.allclose(blk, z[prev[0]:prev[0] + B, prev[1]:prev[1] + B]):
                    print(f"!! REPEATED BLOCK: grid[{r}:{r+B}, {c}:{c+B}] == grid[{prev[0]}:{prev[0]+B}, {prev[1]}:{prev[1]+B}]")
            else:
                blocks[key] = (r, c)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2ce7444e9a")
