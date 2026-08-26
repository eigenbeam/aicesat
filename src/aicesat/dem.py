"""DEM base surface for a scene. Every backend returns heights above the **WGS84 ellipsoid** — the same vertical
reference as ATL03 h_ph — so the DEM, the ICESat-2 photons and the (ellipsoid-corrected) GLAS shots share one datum.

Three backends, chosen by the scene's frame CRS (see scene.frame_crs):
  * EPSG:3413 (Arctic)     -> ArcticDEM v4.1 32 m mosaic (PGC, AWS Open Data). Ellipsoidal. Pure window read.
  * EPSG:3031 (Antarctic)  -> REMA v2.0 32 m mosaic (PGC, AWS Open Data). Ellipsoidal. Pure window read.
  * anything else (aeqd)   -> Copernicus GLO-30 (ESA, AWS Open Data). Heights are EGM2008 **geoid**, so the EGM2008
                             undulation N is added (h_ellipsoid = H_geoid + N) using the PROJ egm08 grid; sampled by
                             lon/lat because the DEM is EPSG:4326 and the frame is azimuthal-equidistant.

There is no photon-interpolated fallback: a scene shows a DEM surface only where a real DEM covers it, else no surface.

Citations (shown in the widget):
  ArcticDEM: Porter, C., Howat, I., Noh, M.-J., et al. (2023). ArcticDEM Mosaics v4.1. Harvard Dataverse.
             https://doi.org/10.7910/DVN/3VDC4W. Provided by the Polar Geospatial Center under NSF-OPP awards.
  REMA:      Howat, I., Porter, C., Smith, B.E., Noh, M.-J., Morin, P. (2019). The Reference Elevation Model of
             Antarctica. The Cryosphere 13, 665-674. Mosaics v2.0, Polar Geospatial Center. CC BY-4.0.
  Copernicus GLO-30: (C) ESA / Airbus / DLR; Copernicus DEM, produced using Copernicus WorldDEM-30. Free of charge.
  EGM2008 undulation grid us_nga_egm08_25.tif (NGA, public domain) via cdn.proj.org.
"""
from __future__ import annotations

import hashlib
import logging
import math

import numpy as np

from . import cache

log = logging.getLogger(__name__)

# --- backends -------------------------------------------------------------------------------------------------------
ARCTIC_URL = "https://pgc-opendata-dems.s3.us-west-2.amazonaws.com/arcticdem/mosaics/v4.1/32m/{r}_{c}/{r}_{c}_32m_v4.1_dem.tif"
ARCTIC_ATTR = "ArcticDEM v4.1 32 m mosaic, Polar Geospatial Center (CC BY-4.0), doi:10.7910/DVN/3VDC4W"
ARCTIC_ORIGIN = -4_000_000.0

REMA_URL = "https://pgc-opendata-dems.s3.us-west-2.amazonaws.com/rema/mosaics/v2.0/32m/{r:02d}_{c:02d}/{r:02d}_{c:02d}_32m_v2.0_dem.tif"
REMA_ATTR = "REMA v2.0 32 m mosaic, Polar Geospatial Center (CC BY-4.0), Howat et al. 2019, The Cryosphere"
REMA_ORIGIN = -3_000_000.0

COP_URL = "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{ns}{la:02d}_00_{ew}{lo:03d}_00_DEM/Copernicus_DSM_COG_10_{ns}{la:02d}_00_{ew}{lo:03d}_00_DEM.tif"
COP_ATTR = "Copernicus DEM GLO-30 (© ESA/Airbus/DLR), heights EGM2008→WGS84-ellipsoid via NGA egm08"
EGM08_URL = "/vsicurl/https://cdn.proj.org/us_nga_egm08_25.tif"  # 2.5' global EGM2008 undulation N (m above ellipsoid)

NODATA = -9999.0
TILE_M = 100_000.0
DEM_DIR = cache.DATA_DIR / "cache" / "dem"
MAX_CELLS = 120_000     # DEM mesh: ~120k vertices keeps the scene JSON ~1 MB
MAX_READ_PX = 2048      # cap a single COG window read so a large lon/lat box can't blow up memory
H_MIN, H_MAX = -500.0, 9000.0  # plausible surface-elevation clip (Dead Sea shore to Everest) -> reject stray nodata

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
            GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", VSI_CACHE="TRUE", GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="1")


