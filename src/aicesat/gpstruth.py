"""IS2TGPSSS — ICESat/ICESat-2 Traverse: monthly kinematic-GPS surface elevation at Summit Station, Greenland.

Ground truth, not altimetry: a survey sled towed along a ~15 km transect west of Summit that is deliberately
aligned with ICESat-2 reference ground tracks (RGT 749 ascending, RGT 879 descending), driven monthly since
2006. Heights are **uncorrected for GIA or any other geophysical effect**, which is exactly what makes them
comparable to the native (also uncorrected) altimetry heights this repo stores.

Positions are ITRF at the epoch of the survey on the **GRS80** ellipsoid (EPSG:7019); GRS80 and WGS84 ellipsoid
heights differ by well under a millimetre, so no datum conversion is applied and none is needed.

The CSV's `antenna_hae_m` is the GPS **antenna** height, not the snow surface. User-guide Eq. 1:

    surface_elevation = antenna_hae_m - arp_to_sled_base + Ztrack

where `Ztrack` is the sled sinkage and `arp_to_sled_base` is the antenna-reference-point height above the sled
bottom. Both live in a separate tab-delimited metadata file keyed by RINEX filename, and both are irregular in
the early record -- see `_load_traverse_metadata` for the three key forms and `SLED_*` for how gaps are filled.

Format: NSIDC IS2TGPSSS v1, DOI 10.5067/UNBV80EA7YBW. CSV with a header row, 12 comma-delimited columns:
  latitude_decimal_degree, longitude_decimal_degree, antenna_hae_m, decimal_hour, day_of_year, year,
  rcvr_clk_ns, NSV, GDOP, SDLAT_95, SDLON_95, SDHGT_95.
Longitude is already -180..180 (verified against a downloaded granule), unlike GLAS and ICESSN.
Row identity: (granule, along-track index).

Citation: Hawley, R. L., Pickell, D. J., McConnell, J. R., Neumann, T. A., Felikson, D., & Dorsi, S. W. (2026).
"""
from __future__ import annotations

import csv
import logging
import re
import time
from datetime import datetime, timedelta

import numpy as np

from . import auth, cache, coverage

log = logging.getLogger(__name__)

# Height precision gate. The product recommends no threshold, so this is ours: SDHGT_95 is the 95% standard
# deviation of the PPP height solution and runs ~0.07-0.12 m even in clean surveys, so this only removes
# genuinely bad epochs rather than trimming normal data.
MAX_SDHGT_M = 0.50

# ARP-to-sled-base (m). The guide quotes 1.797 m, but the metadata records per-survey values from 1.1 to 2.21.
# Those values are not scatter: they are two equipment eras with a clean changeover and no overlap --
#   1.785 m on 78 surveys, 2007-08-17 .. 2013-11-26
#   1.797 m on 150 surveys, 2014-01-12 .. 2025-04-02
# (the strays are the two "ARP to snow surface" rows in early 2007, a 1.795 rounding variant in 2020, and
# one-off sled setups in 2009 and 2017). 12 surveys record no height at all and 10 of those are 2006-2007, so
# a flat 1.797 default would put a 12 mm systematic error in exactly the early record -- enough to fake ~0.6
# mm/yr of trend across the 19-year series. The default is therefore era-aware.
ARP_ERA_CUTOFF = datetime(2014, 1, 1)
ARP_BEFORE_CUTOFF_M = 1.785
ARP_AFTER_CUTOFF_M = 1.797
DEFAULT_ARP_M = ARP_AFTER_CUTOFF_M
_ARP_NOTE_RE = re.compile(r"antenna height likely\s*([0-9.]+)", re.I)
# Two surveys note that the recorded value is ARP-to-snow-surface, i.e. sinkage is already accounted for.
_ARP_IS_SURFACE_RE = re.compile(r"actually ARP to snow surface", re.I)

# Sled sinkage (cm). Observed values span only 0.0-4.0 cm, so a missing measurement is bounded at a few cm.
# Unknown surveys are given the median of the known ones rather than zero: zero would introduce a systematic
# early-vs-late step (the early record is where measurements are missing) and bias any dh/dt fit. Rows filled
# this way are flagged via the `track_depth_known` array so a caller can exclude them.
FALLBACK_TRACK_DEPTH_CM = 2.0

_CSV_RE = re.compile(r"^IS2TGPSSS_(?P<stem>.+?)\.(?P<year>\d{4})_v01\.csv$")
_RANGE_RE = re.compile(r"^(?P<a>\S+?)(?P<n1>\d+)(?P<suf>\.\d{2}o)\s+through\s+\S*?(?P<n2>\d+)(?P=suf)$", re.I)


