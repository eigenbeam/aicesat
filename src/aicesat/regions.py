"""Candidate demo regions (spec Appendix D). bbox = (west, south, east, north) in degrees.

All boxes are PLACEHOLDERS pending collaborator validation for slope / coverage / slow flow.
"""
from __future__ import annotations

BBox = tuple[float, float, float, float]

REGIONS: dict[str, dict] = {
    "egig_west_flank": {
        "bbox": (-45.0, 69.8, -43.0, 70.2),
        "note": "Candidate 2: EGIG line, central-west flank (~70N). Tunable slope; confirm both missions cross.",
    },
    "k_transect_upstream": {
        "bbox": (-48.5, 66.9, -46.5, 67.3),
        "note": "Candidate 3: K-transect, upstream of the fast/ablation zone (~67N).",
    },
    "summit": {
        "bbox": (-39.5, 72.4, -37.5, 72.8),
        "note": "Candidate 1: near Summit Station. Cleanest interior; slope may be too gentle.",
    },
    "ne_flank_upstream_negis": {
        "bbox": (-32.0, 75.5, -29.0, 76.0),
        "note": "Candidate 4: NE interior flank, well upstream of NEGIS.",
    },
    "jakobshavn_margin": {
        "bbox": (-51.0, 69.0, -49.0, 69.4),
        "note": "Widget/relief showcase, NOT a Demo-B region: Jakobshavn Isbrae trunk + Isfjord. Fast-flowing dynamic ice; "
                "co-registration numbers here are contaminated by ice flow and real thinning (spec B.9 / A3).",
    },
    "n_central_flank": {
        "bbox": (-45.0, 75.8, -42.0, 76.2),
        "note": "Candidate 5: north-central interior flank (least established site).",
    },
}

DEFAULT_REGION = "egig_west_flank"

# Default time windows: the FULL record of each collection, so "all granules" pulls everything available
# (needed for a long elevation time series). Each spans its mission's operating period.
DEFAULT_GLAS_WINDOW = ("2003-02-20", "2009-10-11")    # ICESat-1 / GLAS campaigns (whole mission)
DEFAULT_ICESSN_WINDOW = ("2009-01-01", "2019-12-31")  # Operation IceBridge ATM, fills the ICESat -> ICESat-2 gap
DEFAULT_ATL06_WINDOW = ("2018-10-01", "2027-01-01")   # ICESat-2 land ice, full record to present
DEFAULT_ATL03_WINDOW = ("2018-10-01", "2027-01-01")   # ICESat-2 photons, full record to present


def resolve_bbox(region: str | None = None, bbox: BBox | None = None) -> BBox:
    if bbox is not None:
        w, s, e, n = map(float, bbox)
        if not (w < e and s < n):
            raise ValueError(f"bad bbox {bbox}: need west<east and south<north")
        return (w, s, e, n)
    return REGIONS[region or DEFAULT_REGION]["bbox"]
