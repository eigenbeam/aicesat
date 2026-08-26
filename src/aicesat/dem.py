"""DEM base surface: ArcticDEM v4.1 32 m mosaic tiles (PGC, AWS Open Data, public COGs, EPSG:3413, heights above the
WGS84 ellipsoid — the same vertical reference as ATL03 h_ph). Licence CC-BY-4.0.

Citation: Porter, C., Howat, I., Noh, M.-J., et al. (2023). ArcticDEM – Mosaics, Version 4.1. Harvard Dataverse.
https://doi.org/10.7910/DVN/3VDC4W. "DEMs provided by the Polar Geospatial Center under NSF-OPP awards 1043681,
1559691, 1542736, 1810976, and 2129685."

Tiles are 100 km squares on a grid with origin (-4,000,000, -4,000,000) m in EPSG:3413, named {row}_{col} (1-based):
row = floor((y + 4e6) / 1e5) + 1, col = floor((x + 4e6) / 1e5) + 1. Tiles over open ocean do not exist (404).
Used only when the scene frame is EPSG:3413 (Arctic), so reading the scene's grid is a pure window read with COG overviews — no warp; elsewhere the caller falls back to the photon-interpolated surface.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import cache

log = logging.getLogger(__name__)

TILE_URL = "https://pgc-opendata-dems.s3.us-west-2.amazonaws.com/arcticdem/mosaics/v4.1/32m/{r}_{c}/{r}_{c}_32m_v4.1_dem.tif"
ATTRIBUTION = "ArcticDEM v4.1 32 m mosaic, Polar Geospatial Center (CC BY-4.0), doi:10.7910/DVN/3VDC4W"
SOURCE = "ArcticDEM v4.1 32m"
NODATA = -9999.0
TILE_M, ORIGIN = 100_000.0, -4_000_000.0
DEM_DIR = cache.DATA_DIR / "cache" / "dem"
MAX_CELLS = 120_000  # DEM mesh: ~120k vertices is fine for deck.gl; JSON stays ~1 MB


def tile_index(x: float, y: float) -> tuple[int, int]:
    """(row, col) of the 100 km ArcticDEM mosaic tile containing EPSG:3413 (x, y)."""
    return int(math.floor((y - ORIGIN) / TILE_M)) + 1, int(math.floor((x - ORIGIN) / TILE_M)) + 1


def tiles_for_extent(x0: float, y0: float, x1: float, y1: float) -> list[tuple[int, int]]:
    r0, c0 = tile_index(x0, y0); r1, c1 = tile_index(x1, y1)
    return [(r, c) for r in range(min(r0, r1), max(r0, r1) + 1) for c in range(min(c0, c1), max(c0, c1) + 1)]


def _read_tile_window(url: str, bounds: tuple[float, float, float, float], shape: tuple[int, int]) -> np.ndarray | None:
    """Read `bounds` (EPSG:3413 x0, y0, x1, y1) from one COG into `shape` (rows, cols) using overviews; None if absent."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                       GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", VSI_CACHE="TRUE", GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="1")
    try:
        with env, rasterio.open(url) as src:
            tb = src.bounds
            ix0, iy0, ix1, iy1 = max(bounds[0], tb.left), max(bounds[1], tb.bottom), min(bounds[2], tb.right), min(bounds[3], tb.top)
            if ix0 >= ix1 or iy0 >= iy1:
                return None
            win = from_bounds(ix0, iy0, ix1, iy1, transform=src.transform)
            # output pixels of this tile's overlap within the full grid
            rows, cols = shape
            dx, dy = (bounds[2] - bounds[0]) / cols, (bounds[3] - bounds[1]) / rows
            c0, c1 = int(round((ix0 - bounds[0]) / dx)), int(round((ix1 - bounds[0]) / dx))
            r0, r1 = int(round((bounds[3] - iy1) / dy)), int(round((bounds[3] - iy0) / dy))
            if c1 <= c0 or r1 <= r0:
                return None
            arr = src.read(1, window=win, out_shape=(r1 - r0, c1 - c0), resampling=Resampling.bilinear, masked=True).filled(np.nan)
            out = np.full(shape, np.nan, dtype="f4")
            out[r0:r1, c0:c1] = arr
            return out
    except Exception as e:
        msg = str(e)
        if "404" in msg or "does not exist" in msg or "No such file" in msg:
            log.info("DEM tile absent (ocean?): %s", url.rsplit("/", 1)[-1])
        else:
            log.warning("DEM tile read failed %s: %s", url.rsplit("/", 1)[-1], msg[:120])
        return None