# --- ArcticDEM / REMA tile grid (100 km tiles, {row}_{col}, origin at a CRS-specific corner) -------------------------
def _tile_rc(x: float, y: float, origin: float) -> tuple[int, int]:
    return int(math.floor((y - origin) / TILE_M)) + 1, int(math.floor((x - origin) / TILE_M)) + 1


def _tiles_for_extent(x0, y0, x1, y1, origin) -> list[tuple[int, int]]:
    r0, c0 = _tile_rc(x0, y0, origin)
    r1, c1 = _tile_rc(x1, y1, origin)
    return [(r, c) for r in range(min(r0, r1), max(r0, r1) + 1) for c in range(min(c0, c1), max(c0, c1) + 1)]


# ArcticDEM-named wrappers kept for callers/tests (origin -4e6).
def tile_index(x: float, y: float) -> tuple[int, int]:
    """(row, col) of the 100 km ArcticDEM mosaic tile containing EPSG:3413 (x, y)."""
    return _tile_rc(x, y, ARCTIC_ORIGIN)


def tiles_for_extent(x0, y0, x1, y1) -> list[tuple[int, int]]:
    return _tiles_for_extent(x0, y0, x1, y1, ARCTIC_ORIGIN)


def _read_tile_window(url: str, bounds: tuple[float, float, float, float], shape: tuple[int, int]) -> np.ndarray | None:
    """Read `bounds` (projected x0, y0, x1, y1 in the tile's CRS) from one COG into `shape` (rows, cols) using
    overviews; None if the tile is absent or does not overlap. Used for the polar (frame CRS == tile CRS) backends."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    try:
        with rasterio.Env(**_ENV), rasterio.open(url) as src:
            tb = src.bounds
            ix0, iy0 = max(bounds[0], tb.left), max(bounds[1], tb.bottom)
            ix1, iy1 = min(bounds[2], tb.right), min(bounds[3], tb.top)
            if ix0 >= ix1 or iy0 >= iy1:
                return None
            win = from_bounds(ix0, iy0, ix1, iy1, transform=src.transform)
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
        _log_absent(url, e)
        return None


def _log_absent(url, e):
    msg = str(e)
    name = url.rsplit("/", 1)[-1]
    if "404" in msg or "does not exist" in msg or "No such file" in msg or "not recognized" in msg:
        log.info("DEM tile absent (ocean/edge?): %s", name)
    else:
        log.warning("DEM tile read failed %s: %s", name, msg[:120])


def _grid_local(frame, extent, cell_m):
    """Local pixel-centre grid (nx, ny, cell, absolute proj x/y arrays in the frame CRS)."""
    ox, oy = frame["origin_xy"]
    x0, y0, x1, y1 = extent
    cell = float(cell_m)
    while ((x1 - x0) / cell + 1) * ((y1 - y0) / cell + 1) > MAX_CELLS:
        cell *= 1.5
    nx = int(np.ceil((x1 - x0) / cell)) + 1
    ny = int(np.ceil((y1 - y0) / cell)) + 1
    ax = ox + x0 + np.arange(nx) * cell          # absolute projected x of each column
    ay = oy + y0 + np.arange(ny) * cell          # absolute projected y of each row (ascending north)
    return nx, ny, cell, ax, ay


def _finish(z, x0, y0, cell, nx, ny, source, attr, note):
    """Package a (ny, nx) ellipsoidal-height grid (row 0 = south) into the scene.surface dict, z relative to nothing
    yet — caller subtracts z0."""
    valid = np.isfinite(z)
    if not valid.any():
        return None
    zlist = [None if not np.isfinite(v) else round(float(v), 2) for v in z.ravel()]
    return {"x0": float(x0), "y0": float(y0), "cell": float(cell), "nx": int(nx), "ny": int(ny), "z": zlist,
            "source": source, "attribution": attr, "is_dem": True,
            "n_cells_observed": int(valid.sum()), "n_cells_extrapolated": 0, "nodata_cells": int((~valid).sum()),
            "note": note}


# --- polar backends (ArcticDEM / REMA): frame CRS == tile CRS, so a pure window read into the scene grid -------------
def _polar_grid(frame, extent, z0, cell_m, url_tmpl, origin, source, attr) -> dict | None:
    x0, y0, x1, y1 = extent
    nx, ny, cell, ax, ay = _grid_local(frame, extent, cell_m)
    ax0, ay0, ax1, ay1 = ax[0], ay[0], ax[-1], ay[-1]
    key = hashlib.sha1(f"{source}|{ax0:.0f},{ay0:.0f},{ax1:.0f},{ay1:.0f},{cell:.0f}".encode()).hexdigest()[:16]
    DEM_DIR.mkdir(parents=True, exist_ok=True)
    npz = DEM_DIR / f"{key}.npz"
    if npz.exists():
        grid = np.load(npz)["z"]
    else:
        tiles = _tiles_for_extent(ax0, ay0, ax1, ay1, origin)
        bounds = (ax0 - cell / 2, ay0 - cell / 2, ax1 + cell / 2, ay1 + cell / 2)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(min(8, len(tiles))) as ex:
            parts = list(ex.map(lambda rc: _read_tile_window(url_tmpl.format(r=rc[0], c=rc[1]), bounds, (ny, nx)), tiles))
        grid = np.full((ny, nx), np.nan, dtype="f4")
        for p in parts:
            if p is not None:
                m = np.isfinite(p) & ~np.isfinite(grid)
                grid[m] = p[m]
        grid[(grid < H_MIN) | (grid > H_MAX)] = np.nan
        if not np.isfinite(grid).any():
            return None
        np.savez_compressed(npz, z=grid)
    z = np.flipud(grid).astype("f8") - z0     # row 0 = south, to match scene surface layout
    note = (f"{source}: multi-year mosaic DEM, WGS84-ellipsoid heights on a {cell:.0f} m grid; decimetre-metre "
            f"offsets from any single ICESat-2 pass are expected and are NOT the comparison signal. {attr}")
    return _finish(z, x0, y0, cell, nx, ny, source, attr, note)


# --- lon/lat sampling (Copernicus + geoid): DEM is EPSG:4326, frame is aeqd -----------------------------------------
def _sample_ll(url: str, lon: np.ndarray, lat: np.ndarray, margin=0.02) -> np.ndarray:
    """Bilinear-sample a lon/lat raster (band 1) at arbitrary (lon, lat) points. One windowed COG read, capped to
    MAX_READ_PX on the long side. Points outside the raster (or a 404 tile) come back NaN."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds
    from scipy.interpolate import RegularGridInterpolator

    lon = np.asarray(lon, "f8"); lat = np.asarray(lat, "f8")
    lo0, lo1 = float(lon.min()) - margin, float(lon.max()) + margin
    la0, la1 = float(lat.min()) - margin, float(lat.max()) + margin
    out = np.full(lon.shape, np.nan)
    with rasterio.Env(**_ENV), rasterio.open(url) as s:
        b = s.bounds
        lo0, lo1 = max(lo0, b.left), min(lo1, b.right)
        la0, la1 = max(la0, b.bottom), min(la1, b.top)
        if lo0 >= lo1 or la0 >= la1:
            return out
        win = from_bounds(lo0, la0, lo1, la1, transform=s.transform)
        fr, fc = max(1, int(round(win.height))), max(1, int(round(win.width)))
        sc = min(1.0, MAX_READ_PX / max(fr, fc))
        orows, ocols = max(1, int(fr * sc)), max(1, int(fc * sc))
        arr = s.read(1, window=win, out_shape=(orows, ocols), resampling=Resampling.bilinear, masked=True).astype("f8").filled(np.nan)
        wt = s.window_transform(win)
        sx, sy = wt.a * fc / ocols, wt.e * fr / orows            # pixel size after out_shape scaling (sy < 0)
        xs = wt.c + (np.arange(ocols) + 0.5) * sx                # lon ascending
        ys = wt.f + (np.arange(orows) + 0.5) * sy                # lat descending
        arr[(arr < H_MIN) | (arr > H_MAX)] = np.nan               # reject stray nodata (leaves geoid N, |N|<110, intact)
        interp = RegularGridInterpolator((ys[::-1], xs), arr[::-1, :], bounds_error=False, fill_value=np.nan)
        out = interp(np.column_stack([lat.ravel(), lon.ravel()])).reshape(lon.shape)
    return out