def _num(v) -> float | None:
    """Metadata cells are free text: 'Unknown', '', or a number."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def csv_to_rinex_key(name: str) -> str | None:
    """CSV granule name -> the key used by the metadata file.

    `IS2TGPSSS_ICE13260_2.2012_v01.csv` -> `ICE13260_2.12o`   (guide Tables 1-2: YYYY -> YY, drop the prefix)
    """
    m = _CSV_RE.match(name.rsplit("/", 1)[-1])
    if not m:
        return None
    return f"{m['stem']}.{m['year'][2:]}o"


def _load_traverse_metadata(path: str) -> dict[str, dict]:
    """Parse IS2TGPSSS_TraverseMetadata_v01.txt into {rinex_key: row}. Tab-delimited, with ~744 blank padding
    rows after the real ones. Three key forms appear and all three are expanded here:
      * a single file            `ICE13170.15o`
      * slash-joined files       `ICE11290_1.07o/ICE11290_2.07o`      (one survey split across files)
      * an inclusive range       `ICE12120_1.15o through ICE12120_12.15o`
    """
    out: dict[str, dict] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            raw = (row.get("Associated RINEX File") or "").strip()
            if not raw:
                continue                                     # trailing blank padding
            rng = _RANGE_RE.match(raw)
            if rng:
                stem, suf = rng["a"], rng["suf"]
                for i in range(int(rng["n1"]), int(rng["n2"]) + 1):
                    out[f"{stem}{i}{suf}"] = row
                continue
            for k in raw.split("/"):
                if k.strip():
                    out[k.strip()] = row
    log.info("traverse metadata: %d survey keys", len(out))
    return out


def _survey_date(row: dict | None) -> datetime | None:
    """The metadata `Date` cell, written as m/d/yy (occasionally m/d/YYYY)."""
    if not row:
        return None
    d = (row.get("Date") or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            pass
    return None


def default_arp_m(when: datetime | None) -> float:
    """Era-aware fallback antenna height -- see ARP_ERA_CUTOFF. Undated surveys get the modern value."""
    if when is None:
        return DEFAULT_ARP_M
    return ARP_BEFORE_CUTOFF_M if when < ARP_ERA_CUTOFF else ARP_AFTER_CUTOFF_M


def _sled_geometry(row: dict | None) -> tuple[float, float, bool]:
    """(arp_to_sled_base_m, track_depth_cm, track_depth_known) for one survey.

    Returns era-aware defaults and the fallback sinkage when the metadata is absent or unparseable, so a survey
    is never silently dropped for want of a few centimetres -- but `track_depth_known` records which it was.
    """
    if row is None:
        return DEFAULT_ARP_M, FALLBACK_TRACK_DEPTH_CM, False
    notes = row.get("Notes") or ""
    arp = _num(row.get("ARP to Sled Base (m)"))
    if arp is None:
        m = _ARP_NOTE_RE.search(notes)
        arp = float(m.group(1)) if m else default_arp_m(_survey_date(row))
    # "ARP to Sled Base is actually ARP to snow surface": the height already reaches the surface, so adding
    # sinkage on top would double-count it.
    if _ARP_IS_SURFACE_RE.search(notes):
        return arp, 0.0, True
    start, end = _num(row.get("Start Track Depth (cm)")), _num(row.get("End Track Depth (cm)"))
    depths = [d for d in (start, end) if d is not None]
    if not depths:
        return arp, FALLBACK_TRACK_DEPTH_CM, False
    return arp, float(np.mean(depths)), True


def _parse_file(path: str, bbox, meta_row: dict | None) -> dict[str, np.ndarray] | None:
    """One survey CSV -> arrays, with the antenna height reduced to a snow-surface elevation (guide Eq. 1)."""
    w, s, e, n = bbox
    # A handful of granules carry a truncated final-write row (e.g. "72.5916,-38."). invalid_raise=False skips
    # those lines instead of failing the whole survey.
    a = np.genfromtxt(path, delimiter=",", names=True, dtype="f8", invalid_raise=False)
    if a.size == 0 or "antenna_hae_m" not in (a.dtype.names or ()):
        return None
    a = np.atleast_1d(a)
    lat, lon = a["latitude_decimal_degree"], a["longitude_decimal_degree"]
    hae, sd = a["antenna_hae_m"], a["SDHGT_95"]

    arp_m, depth_cm, depth_known = _sled_geometry(meta_row)
    h = hae - arp_m + depth_cm / 100.0

    keep = np.isfinite(h) & np.isfinite(lat) & np.isfinite(lon) & (sd < MAX_SDHGT_M)
    keep &= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    if not keep.any():
        return None

    # year + day_of_year + decimal_hour (UTC) -> datetime64[ms]
    year = a["year"][keep].astype("i4")
    doy = a["day_of_year"][keep].astype("i8")          # i8: (doy-1)*86_400_000 overflows int32
    hour = a["decimal_hour"][keep]
    base = np.array([np.datetime64(f"{y:04d}-01-01", "ms") for y in year])
    ms = (doy - 1) * 86_400_000 + np.round(hour * 3_600_000).astype("i8")
    t = base + ms.astype("timedelta64[ms]")

    nn = int(keep.sum())
    return {"lon": lon[keep], "lat": lat[keep], "h": h[keep], "t": t,
            "sdhgt_m": sd[keep], "gdop": a["GDOP"][keep],
            "track_depth_cm": np.full(nn, depth_cm, "f4"),
            "track_depth_known": np.full(nn, depth_known, "?"),
            "arp_to_sled_m": np.full(nn, arp_m, "f4")}


def extract(bbox, window, max_granules: int = 400, polygon=None) -> tuple[dict[str, np.ndarray], dict]:
    """All IS2TGPSSS surveys over a bbox/window as one point cloud, plus provenance."""
    k = cache.key("gpstruth", coverage.GPSTRUTH_VERSION, bbox, window, max_granules, MAX_SDHGT_M,
                  FALLBACK_TRACK_DEPTH_CM, polygon)
    hit = cache.load(k)
    if hit:
        log.info("gpstruth cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    import earthaccess

    auth.login()
    granules = coverage.search(coverage.GPSTRUTH_SHORT_NAME, coverage.GPSTRUTH_VERSION, bbox, window)
    if not granules:
        raise RuntimeError(f"no IS2TGPSSS granules over {bbox} in {window}")

    # The collection ships RINEX (.o), the CSV positions, and one metadata .txt; we need the last two.
    # coverage.granule_name resolves the data-link basename only for .h5, so it would hand back this
    # collection's CMR native-id ("...csv_5QSQNx3x") instead of the filename. Resolve locally.
    def _name(g):
        try:
            links = g.data_links()
            if links:
                return links[0].rsplit("/", 1)[-1]
        except Exception:
            pass
        return re.sub(r"_[A-Za-z0-9]{8}$", "", str(g["meta"]["native-id"]))

    meta_g = [g for g in granules if "TraverseMetadata" in _name(g)]
    csv_g = [g for g in granules if _name(g).endswith(".csv")]
    if not meta_g:
        raise RuntimeError("IS2TGPSSS metadata file (track depth / antenna height) not found in the granule list")
    n_found = len(csv_g)
    csv_g = csv_g[:max_granules]

    raw_dir = cache.DATA_DIR / "raw" / "is2tgpsss"          # small CSVs; download beats remote text reads
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = earthaccess.download(meta_g + csv_g, local_path=str(raw_dir), threads=8, show_progress=False)
    paths = sorted(map(str, paths))
    meta_path = next(p for p in paths if "TraverseMetadata" in p)
    key2row = _load_traverse_metadata(meta_path)

    parts, prov, n_imputed = [], [], 0
    for path in [p for p in paths if p.endswith(".csv")]:
        name = path.rsplit("/", 1)[-1]
        rkey = csv_to_rinex_key(name)
        row = key2row.get(rkey)
        if row is None and rkey and "_" in rkey:
            row = key2row.get(re.sub(r"_\d+(?=\.\d{2}o$)", "", rkey))    # `ICE13170_1.17o` -> `ICE13170.17o`
        if row is None:
            log.warning("%s: no traverse-metadata row for key %r; using default sled geometry", name, rkey)
        t0 = time.time()
        try:
            d = _parse_file(path, bbox, row)
        except Exception as ex:
            log.warning("%s: IS2TGPSSS parse failed: %s", name, ex)
            continue
        if d is None:
            continue
        if not bool(d["track_depth_known"][0]):
            n_imputed += 1
        d["granule_idx"] = np.full(d["lon"].size, len(prov), dtype="i2")
        parts.append(d)
        prov.append({"granule": name, "rinex_key": rkey, "n": int(d["lon"].size),
                     "track_depth_cm": float(d["track_depth_cm"][0]),
                     "track_depth_known": bool(d["track_depth_known"][0]),
                     "arp_to_sled_m": float(d["arp_to_sled_m"][0]),
                     "seconds": round(time.time() - t0, 2)})
    if not parts:
        raise RuntimeError("IS2TGPSSS granules found but no usable GPS epochs in bbox")

    arrays = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arrays["lon"], arrays["lat"], polygon)
        arrays = {key: v[keep] for key, v in arrays.items()}
        if arrays["lon"].size == 0:
            raise RuntimeError("no IS2TGPSSS epochs inside the polygon")

    years = np.unique(arrays["t"].astype("datetime64[Y]")).astype(str).tolist()
    meta = {"mission": "GPSTRUTH", "product": f"IS2TGPSSS v{coverage.GPSTRUTH_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF (epoch of survey)", "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (GRS80 ellipsoid height; differs from WGS84 by << 1 mm)",
            "gia_correction": "none - ground-truth surface elevation, uncorrected",
            "surface_reduction": "antenna_hae_m - arp_to_sled_base + track_depth (user guide Eq. 1)",
            "quality_filter": f"SDHGT_95 < {MAX_SDHGT_M:.2f} m",
            "n_surveys_track_depth_imputed": n_imputed,
            "track_depth_fallback_cm": FALLBACK_TRACK_DEPTH_CM,
            "years": years, "n": int(arrays["lon"].size), "n_granules_found": n_found,
            "n_granules_read": len(prov), "granules": prov, "polygon": polygon,
            "citation": "Hawley et al. (2026), IS2TGPSSS v1, doi:10.5067/UNBV80EA7YBW"}
    meta["cache_key"] = k
    cache.save(k, arrays, meta)
    return arrays, meta
