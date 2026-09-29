"""Offline parser tests for IS2TGPSSS (Summit GPS traverse ground truth).

Fixtures reproduce the real metadata quirks found in IS2TGPSSS_TraverseMetadata_v01.txt: tab delimiters,
'Unknown' cells, slash-joined and 'through'-range RINEX keys, the antenna height recorded only in Notes, and
the two surveys whose recorded height is to the snow surface rather than the sled base.
"""
import hashlib

import numpy as np
import pyarrow.parquet as pq
import pytest

from aicesat import access, auth, coverage, gpstruth, index_gpstruth, lake

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


# =================================================================================================================
# The index path (the port onto main's index-only contract). Ben's tests above cover the parser and the sled
# geometry; these cover the build, the fetch and the wiring that replaced the whole-file download.
# =================================================================================================================
BLOBS: dict[str, bytes] = {}


def _two_cell_line(n=40):
    """lat/lon along a straight line from one res-5 cell's centre (at Summit) to a neighbour's, so the survey
    genuinely straddles two cells."""
    import h3
    a = h3.latlng_to_cell(72.61, -38.53, 5)
    b = sorted(set(h3.grid_disk(a, 1)) - {a})[0]
    (la0, lo0), (la1, lo1) = h3.cell_to_latlng(a), h3.cell_to_latlng(b)
    return np.linspace(la0, la1, n), np.linspace(lo0, lo1, n)


_LAT, _LON = _two_cell_line()
BBOX = (float(_LON.min()) - 0.01, float(_LAT.min()) - 0.01, float(_LON.max()) + 0.01, float(_LAT.max()) + 0.01)


class FakeReader:
    """Byte ranges and whole files out of an in-memory blob; counts GETs. No auth, no sockets."""

    def __init__(self, *a, **k):
        self.stats = access.AccessStats()

    def presign_all(self, urls):
        return {u: u for u in urls}

    def read_all(self, url):
        self.stats.requests += 1
        return BLOBS[url]

    def fetch(self, url, ranges):
        blob = BLOBS[url]
        self.stats.requests += len(ranges)
        self.stats.bytes += sum(s for _, s in ranges)
        return [blob[o:o + s] for o, s in ranges]


class FakeGranule:
    def __init__(self, url):
        self.url = url

    def data_links(self, access=None):
        return [""] if access == "direct" else [self.url]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "index" / "meta.duckdb")
    monkeypatch.setattr(lake, "SETTINGS_PATH", tmp_path / "index" / "settings.json")
    monkeypatch.setattr(lake, "EVICTION_LOG", tmp_path / "index" / "evictions.jsonl")
    monkeypatch.setattr(index_gpstruth, "GPSTRUTH_INDEX_DIR", tmp_path / "idx_gpstruth")
    monkeypatch.setattr(access, "RangeReader", FakeReader)
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setenv("AICESAT_S3_DIRECT", "0")
    BLOBS.clear()
    yield
    lake.drain_writes()          # settle the writer before monkeypatch restores LAKE_DIR
    BLOBS.clear()


def _survey(name, rows, url=None):
    """A survey CSV as the archive serves it (CRLF, header row), registered with the fake reader."""
    url = url or f"https://x/{name}"
    BLOBS[url] = (CSV_HDR.replace("\n", "\r\n") + "".join(r.replace("\n", "\r\n") for r in rows)).encode()
    return FakeGranule(url)


def _two_cell_survey(name="IS2TGPSSS_ICE12360.2020_v01.csv", hae=3250.0):
    return _survey(name, [_row(lat=la, lon=lo, hae=hae + 0.01 * i, hour=15.0 + i / 3600)
                          for i, (la, lo) in enumerate(zip(_LAT, _LON))])


GEOM = (1.797, 3.0, True, "ICE12360.20o")


def _build(g, geometry=GEOM, sha="meta-a"):
    return index_gpstruth.build_gpstruth_index(g, geometry, sha)


def _same(a, b):
    oa, ob = np.lexsort((a["lon"], a["t"])), np.lexsort((b["lon"], b["t"]))
    assert set(a) >= set(index_gpstruth._DIRECT) and set(b) >= set(index_gpstruth._DIRECT)
    for k in index_gpstruth._DIRECT:
        assert np.array_equal(np.asarray(a[k])[oa], np.asarray(b[k])[ob]), k


def test_metadata_from_bytes_matches_the_file_parser(tmp_path):
    text = (HDR + "ICE13170.15o\t11/13/15\t13:05\t15:25\t1.0\t1.0\t1.797\t\n"
            + "ICE12120_1.15o through ICE12120_3.15o\t7/31/15\t12:10\t13:27\t3.0\t4.0\t1.797\tx\n")
    p = tmp_path / "meta.txt"; p.write_text(text)
    from_file = gpstruth._load_traverse_metadata(str(p))
    from_bytes = gpstruth.traverse_metadata_from_bytes(text.replace("\n", "\r\n").encode())   # archive is CRLF
    assert sorted(from_file) == sorted(from_bytes) and len(from_bytes) == 4
    assert from_bytes["ICE12120_2.15o"]["End Track Depth (cm)"] == "4.0"


def test_split_granules_skips_rinex_and_finds_the_metadata():
    gs = [FakeGranule("https://x/IS2TGPSSS_ICE12360.2006_v01.csv"),
          FakeGranule("https://x/IS2TGPSSS_ICE12360.06o_v01"),            # RINEX observation file
          FakeGranule("https://x/IS2TGPSSS_TraverseMetadata_v01.txt")]
    meta, csvs = gpstruth.split_granules(gs)
    assert meta is gs[2] and csvs == [gs[0]]