def _cop_url(lo_i: int, la_i: int) -> str:
    ns = "N" if la_i >= 0 else "S"
    ew = "E" if lo_i >= 0 else "W"
    return COP_URL.format(ns=ns, la=abs(la_i), ew=ew, lo=abs(lo_i))


def _copernicus_H(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Orthometric (EGM2008) height from GLO-30, merged across the 1-deg tiles the points span. NaN over absent
    (ocean) tiles."""
    H = np.full(lon.shape, np.nan)
    la_lo, la_hi = int(math.floor(lat.min())), int(math.floor(lat.max()))
    lo_lo, lo_hi = int(math.floor(lon.min())), int(math.floor(lon.max()))
    for la_i in range(la_lo, la_hi + 1):
        for lo_i in range(lo_lo, lo_hi + 1):
            try:
                h = _sample_ll(_cop_url(lo_i, la_i), lon, lat)
            except Exception as e:
                _log_absent(_cop_url(lo_i, la_i), e)
                continue
            m = ~np.isfinite(H) & np.isfinite(h)
            H[m] = h[m]
    return H


def _geoid_N(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """EGM2008 undulation N (m, geoid above ellipsoid) at lon/lat, from the NGA egm08 grid via /vsicurl."""
    return _sample_ll(EGM08_URL, lon, lat, margin=0.1)


def _copernicus_grid(frame, extent, z0, cell_m) -> dict | None:
    from pyproj import Transformer
    x0, y0, x1, y1 = extent
    nx, ny, cell, ax, ay = _grid_local(frame, extent, cell_m)
    gx, gy = np.meshgrid(ax, ay)                                  # absolute projected coords (frame CRS)
    to_ll = Transformer.from_crs(frame["crs"], "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(gx, gy)
    lon = np.asarray(lon); lat = np.asarray(lat)
    key = hashlib.sha1(f"COP|{lon.min():.4f},{lat.min():.4f},{lon.max():.4f},{lat.max():.4f},{nx}x{ny}".encode()).hexdigest()[:16]
    DEM_DIR.mkdir(parents=True, exist_ok=True)
    npz = DEM_DIR / f"{key}.npz"
    if npz.exists():
        h_ell = np.load(npz)["z"]
    else:
        H = _copernicus_H(lon, lat)
        if not np.isfinite(H).any():
            return None
        N = _geoid_N(lon, lat)
        h_ell = (H + N).astype("f4")                             # ellipsoidal, matches ATL03
        np.savez_compressed(npz, z=h_ell)
    z = h_ell.astype("f8") - z0                                  # row 0 = south already (ay ascends north)
    note = (f"Copernicus GLO-30: global 30 m DEM, converted from EGM2008 orthometric to WGS84-ellipsoid heights by "
            f"adding the NGA egm08 geoid undulation, on a {cell:.0f} m grid; single-pass offsets are expected and are "
            f"NOT the comparison signal. {COP_ATTR}")
    return _finish(z, x0, y0, cell, nx, ny, "Copernicus GLO-30", COP_ATTR, note)


# --- dispatch --------------------------------------------------------------------------------------------------------
def surface_for_frame(frame: dict, extent: tuple[float, float, float, float], z0: float, cell_m: float = 100.0) -> dict | None:
    """Ellipsoidal-height DEM on the scene's local grid (same dict shape as before; z relative to z0). Chooses the
    DEM by the frame CRS. Returns None where no DEM covers the scene (caller then shows no surface)."""
    crs = frame.get("crs")
    if crs == "EPSG:3413":
        return _polar_grid(frame, extent, z0, cell_m, ARCTIC_URL, ARCTIC_ORIGIN, "ArcticDEM v4.1 32m", ARCTIC_ATTR)
    if crs == "EPSG:3031":
        return _polar_grid(frame, extent, z0, cell_m, REMA_URL, REMA_ORIGIN, "REMA v2.0 32m", REMA_ATTR)
    return _copernicus_grid(frame, extent, z0, cell_m)


def slope_deg(surface: dict | None, x: np.ndarray, y: np.ndarray) -> float | None:
    """Median DEM slope (degrees) at local (x, y) positions, from central differences on the surface grid.
    Works for any real DEM surface (`is_dem`), not the retired photon-interpolated grid."""
    if not surface or not surface.get("is_dem"):
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