def surface_for_frame(frame: dict, extent: tuple[float, float, float, float], z0: float, cell_m: float = 100.0) -> dict | None:
    """DEM height field on the scene's local grid (same dict shape as scene.surface_grid; z relative to z0).
    ArcticDEM tiles are EPSG:3413, so this is a pure window read only when the scene frame is EPSG:3413 (Arctic);
    elsewhere returns None and the caller falls back to the photon-interpolated surface (REMA/global DEM: future)."""
    if frame.get("crs") != "EPSG:3413":
        return None
    ox, oy = frame["origin_xy"]
    x0, y0, x1, y1 = extent
    cell = float(cell_m)
    while ((x1 - x0) / cell + 1) * ((y1 - y0) / cell + 1) > MAX_CELLS:
        cell *= 1.5
    nx, ny = int(np.ceil((x1 - x0) / cell)) + 1, int(np.ceil((y1 - y0) / cell)) + 1
    ax0, ay0, ax1, ay1 = ox + x0, oy + y0, ox + x0 + (nx - 1) * cell, oy + y0 + (ny - 1) * cell
    key = hashlib.sha1(f"{ax0:.0f},{ay0:.0f},{ax1:.0f},{ay1:.0f},{cell:.0f}".encode()).hexdigest()[:16]
    DEM_DIR.mkdir(parents=True, exist_ok=True)
    npz = DEM_DIR / f"{key}.npz"
    if npz.exists():
        grid = np.load(npz)["z"]
    else:
        tiles = tiles_for_extent(ax0, ay0, ax1, ay1)
        bounds = (ax0 - cell / 2, ay0 - cell / 2, ax1 + cell / 2, ay1 + cell / 2)  # pixel-centre grid -> pixel-edge bounds
        with ThreadPoolExecutor(min(8, len(tiles))) as ex:
            parts = list(ex.map(lambda rc: _read_tile_window(TILE_URL.format(r=rc[0], c=rc[1]), bounds, (ny, nx)), tiles))
        grid = np.full((ny, nx), np.nan, dtype="f4")
        for p in parts:
            if p is not None:
                m = np.isfinite(p) & ~np.isfinite(grid)
                grid[m] = p[m]
        if not np.isfinite(grid).any():
            return None
        np.savez_compressed(npz, z=grid)
    z = np.flipud(grid)  # row 0 = south, to match surface_grid's ascending-y layout
    z = z.astype("f8") - z0
    valid = np.isfinite(z)
    zlist = [None if not np.isfinite(v) else round(float(v), 2) for v in z.ravel()]
    return {"x0": float(x0), "y0": float(y0), "cell": cell, "nx": nx, "ny": ny, "z": zlist, "source": SOURCE, "attribution": ATTRIBUTION,
            "n_cells_observed": int(valid.sum()), "n_cells_extrapolated": 0, "nodata_cells": int((~valid).sum()),
            "note": f"{SOURCE}: multi-year median DEM (2007–2022 strips), WGS84-ellipsoid heights on a {cell:.0f} m grid; "
                    f"decimetre–metre offsets from any single ICESat-2 pass are expected and are NOT the comparison signal. {ATTRIBUTION}"}


def slope_deg(surface: dict, x: np.ndarray, y: np.ndarray) -> float | None:
    """Median DEM slope (degrees) at local (x, y) positions, from central differences on the surface grid."""
    if not surface or surface.get("source") != SOURCE:
        return None
    z = np.array([np.nan if v is None else v for v in surface["z"]], dtype="f8").reshape(surface["ny"], surface["nx"])
    cell = surface["cell"]
    gy, gx = np.gradient(z, cell)
    s = np.degrees(np.arctan(np.hypot(gx, gy)))
    i = np.clip(np.round((y - surface["y0"]) / cell).astype(int), 0, surface["ny"] - 1)
    j = np.clip(np.round((x - surface["x0"]) / cell).astype(int), 0, surface["nx"] - 1)
    v = s[i, j]
    v = v[np.isfinite(v)]
    return float(np.median(v)) if v.size else None