def test_survey_row_falls_back_from_a_split_rinex_key():
    rows = {"ICE13170.17o": {"Date": "6/19/17"}}
    row, rkey = gpstruth.survey_row(rows, "IS2TGPSSS_ICE13170_1.2017_v01.csv")
    assert rkey == "ICE13170_1.17o" and row is rows["ICE13170.17o"]


def test_metadata_granule_name_resolves_to_its_file():
    """coverage.granule_name used to resolve only .h5/.csv, handing back the CMR native-id for the .txt."""
    assert coverage.granule_name(FakeGranule("https://x/IS2TGPSSS_TraverseMetadata_v01.txt")) == \
        "IS2TGPSSS_TraverseMetadata_v01.txt"


def test_index_rows_carry_spans_header_date_and_sled_geometry():
    tbl = _build(_two_cell_survey())
    rows = tbl.to_pylist()
    assert len(rows) == 2, "the survey should straddle two res-5 cells"
    assert {r["gdate"] for r in rows} == {"20200823"}                    # 2020 day 236
    assert all(r["header"].startswith("latitude_decimal_degree") and not r["header"].endswith("\r") for r in rows)
    assert all((r["arp_to_sled_m"], r["track_depth_cm"], r["track_depth_known"], r["rinex_key"]) == GEOM for r in rows)
    assert sum(r["n_lines"] for r in rows) == 40
    meta = pq.read_schema(index_gpstruth._index_dir(5) / "IS2TGPSSS_ICE12360.2020_v01.csv.parquet").metadata
    assert meta[index_gpstruth.METADATA_KEY.encode()] == b"meta-a"


def test_an_empty_survey_writes_a_schema_identical_index_file():
    full = _build(_two_cell_survey())
    empty = _build(_survey("IS2TGPSSS_ICE19990.2020_v01.csv", []))
    assert empty.num_rows == 0 and empty.schema.remove_metadata() == full.schema.remove_metadata()


def test_lake_first_fetch_matches_the_direct_path_and_then_costs_nothing():
    _build(_two_cell_survey())
    golden, _ = index_gpstruth._fetch_direct(BBOX)
    got, st1 = index_gpstruth.fetch_bbox(BBOX)
    assert golden["lon"].size == 40 and st1["chunks_from_nasa"] == 2
    _same(golden, got)
    again, st2 = index_gpstruth.fetch_bbox(BBOX)
    assert st2["chunks_from_nasa"] == 0 and st2.get("requests", 0) == 0
    _same(got, again)


def test_fetch_reduces_with_the_geometry_on_the_index_rows():
    """h = antenna_hae - arp_to_sled + track_depth/100, with the survey's own geometry (guide Eq. 1)."""
    _build(_two_cell_survey(hae=3250.0), geometry=(1.785, 2.0, False, "ICE12360.20o"))
    got, _ = index_gpstruth._fetch_direct(BBOX)
    o = np.argsort(got["t"])
    want = 3250.0 + 0.01 * np.arange(40) - 1.785 + 0.02
    np.testing.assert_allclose(got["h"][o], want, rtol=0, atol=1e-9)   # rtol=0: the default allows ~3 cm at 3250 m
    assert (~got["track_depth_known"].astype(bool)).all() and np.allclose(got["arp_to_sled_m"], 1.785)


def test_a_revised_metadata_file_makes_the_index_stale():
    _build(_two_cell_survey(), sha="meta-a")
    assert index_gpstruth.indexed_gpstruth_granules(5, metadata_sha1="meta-a") == {"IS2TGPSSS_ICE12360.2020_v01.csv"}
    assert index_gpstruth.indexed_gpstruth_granules(5, metadata_sha1="meta-b") == set()
    assert not list(index_gpstruth._index_dir(5).glob("*.parquet")), "the stale file must be deleted, not kept"


def test_extract_records_the_frame_assumption_and_imputed_epochs(monkeypatch, tmp_path):
    from aicesat import cache
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(gpstruth, "_index_covers", lambda bbox, polygon=None: True)
    _build(_two_cell_survey(), geometry=(1.797, 2.0, False, ""))
    arrays, meta = gpstruth.extract(BBOX, ("2006-08-01", "2025-12-31"))
    assert meta["native_frame"] == "ITRF2020" and "ITRF2020" in meta["frame_assumption"]
    assert meta["n"] == 40 and meta["n_epochs_track_depth_imputed"] == 40
    assert set(index_gpstruth._DIRECT) <= set(arrays)


def test_gps_heights_are_plate_motion_propagated_not_left_raw(monkeypatch):
    """The frame label must be one the frame step accepts, or the time series reports GPSTRUTH as unpropagated."""
    from aicesat import coreg, scene, timeseries
    n = 20
    arrays = {"lon": np.full(n, -38.53), "lat": np.full(n, 72.61), "h": np.full(n, 3200.0),
              "t": np.full(n, np.datetime64("2015-06-01"), "datetime64[ms]")}
    monkeypatch.setattr(coreg, "_reload_arrays", lambda s: (arrays, {"native_frame": gpstruth.NATIVE_FRAME}))
    doc = {"frame": scene.local_frame(BBOX), "series": {"GPSTRUTH": {"cache_key": "k"}}}
    rec = timeseries._load_all(doc, 2005.0)[0]
    assert rec["propagated"] is True
    assert coreg.horizontal_displacement_m(arrays["lon"], arrays["lat"], rec["lon"], rec["lat"]).min() > 0.05
