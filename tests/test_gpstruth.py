"""Offline parser tests for IS2TGPSSS (Summit GPS traverse ground truth).

Fixtures reproduce the real metadata quirks found in IS2TGPSSS_TraverseMetadata_v01.txt: tab delimiters,
'Unknown' cells, slash-joined and 'through'-range RINEX keys, the antenna height recorded only in Notes, and
the two surveys whose recorded height is to the snow surface rather than the sled base.
"""
import numpy as np

from aicesat import gpstruth

HDR = ("Associated RINEX File\tDate\tSurvey Start Time (UTC)\tSurvey End Time (UTC)\t"
       "Start Track Depth (cm)\tEnd Track Depth (cm)\tARP to Sled Base (m)\tNotes\n")

CSV_HDR = ("latitude_decimal_degree,longitude_decimal_degree,antenna_hae_m,decimal_hour,day_of_year,year,"
           "rcvr_clk_ns,NSV,GDOP,SDLAT_95,SDLON_95,SDHGT_95\n")


def _csv(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(CSV_HDR + "".join(rows))
    return str(p)


def _row(lat=72.60, lon=-38.55, hae=3250.0, hour=15.5, doy=236, year=2020, sd=0.10):
    return f"{lat},{lon},{hae},{hour},{doy},{year},29637.5,8,3.1,0.037,0.028,{sd}\n"


def test_csv_to_rinex_key():
    assert gpstruth.csv_to_rinex_key("IS2TGPSSS_ICE13260_2.2012_v01.csv") == "ICE13260_2.12o"
    assert gpstruth.csv_to_rinex_key("IS2TGPSSS_ICE13270.2007_v01.csv") == "ICE13270.07o"
    assert gpstruth.csv_to_rinex_key("not-a-granule.txt") is None


def test_metadata_expands_slash_and_through_keys(tmp_path):
    p = tmp_path / "meta.txt"
    p.write_text(HDR
                 + "ICE13170.15o\t11/13/15\t13:05\t15:25\t1.0\t1.0\t1.797\t\n"
                 + "ICE11290_1.07o/ICE11290_2.07o\t5/9/07\t11:00\t12:00\tUnknown\tUnknown\tUnknown\t\n"
                 + "ICE12120_1.15o through ICE12120_12.15o\t7/31/15\t12:10\t13:27\t3.0\t4.0\t1.797\tmany files\n"
                 + "\t\t\t\t\t\t\t\n")                      # trailing blank padding, as in the real file
    m = gpstruth._load_traverse_metadata(str(p))
    assert "ICE13170.15o" in m
    assert "ICE11290_1.07o" in m and "ICE11290_2.07o" in m       # slash-joined -> both keys
    assert "ICE12120_1.15o" in m and "ICE12120_12.15o" in m      # inclusive range -> all 12
    assert sum(k.startswith("ICE12120_") for k in m) == 12
    assert "" not in m                                          # padding rows are not keys


def test_sled_geometry_resolution_order():
    # explicit column wins
    arp, depth, known = gpstruth._sled_geometry(
        {"ARP to Sled Base (m)": "1.795", "Start Track Depth (cm)": "2.0", "End Track Depth (cm)": "4.0",
         "Notes": "", "Date": "2/23/20"})
    assert arp == 1.795 and depth == 3.0 and known is True      # depth = mean(start, end)

    # column Unknown -> value recovered from the Notes free text
    arp, depth, known = gpstruth._sled_geometry(
        {"ARP to Sled Base (m)": "Unknown", "Start Track Depth (cm)": "2.0", "End Track Depth (cm)": "Unknown",
         "Notes": "Antenna height likely 1.785 (Derek Pickell)", "Date": "11/12/20"})
    assert arp == 1.785 and depth == 2.0 and known is True      # one depth present is enough

    # nothing anywhere -> era default + imputed sinkage, flagged not-known
    arp, depth, known = gpstruth._sled_geometry(
        {"ARP to Sled Base (m)": "Unknown", "Start Track Depth (cm)": "Unknown",
         "End Track Depth (cm)": "Unknown", "Notes": "", "Date": "8/24/06"})
    assert arp == gpstruth.ARP_BEFORE_CUTOFF_M
    assert depth == gpstruth.FALLBACK_TRACK_DEPTH_CM and known is False


def test_arp_default_is_era_aware():
    """The real record is two equipment eras: 1.785 m through 2013-11-26, 1.797 m from 2014-01-12. A flat
    default would put a 12 mm systematic error in the early record, where the metadata gaps are."""
    from datetime import datetime
    assert gpstruth.default_arp_m(datetime(2007, 8, 17)) == 1.785
    assert gpstruth.default_arp_m(datetime(2013, 11, 26)) == 1.785
    assert gpstruth.default_arp_m(datetime(2014, 1, 12)) == 1.797
    assert gpstruth.default_arp_m(None) == gpstruth.DEFAULT_ARP_M


def test_arp_to_snow_surface_does_not_add_sinkage():
    """Two 2007 surveys note the recorded height already reaches the snow surface; adding Ztrack would
    double-count the sinkage."""
    arp, depth, known = gpstruth._sled_geometry(
        {"ARP to Sled Base (m)": "1.55", "Start Track Depth (cm)": "0.0", "End Track Depth (cm)": "0.0",
         "Notes": "ARP to Sled Base is actually ARP to snow surface", "Date": "1/3/07"})
    assert arp == 1.55 and depth == 0.0 and known is True


def test_parse_file_applies_surface_reduction_and_time(tmp_path):
    path = _csv(tmp_path, "IS2TGPSSS_ICE12360.2020_v01.csv", [_row(hae=3250.0, doy=236, year=2020, hour=15.5)])
    row = {"ARP to Sled Base (m)": "1.797", "Start Track Depth (cm)": "3.0", "End Track Depth (cm)": "5.0",
           "Notes": "", "Date": "8/23/20"}
    d = gpstruth._parse_file(path, (-39.0, 72.4, -38.0, 72.8), row)
    assert d is not None and d["h"].size == 1
    # 3250.0 - 1.797 + 0.04 m  (mean sinkage 4 cm)
    assert abs(d["h"][0] - (3250.0 - 1.797 + 0.04)) < 1e-9
    assert str(d["t"][0]).startswith("2020-08-23")            # 2020 doy 236 = 23 Aug
    assert "15:30" in str(d["t"][0])                          # decimal_hour 15.5
    assert bool(d["track_depth_known"][0]) is True


def test_parse_file_quality_and_bbox_filters(tmp_path):
    path = _csv(tmp_path, "IS2TGPSSS_ICE12360.2020_v01.csv", [
        _row(lat=72.60, sd=0.10),                              # keep
        _row(lat=72.60, sd=9.99),                              # SDHGT_95 above the gate -> drop
        _row(lat=71.00, sd=0.10),                              # outside bbox -> drop
    ])
    d = gpstruth._parse_file(path, (-39.0, 72.4, -38.0, 72.8), None)
    assert d is not None and d["h"].size == 1


def test_parse_file_flags_imputed_sinkage(tmp_path):
    """A survey with no metadata row is still ingested (the record would otherwise lose most of 2006-2012),
    but every row carries track_depth_known=False so a caller can exclude it."""
    path = _csv(tmp_path, "IS2TGPSSS_ICE12360.2007_v01.csv", [_row(year=2007, doy=100)])
    d = gpstruth._parse_file(path, (-39.0, 72.4, -38.0, 72.8), None)
    assert d is not None
    assert bool(d["track_depth_known"][0]) is False
    assert d["track_depth_cm"][0] == gpstruth.FALLBACK_TRACK_DEPTH_CM


def test_longitude_is_not_renormalized(tmp_path):
    """IS2TGPSSS delivers -180..180 already, unlike GLAS and ICESSN; a 0..360 fixup would push Summit out."""
    path = _csv(tmp_path, "IS2TGPSSS_ICE12360.2020_v01.csv", [_row(lon=-38.55)])
    d = gpstruth._parse_file(path, (-39.0, 72.4, -38.0, 72.8), None)
    assert d is not None and abs(d["lon"][0] - (-38.55)) < 1e-9


def test_lake_write_points_materializes_gpstruth(tmp_path, monkeypatch):
    from aicesat import lake
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    n = 200
    arrays = {"lon": np.linspace(-38.58, -38.48, n), "lat": np.linspace(72.58, 72.64, n),
              "h": np.full(n, 3250.0), "t": np.array(["2020-08-23"] * n, dtype="datetime64[ms]"),
              "granule_idx": np.zeros(n, "i2")}
    meta = {"granules": [{"granule": "IS2TGPSSS_ICE12360.2020_v01.csv"}], "height_ref": "WGS84 ellipsoid"}
    cells = lake.write_points("GPSTRUTH", arrays, meta)
    assert len(cells) > 0
    assert sum(c["rows"] for c in lake.cell_stats("GPSTRUTH").values()) == n
    assert "GPSTRUTH" in [m["mission"] for m in lake.missions()]
