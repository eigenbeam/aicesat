"""Imagery base layer: EOX Sentinel-2 cloudless WMTS tiles (Web Mercator) mosaicked and warped into the scene's local
EPSG:3413 frame, saved as one JPEG the widget drapes on the surface mesh. Cached by (extent, layer, zoom).

Licence: EOX "Sentinel-2 cloudless" is CC BY-NC-SA 4.0 (https://s2maps.eu) — attribution is shown in the widget.
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

from . import cache

log = logging.getLogger(__name__)

LAYER = "s2cloudless-2020_3857"
TILE_URL = "https://tiles.maps.eox.at/wmts/1.0.0/{layer}/default/g/{z}/{y}/{x}.jpg"
ATTRIBUTION = "Sentinel-2 cloudless 2020 by EOX IT Services GmbH (CC BY-NC-SA 4.0), contains modified Copernicus Sentinel data"
MAX_ZOOM, MAX_TILES = 13, 600
_ps_to_ll = Transformer.from_crs("EPSG:3413", "EPSG:4326", always_xy=True)
IMG_DIR = cache.DATA_DIR / "cache" / "imagery"


def _merc(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat = np.clip(lat, -85.05, 85.05)
    y = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / math.pi) / 2 * n
    return x, y  # in tile units


def build(frame: dict, extent: tuple[float, float, float, float], width_px: int = 2048, layer: str = LAYER) -> dict:
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
    lat_c = _ps_to_ll.transform(ox + (x0 + x1) / 2, oy + (y0 + y1) / 2)[1]
    z = int(min(MAX_ZOOM, max(3, round(math.log2(156543.03 * math.cos(math.radians(lat_c)) / m_per_px)))))
    meta.update({"zoom": z, "m_per_px": m_per_px, "width": w, "height": h})
    if out.exists():
        log.info("imagery cache hit %s", out.name)
        return meta
    # target grid -> lon/lat -> mercator tile coords
    xs = ox + x0 + (np.arange(w) + 0.5) * m_per_px
    ys = oy + y1 - (np.arange(h) + 0.5) * m_per_px  # row 0 = north (top)
    gx, gy = np.meshgrid(xs, ys)
    lon, lat = _ps_to_ll.transform(gx.ravel(), gy.ravel())
    tx, ty = _merc(np.asarray(lon), np.asarray(lat), z)
    txi, tyi = np.floor(tx).astype(int), np.floor(ty).astype(int)
    tiles = sorted(set(zip(txi.tolist(), tyi.tolist())))
    if len(tiles) > MAX_TILES:  # too fine: back off one zoom level and recurse
        return build(frame, extent, width_px // 2, layer)
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
