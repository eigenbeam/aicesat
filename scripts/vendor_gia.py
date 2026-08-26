"""Vendor the ICE-6G_C (VM5a) present-day radial-uplift-rate grid as a compact npz for the GIA correction.

Source: W.R. Peltier group, University of Toronto — datasets/Ice6G_C_VM5a_O512/drad.1grid_O512.nc
(variable `Drad_250`, mm/yr, positive = radial uplift; 1°×1°, Lon 0..359, Lat 89.5..-89.5).
Run once; the npz (~150 KB) is committed so the server never needs this host at runtime.

Citation: Peltier, W.R., Argus, D.F., Drummond, R. (2015). Space geodesy constrains ice age terminal
deglaciation: the global ICE-6G_C (VM5a) model. JGR Solid Earth 120, 450-487, doi:10.1002/2014JB011176.
"""
import io
import ssl
import urllib.request
from pathlib import Path

import numpy as np
from scipy.io import netcdf_file

URL = "https://www.atmosp.physics.utoronto.ca/~peltier/datasets/Ice6G_C_VM5a_O512/drad.1grid_O512.nc"
OUT = Path(__file__).resolve().parents[1] / "src" / "aicesat" / "data" / "gia_ice6g_c_vm5a.npz"
CITATION = ("Peltier, W.R., Argus, D.F., Drummond, R. (2015), Space geodesy constrains ice age terminal "
            "deglaciation: the global ICE-6G_C (VM5a) model, JGR Solid Earth 120, 450-487, doi:10.1002/2014JB011176")


def main():
    try:
        raw = urllib.request.urlopen(URL, timeout=90).read()
    except Exception:  # the academic host ships an incomplete cert chain; allow unverified for this public-data fetch
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        raw = urllib.request.urlopen(URL, timeout=90, context=ctx).read()
    ds = netcdf_file(io.BytesIO(raw), mmap=False)
    lon = ds.variables["Lon"][:].astype("f4")           # 0..359
    lat = ds.variables["Lat"][:].astype("f4")           # 89.5..-89.5 (descending)
    rate = ds.variables["Drad_250"][:].astype("f4")     # (lat, lon), mm/yr, + = uplift
    order = np.argsort(lat)                              # store lat ascending for RegularGridInterpolator
    lat, rate = lat[order], rate[order]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT, lon=lon, lat=lat, rate=rate,
                        model="ICE-6G_C (VM5a)", units="mm/yr", sign="positive = radial uplift",
                        citation=CITATION, source_url=URL, variable="Drad_250")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes), rate range "
          f"[{rate.min():.2f}, {rate.max():.2f}] mm/yr on {rate.shape} grid")


if __name__ == "__main__":
    main()
