"""Extract strictly PHYSICAL place names for the UI from GeoNames country dumps (CC BY 4.0, geonames.org; GeoNames
incorporates the NGA GEOnet Names Server). Filtered by feature code, so no political or populated place can enter:
glaciers, ice caps, fjords, bays, straits, islands, nunataks, mountains and peaks only.

usage: uv run scripts/build_geonames.py GL.txt NP.txt   (unzipped from https://download.geonames.org/export/dump/)
writes src/aicesat/ui/geonames_data.js
"""
import json
import sys
from pathlib import Path

# feature code -> label rank (lower draws first and wins label collisions)
CODES = {"CAPG": 0, "GLCR": 1, "FJD": 2, "BAY": 3, "STRT": 3, "SD": 3, "ISL": 4, "PK": 4, "MTS": 4, "NTK": 5, "MT": 5}
# Regions the demo stores data for: West Greenland (plus the ice cap's label point) and the Langtang area of Nepal.
BOXES = [(-56.0, 66.0, -40.0, 74.0), (84.5, 27.5, 86.5, 29.0)]
OUT = Path(__file__).resolve().parents[1] / "src" / "aicesat" / "ui" / "geonames_data.js"


def inside(lat, lon):
    return any(w <= lon <= e and s <= lat <= n for w, s, e, n in BOXES)


rows = []
for path in sys.argv[1:]:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        if len(f) < 9 or f[6] not in ("H", "T") or f[7] not in CODES:
            continue
        lat, lon = float(f[4]), float(f[5])
        if f[7] == "CAPG" or inside(lat, lon):
            rows.append([f[1], round(lat, 4), round(lon, 4), f[7], CODES[f[7]], int(f[0])])
rows.sort(key=lambda r: (r[4], r[0]))
OUT.write_text("/* Physical place names from GeoNames (geonames.org, CC BY 4.0), filtered by feature code in\n"
               "   scripts/build_geonames.py -- no political or populated places. [name, lat, lon, code, rank, geonameid] */\n"
               "window.AICESAT = window.AICESAT || {};\nAICESAT.GEONAMES = " + json.dumps(rows, ensure_ascii=False) + ";\n",
               encoding="utf-8")
print(f"{len(rows)} names -> {OUT}", {c: sum(r[3] == c for r in rows) for c in CODES})
