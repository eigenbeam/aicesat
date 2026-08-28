"""Imagery base layer for a scene, warped into the scene's local frame (see scene.frame_crs) and saved as one JPEG
the widget drapes on the surface mesh. Two selectable sources (env AICESAT_IMAGERY):

  * "eox" (DEFAULT): pre-rendered RGB tiles from EOX "Sentinel-2 cloudless 2020" WMTS (Web Mercator), mosaicked and
    resampled. One external EU server (tiles.maps.eox.at) — the only cross-network hop in scene creation.
  * "s2": in-region true-colour composite from Sentinel-2 L2A COGs in AWS us-west-2 (Element84 Earth Search STAC +
    the `sentinel-cogs` Open Data bucket). Picks the least-cloudy recent scene, reads B04/B03/B02 with rasterio
    windowed reads, reprojects into the frame and stretches to 8-bit RGB. Scales like the rest of the pipeline when
    the app runs in-region; no cross-Atlantic tile fetch.

Both produce the SAME on-disk + metadata contract (JPEG at `path`; meta {path, x0,y0,x1,y1, zoom, m_per_px, width,
height, attribution, source}) so scene.add_imagery and the widget consume either unchanged. Cached by source-tagged
(extent, layer/scene, width).

Licences: EOX "Sentinel-2 cloudless" is CC BY-NC-SA 4.0 (https://s2maps.eu). Sentinel-2 L2A is modified Copernicus
Sentinel data (ESA), free and open; distributed via the AWS sentinel-cogs Open Data bucket. Attribution is shown in
the widget.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

from . import cache

log = logging.getLogger(__name__)

# --- EOX Sentinel-2 cloudless WMTS (external, default) --------------------------------------------------------------
LAYER = "s2cloudless-2020_3857"
TILE_URL = "https://tiles.maps.eox.at/wmts/1.0.0/{layer}/default/g/{z}/{y}/{x}.jpg"
ATTRIBUTION = "Sentinel-2 cloudless 2020 by EOX IT Services GmbH (CC BY-NC-SA 4.0), contains modified Copernicus Sentinel data"
MAX_ZOOM, MAX_TILES = 13, 600


@lru_cache(maxsize=64)
def _to_ll(crs: str) -> Transformer:
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
IMG_DIR = cache.DATA_DIR / "cache" / "imagery"


def _merc(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat = np.clip(lat, -85.05, 85.05)
    y = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / math.pi) / 2 * n
    return x, y  # in tile units


def build(frame: dict, extent: tuple[float, float, float, float], width_px: int = 2048, layer: str = LAYER) -> dict:
    """Base-layer imagery for the scene. Dispatches on env AICESAT_IMAGERY: "s2" -> in-region Sentinel-2 L2A COGs
    (us-west-2), anything else (default "eox") -> EOX Sentinel-2 cloudless WMTS. Both return the same meta contract:
    {path, x0, y0, x1, y1, zoom, m_per_px, width, height, attribution, source}."""
    src = os.environ.get("AICESAT_IMAGERY", "eox").strip().lower()
    if src == "s2":
        return _build_s2(frame, extent, width_px)
    return _build_eox(frame, extent, width_px, layer)


def _build_eox(frame: dict, extent: tuple[float, float, float, float], width_px: int = 2048, layer: str = LAYER) -> dict:
    """extent = (x0, y0, x1, y1) in local metres. Returns {path, x0, y0, x1, y1, zoom, m_per_px, attribution, source}."""
    x0, y0, x1, y1 = extent
    key = hashlib.sha1(f"{layer}|{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}|{width_px}|{frame['origin_xy']}".encode()).hexdigest()[:16]
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    out = IMG_DIR / f"{key}.jpg"
    meta = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "attribution": ATTRIBUTION, "source": layer, "path": str(out)}
    ox, oy = frame["origin_xy"]
    w = width_px
    h = max(64, int(round(w * (y1 - y0) / (x1 - x0))))
    m_per_px = (x1 - x0) / w
    # zoom so that the mercator pixel size at this latitude roughly matches the target pixel size
    to_ll = _to_ll(frame["crs"])
    lat_c = to_ll.transform(ox + (x0 + x1) / 2, oy + (y0 + y1) / 2)[1]
    z = int(min(MAX_ZOOM, max(3, round(math.log2(156543.03 * math.cos(math.radians(lat_c)) / m_per_px)))))
    meta.update({"zoom": z, "m_per_px": m_per_px, "width": w, "height": h})
    if out.exists():
        log.info("imagery cache hit %s", out.name)
        return meta
    # target grid -> lon/lat -> mercator tile coords
    xs = ox + x0 + (np.arange(w) + 0.5) * m_per_px
    ys = oy + y1 - (np.arange(h) + 0.5) * m_per_px  # row 0 = north (top)
    gx, gy = np.meshgrid(xs, ys)
    lon, lat = to_ll.transform(gx.ravel(), gy.ravel())
    tx, ty = _merc(np.asarray(lon), np.asarray(lat), z)
    txi, tyi = np.floor(tx).astype(int), np.floor(ty).astype(int)
    tiles = sorted(set(zip(txi.tolist(), tyi.tolist())))
    if len(tiles) > MAX_TILES:  # too fine: back off one zoom level and recurse
        return _build_eox(frame, extent, width_px // 2, layer)
    session = requests.Session()

    def fetch(t):
        r = session.get(TILE_URL.format(layer=layer, z=z, x=t[0], y=t[1]), timeout=60)
        r.raise_for_status()
        return t, np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))

    with ThreadPoolExecutor(8) as ex:
        imgs = dict(ex.map(fetch, tiles))
    # sample nearest pixel from each tile
    px = ((tx - txi) * 256).astype(int).clip(0, 255)
    py = ((ty - tyi) * 256).astype(int).clip(0, 255)
    rgb = np.zeros((w * h, 3), dtype="u1")
    for t, img in imgs.items():
        m = (txi == t[0]) & (tyi == t[1])
        rgb[m] = img[py[m], px[m]]
    Image.fromarray(rgb.reshape(h, w, 3)).save(out, quality=88)
    log.info("imagery: %d tiles at z%d -> %dx%d (%.1f m/px) %s", len(tiles), z, w, h, m_per_px, out.name)
    return meta


# --- In-region Sentinel-2 L2A (Element84 Earth Search STAC + sentinel-cogs COGs, AWS us-west-2) ----------------------
# The sentinel-cogs Open Data bucket is public and free (no requester-pays), so reads are unsigned: in-region a direct
# /vsis3 range read (no HTTPS/CloudFront hop, no egress), out-of-region a /vsicurl HTTPS read of the same object.
STAC_URL = "https://earth-search.aws.element84.com/v1/search"
S2_COLLECTION = "sentinel-2-l2a"
S2_BANDS = ("red", "green", "blue")            # STAC asset keys for B04 / B03 / B02 (10 m true-colour)
S2_ATTR = "Contains modified Copernicus Sentinel-2 L2A data (ESA), free & open; via Element84 Earth Search + AWS sentinel-cogs Open Data"
S2_MAX_READ_PX = 2048                           # cap a single COG window read so a large box can't blow up memory
S2_CLOUD_MAX = float(os.environ.get("AICESAT_S2_CLOUD_MAX", "20"))       # max eo:cloud_cover (%) accepted
S2_MONTHS = float(os.environ.get("AICESAT_S2_MONTHS", "24"))            # look back this many months for a scene
S2_MIN_SUN_ELEV = float(os.environ.get("AICESAT_S2_MIN_SUN_ELEV", "10"))  # skip polar low-sun / near-dark scenes
_S2_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
               GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", VSI_CACHE="TRUE", GDAL_HTTP_MAX_RETRY="3",
               GDAL_HTTP_RETRY_DELAY="1", AWS_NO_SIGN_REQUEST="YES")


def _s2_search(bbox_ll: tuple[float, float, float, float], months: float, cloud_max: float, limit: int = 20) -> list[dict]:
    """POST the Earth Search STAC /search for L2A scenes intersecting bbox_ll (lon0,lat0,lon1,lat1) over the last
    `months`, cloud < cloud_max, sorted by cloud cover ascending. Public API — works from anywhere."""
    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(days=months * 30.5)
    body = {
        "collections": [S2_COLLECTION],
        "bbox": list(bbox_ll),
        "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        "query": {"eo:cloud_cover": {"lt": cloud_max}},
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
        "limit": limit,
    }
    r = requests.post(STAC_URL, json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("features", [])


def _s2_pick(items: list[dict], bbox_ll: tuple[float, float, float, float]) -> dict | None:
    """From cloud-sorted items, the least-cloudy scene that (1) is well-lit (sun elevation ok, when known) and
    (2) fully contains the query bbox, backing off each preference if none qualifies. v1 = single scene."""
    def lit(it):
        return it["properties"].get("view:sun_elevation", 90.0) >= S2_MIN_SUN_ELEV

    def covers(it):
        b = it.get("bbox")
        return bool(b) and b[0] <= bbox_ll[0] and b[1] <= bbox_ll[1] and b[2] >= bbox_ll[2] and b[3] >= bbox_ll[3]

    pool = [it for it in items if lit(it)] or items
    chosen = [it for it in pool if covers(it)] or pool
    return chosen[0] if chosen else None       # items are already sorted by cloud cover ascending


def _href_for_read(href: str) -> str:
    """Read URL for a sentinel-cogs asset: in-region a direct unsigned /vsis3 read, else the plain HTTPS URL
    (GDAL opens it via /vsicurl). sentinel-cogs is a public, free-egress bucket, so no credentials either way."""
    from . import access
    if access.in_region() and ".amazonaws.com/" in href and "sentinel-cogs" in href:
        key = href.split(".amazonaws.com/", 1)[1]
        return f"/vsis3/sentinel-cogs/{key}"
    return href


def _sample_utm(url: str, ux: np.ndarray, uy: np.ndarray) -> np.ndarray:
    """Bilinear-sample band 1 of a UTM COG at projected (ux, uy) easting/northing. One windowed read, capped to
    S2_MAX_READ_PX on the long side; points outside the raster come back NaN. Mirrors dem._sample_ll but in UTM."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds
    from scipy.interpolate import RegularGridInterpolator

    out = np.full(ux.shape, np.nan)
    fin = np.isfinite(ux) & np.isfinite(uy)
    if not fin.any():
        return out
    x0, x1 = float(ux[fin].min()), float(ux[fin].max())
    y0, y1 = float(uy[fin].min()), float(uy[fin].max())
    with rasterio.Env(**_S2_ENV), rasterio.open(url) as s:
        b = s.bounds
        x0, x1 = max(x0, b.left), min(x1, b.right)
        y0, y1 = max(y0, b.bottom), min(y1, b.top)
        if x0 >= x1 or y0 >= y1:
            return out
        win = from_bounds(x0, y0, x1, y1, transform=s.transform)
        fr, fc = max(1, int(round(win.height))), max(1, int(round(win.width)))
        sc = min(1.0, S2_MAX_READ_PX / max(fr, fc))
        orows, ocols = max(1, int(fr * sc)), max(1, int(fc * sc))
        arr = s.read(1, window=win, out_shape=(orows, ocols), resampling=Resampling.bilinear, masked=True).astype("f8").filled(np.nan)
        wt = s.window_transform(win)
        sx, sy = wt.a * fc / ocols, wt.e * fr / orows            # pixel size after out_shape scaling (sy < 0)
        xs = wt.c + (np.arange(ocols) + 0.5) * sx                # easting ascending
        ys = wt.f + (np.arange(orows) + 0.5) * sy                # northing descending
        interp = RegularGridInterpolator((ys[::-1], xs), arr[::-1, :], bounds_error=False, fill_value=np.nan)
        out[fin] = interp(np.column_stack([uy[fin], ux[fin]]))
    return out


