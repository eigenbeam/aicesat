"""GLAS laser operational periods (NSIDC laseroperationalperiods.pdf; dates are UTC calendar days)."""
from __future__ import annotations

from datetime import date

GLAS_CAMPAIGNS: list[tuple[str, date, date]] = [
    ("L1A", date(2003, 2, 20), date(2003, 3, 21)),
    ("L1B", date(2003, 3, 21), date(2003, 3, 29)),
    ("L2A", date(2003, 9, 25), date(2003, 11, 19)),
    ("L2B", date(2004, 2, 17), date(2004, 3, 21)),
    ("L2C", date(2004, 5, 18), date(2004, 6, 21)),
    ("L3A", date(2004, 10, 3), date(2004, 11, 8)),
    ("L3B", date(2005, 2, 17), date(2005, 3, 24)),
    ("L3C", date(2005, 5, 20), date(2005, 6, 23)),
    ("L3D", date(2005, 10, 21), date(2005, 11, 24)),
    ("L3E", date(2006, 2, 22), date(2006, 3, 28)),
    ("L3F", date(2006, 5, 24), date(2006, 6, 26)),
    ("L3G", date(2006, 10, 25), date(2006, 11, 27)),
    ("L3H", date(2007, 3, 12), date(2007, 4, 14)),
    ("L3I", date(2007, 10, 2), date(2007, 11, 5)),
    ("L3J", date(2008, 2, 17), date(2008, 3, 21)),
    ("L3K", date(2008, 10, 4), date(2008, 10, 19)),
    ("L2D", date(2008, 11, 25), date(2008, 12, 17)),
    ("L2E", date(2009, 3, 9), date(2009, 4, 11)),
    ("L2F", date(2009, 9, 30), date(2009, 10, 11)),
]


def campaign_for(d: date) -> str:
    for name, a, b in GLAS_CAMPAIGNS:
        if a <= d <= b:
            return name
    return "unknown"
