"""GIA (glacial isostatic adjustment) present-day vertical bedrock motion — the vertical counterpart to the plate-
motion correction. `uplift_rate_mm_yr(lon, lat)` returns the radial uplift rate (mm/yr, positive = up) from the
vendored ICE-6G_C (VM5a) 1° grid (data/gia_ice6g_c_vm5a.npz; rebuild with scripts/vendor_gia.py). GIA is smooth, so
the 1° grid is bilinearly interpolated. No network at runtime.

Citation: Peltier, Argus & Drummond (2015), JGR Solid Earth 120, 450-487, doi:10.1002/2014JB011176.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

_NPZ = Path(__file__).parent / "data" / "gia_ice6g_c_vm5a.npz"
_DEFAULT_MODEL = "ICE-6G_C (VM5a)"
_DEFAULT_CITATION = "Peltier, Argus & Drummond (2015), JGR Solid Earth, doi:10.1002/2014JB011176"


@lru_cache(maxsize=1)
def _load():
    """(interpolator over (lat, lon) with a wrapped lon seam, model, citation). Cached."""
    from scipy.interpolate import RegularGridInterpolator

    d = np.load(_NPZ, allow_pickle=False)
    lat = d["lat"].astype("f8")            # ascending -89.5..89.5
    lon = d["lon"].astype("f8")            # 0..359
    rate = d["rate"].astype("f8")          # (lat, lon), mm/yr, + = uplift
    lon2 = np.concatenate([lon, [lon[0] + 360.0]])           # wrap column so the antimeridian seam interpolates
    rate2 = np.concatenate([rate, rate[:, :1]], axis=1)
    interp = RegularGridInterpolator((lat, lon2), rate2, bounds_error=False, fill_value=np.nan)
    return interp, str(d["model"]), str(d["citation"])


def _meta():
    try:
        _, model, citation = _load()
        return model, citation
    except Exception:
        return _DEFAULT_MODEL, _DEFAULT_CITATION


MODEL, CITATION = _meta()


def uplift_rate_mm_yr(lon, lat) -> np.ndarray:
    """Present-day GIA radial uplift rate (mm/yr, + = up) at lon/lat arrays. NaN outside the grid."""
    interp, _, _ = _load()
    lon = np.asarray(lon, "f8")
    lat = np.asarray(lat, "f8")
    lo = np.mod(lon, 360.0)                 # accept -180..180 or 0..360
    la = np.clip(lat, -89.5, 89.5)
    out = interp(np.column_stack([la.ravel(), lo.ravel()]))
    return out.reshape(lon.shape)