def _stretch_rgb(r: np.ndarray, g: np.ndarray, b: np.ndarray, lo_pct: float = 2.0, hi_pct: float = 98.0,
                 gamma: float = 1.35) -> np.ndarray:
    """Flat (N,) reflectance bands -> (N,3) uint8 true colour. A percentile linear stretch pooled across the three
    bands (one lo/hi for all, so hue is preserved) plus a mild gamma lift. NaN pixels (outside the scene) -> black."""
    stack = np.stack([r, g, b], axis=-1).astype("f8")
    valid = np.isfinite(stack).all(axis=-1)
    if not valid.any():
        raise RuntimeError("Sentinel-2 scene did not cover the requested area")
    lo, hi = np.percentile(stack[valid], [lo_pct, hi_pct])
    if hi <= lo:
        hi = lo + 1.0
    scaled = np.clip((stack - lo) / (hi - lo), 0.0, 1.0)
    scaled = np.power(scaled, 1.0 / gamma)
    scaled[~valid] = 0.0                        # zero NaN pixels before the cast (NaN->u1 is undefined)
    rgb = (scaled * 255.0 + 0.5).astype("u1")
    rgb[~valid] = 0
    return rgb


def _build_s2(frame: dict, extent: tuple[float, float, float, float], width_px: int = 2048) -> dict:
    """In-region Sentinel-2 L2A true-colour composite for the scene, same contract as _build_eox. Reads B04/B03/B02
    from the least-cloudy recent COG scene and warps it into the scene frame. Raises if no usable scene is found
    (scene.add_imagery then degrades to no imagery, exactly as an EOX failure does)."""
    x0, y0, x1, y1 = extent
    ox, oy = frame["origin_xy"]
    w = width_px
    h = max(64, int(round(w * (y1 - y0) / (x1 - x0))))
    m_per_px = (x1 - x0) / w
    key = hashlib.sha1(f"s2|{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}|{w}|{frame['origin_xy']}|{frame['crs']}".encode()).hexdigest()[:16]
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    out = IMG_DIR / f"{key}.jpg"
    side = out.with_suffix(".json")
    base = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "zoom": 0, "m_per_px": m_per_px, "width": w, "height": h,
            "path": str(out), "attribution": S2_ATTR, "source": "Sentinel-2 L2A"}
    if out.exists():
        log.info("imagery(s2) cache hit %s", out.name)
        if side.exists():
            try:
                return {**base, **json.loads(side.read_text())}
            except Exception:
                pass
        return base

    # Output-pixel-centre grid in absolute frame coords (row 0 = north/top), then its lon/lat span for the STAC query.
    xs = ox + x0 + (np.arange(w) + 0.5) * m_per_px
    ys = oy + y1 - (np.arange(h) + 0.5) * m_per_px
    gx, gy = np.meshgrid(xs, ys)
    gxr, gyr = gx.ravel(), gy.ravel()
    lon, lat = _to_ll(frame["crs"]).transform(gxr, gyr)
    lon, lat = np.asarray(lon), np.asarray(lat)
    bbox_ll = (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))

    items = _s2_search(bbox_ll, S2_MONTHS, S2_CLOUD_MAX)
    item = _s2_pick(items, bbox_ll)
    if item is None:
        raise RuntimeError(f"no Sentinel-2 L2A scene < {S2_CLOUD_MAX:.0f}% cloud with sun > {S2_MIN_SUN_ELEV:.0f}° "
                           f"over {bbox_ll} in the last {S2_MONTHS:.0f} months")
    epsg = int(item["properties"]["proj:epsg"])
    cloud = item["properties"].get("eo:cloud_cover")
    dt = (item["properties"].get("datetime") or "")[:10]

    # Reproject the output grid from the scene frame into the chosen scene's UTM CRS, then sample each RGB band there.
    ux, uy = Transformer.from_crs(frame["crs"], f"EPSG:{epsg}", always_xy=True).transform(gxr, gyr)
    ux, uy = np.asarray(ux), np.asarray(uy)
    urls = [_href_for_read(item["assets"][k]["href"]) for k in S2_BANDS]
    with ThreadPoolExecutor(3) as ex:
        bands = list(ex.map(lambda u: _sample_utm(u, ux, uy), urls))
    rgb = _stretch_rgb(*bands).reshape(h, w, 3)
    Image.fromarray(rgb).save(out, quality=88)

    src_desc = f"Sentinel-2 L2A {item['id']}" + (f" ({dt}, {cloud:.0f}% cloud)" if cloud is not None else f" ({dt})")
    meta = {**base, "source": src_desc, "attribution": f"{S2_ATTR}; scene {item['id']} {dt}"}
    try:
        side.write_text(json.dumps({"source": meta["source"], "attribution": meta["attribution"]}))
    except Exception:
        pass
    log.info("imagery(s2): %s cloud=%.1f%% epsg=%d -> %dx%d (%.1f m/px) %s",
             item["id"], cloud if cloud is not None else -1.0, epsg, w, h, m_per_px, out.name)
    return meta
