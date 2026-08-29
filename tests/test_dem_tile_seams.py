"""DEM tiles must merge without a seam.

_read_tile_window places each tile's data into the scene grid. If the destination block is not snapped to the output
lattice, each tile lands a fraction of a pixel off — differently per tile — and adjacent tiles disagree along their
shared edge. On steep terrain that is a hard elevation step at the seam (measured 258 m on a real Greenland scene),
which reads visually as the terrain repeating/shifting across the tile boundary.

The test cuts ONE continuous surface into two adjacent tiles, merges them the way _polar_grid does, and requires the
result to match the original surface — so any placement shift shows up as a seam.
"""
import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

from aicesat import dem  # noqa: E402

RES = 32.0          # tile pixel size (m), like ArcticDEM 32 m
TILE = 64           # pixels per tile side


def surface(x, y):
    """A smooth but steep analytic surface — a seam shows up as a step against it."""
    return 800.0 + 0.02 * x + 0.015 * y + 120.0 * np.sin(x / 900.0) * np.cos(y / 700.0)


def _write_tile(path, x0, y1, nx=TILE, ny=TILE):
    """One tile with its top-left corner at (x0, y1), sampled from `surface` at pixel centres."""
    xs = x0 + (np.arange(nx) + 0.5) * RES
    ys = y1 - (np.arange(ny) + 0.5) * RES
    z = surface(xs[None, :], ys[:, None]).astype("f4")
    with rasterio.open(path, "w", driver="GTiff", height=ny, width=nx, count=1, dtype="float32",
                       crs="EPSG:3413", transform=from_origin(x0, y1, RES, RES)) as dst:
        dst.write(z, 1)
    return z


def test_adjacent_tiles_merge_without_a_seam(tmp_path):
    span = TILE * RES
    left = tmp_path / "left.tif"
    right = tmp_path / "right.tif"
    _write_tile(left, 0.0, span)                 # x in [0, span)
    _write_tile(right, span, span)               # x in [span, 2*span) — shares the edge at x=span

    # scene grid spanning both tiles, at a DIFFERENT resolution than the tiles (forces resampling, as in production)
    nx, ny = 97, 48
    bounds = (0.0, 0.0, 2 * span, span)
    parts = [dem._read_tile_window(str(p), bounds, (ny, nx)) for p in (left, right)]
    assert all(p is not None for p in parts), "both tiles must overlap the grid"

    grid = np.full((ny, nx), np.nan, dtype="f4")
    for p in parts:                              # merge exactly as _polar_grid does (first finite wins)
        m = np.isfinite(p) & ~np.isfinite(grid)
        grid[m] = p[m]

    assert np.isfinite(grid).all(), "merged grid has holes"

    # compare against the analytic surface sampled on the SAME grid
    dx, dy = (bounds[2] - bounds[0]) / nx, (bounds[3] - bounds[1]) / ny
    gx = bounds[0] + (np.arange(nx) + 0.5) * dx
    gy = bounds[3] - (np.arange(ny) + 0.5) * dy
    want = surface(gx[None, :], gy[:, None])
    err = np.abs(grid - want)
    # resampling a 32 m tile onto a ~66 m grid carries real interpolation error; a placement SHIFT is far larger.
    assert err.max() < 25.0, f"max error {err.max():.1f} m — tiles are misplaced"

    # the seam itself: no step at the boundary beyond the local terrain gradient
    steps = np.abs(np.diff(grid, axis=1))
    seam_col = int(round(span / dx)) - 1
    seam = steps[:, max(0, seam_col - 1):seam_col + 2].max()
    typical = float(np.median(steps))
    assert seam < typical + 15.0, f"step of {seam:.1f} m at the seam vs typical {typical:.1f} m"


def test_single_tile_lands_on_the_lattice(tmp_path):
    """A tile smaller than the grid must occupy exactly the cells its extent covers — no half-pixel drift."""
    span = TILE * RES
    t = tmp_path / "one.tif"
    _write_tile(t, 0.0, span)
    nx, ny = 40, 40
    # tile spans x in [0, span), y in [0, span) -> the top-RIGHT quadrant of this grid
    bounds = (-span, -span, span, span)
    out = dem._read_tile_window(str(t), bounds, (ny, nx))
    assert out is not None
    fin = np.isfinite(out)
    rows = np.where(fin.any(axis=1))[0]
    cols = np.where(fin.any(axis=0))[0]
    assert rows[0] == 0 and (rows[-1] + 1) == ny // 2         # exactly the top half, anchored at row 0
    assert cols[0] == nx // 2 and (cols[-1] + 1) == nx        # exactly the right half, anchored at the midline
