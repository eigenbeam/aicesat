"""ICESSN carries each granule's ITRF realization, from its header through the index to the time series (#8 part 2).

ATM granules state their realization in the header ("International Terrestrial Reference Frame: ITRF05"), and it
changes across the record (ITRF05 in 2011, ITRF08 2012-2016, ITRF14 from 2017). The byte-range fetch never sees the
header, so the builder reads it and stores it on every index row; the fetch carries it to every point; and the time
series propagates each realization through its own frame step. The header parser and its first three tests are Ben
Smith's, from PR #18.
"""
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import access, auth, cache, coreg, icessn, index_icessn, lake, scene, timeseries

ROWS = ("43200.0, 70.00, 315.0, 2500.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"
        "43200.5, 70.01, 315.0, 2501.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n")


# ---- Ben's header tests (PR #18), pointed at the bytes the index builder holds ----------------------------------
def test_icessn_itrf_parsed_from_header(tmp_path):
    """The ITRF realization really does change mid-record (ITRF05 in 2011, ITRF08 2012-16, ITRF14 from 2017),
    and the 2019 granules write it lowercase — so parsing is per granule and case-insensitive."""
    for header, expect in (
        ("# International Terrestrial Reference Frame: ITRF05\n", 2005),
        ("# International Terrestrial Reference Frame: ITRF08\n", 2008),
        ("# International Terrestrial Reference Frame: itrf14\n", 2014),   # 2019 granules are lowercase
        ("# International Terrestrial Reference Frame: ITRF2008\n", 2008),  # four-digit form
        ("# International Terrestrial Reference Frame: ITRF97\n", 1997),    # two-digit 1900s
        ("# no frame line here\n", icessn.ITRF_UNKNOWN),
    ):
        p = tmp_path / f"ILATM2_20150401_1200{expect % 100:02d}_smooth_nadir3seg_50pt.csv"
        p.write_text(header + ROWS)
        assert icessn._itrf_year(str(p)) == expect, header
        assert icessn.itrf_year_from_bytes((header + ROWS).encode()) == expect, header


def test_icessn_itrf_only_read_from_the_header_block(tmp_path):
    """A frame string appearing after the header must not be picked up."""
    p = tmp_path / "ILATM2_20150401_121500_smooth_nadir3seg_50pt.csv"
    p.write_text("# Filename: x\n" + ROWS + "# International Terrestrial Reference Frame: ITRF08\n")
    assert icessn._itrf_year(str(p)) == icessn.ITRF_UNKNOWN


def test_icessn_frame_name():
    assert icessn._frame_name(2008) == "ITRF2008"
    assert "unknown" in icessn._frame_name(icessn.ITRF_UNKNOWN)


def test_one_realization_is_named_exactly_and_several_are_labelled_mixed():
    """A single realization must be a name the frame step accepts; several must NOT be, so nothing transforms a
    mixed extract with one realization's parameters."""
    assert icessn._native_frame([2008, 2008]) == ("ITRF2008", [2008])
    label, ys = icessn._native_frame([2005, 2008, 2008])
    assert ys == [2005, 2008] and label.startswith("ITRF (mixed:")
    with pytest.raises(ValueError):
        coreg._frame_pipeline(label)


# ---- through the index and the fetch -------------------------------------------------------------------------------
BLOBS: dict[str, bytes] = {}


class FakeReader:
    def __init__(self, *a, **k):
        self.stats = access.AccessStats()

    def presign_all(self, urls):
        return {u: u for u in urls}

    def read_all(self, url):
        return BLOBS[url]

    def fetch(self, url, ranges):
        blob = BLOBS[url]
        self.stats.requests += len(ranges)
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
    monkeypatch.setattr(index_icessn, "ICESSN_INDEX_DIR", tmp_path / "idx_icessn")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(access, "RangeReader", FakeReader)
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setenv("AICESAT_S3_DIRECT", "0")
    BLOBS.clear()
    yield
    lake.drain_writes()
    BLOBS.clear()


BBOX = (-45.2, 69.9, -44.8, 70.1)


def _granule(date, itrf_header, elev0):
    """An ILATM2 granule over BBOX: a header naming its realization, then nadir platelets along a short line."""
    name = f"ILATM2_{date}_120000_smooth_nadir3seg_50pt.csv"
    lines = [f"{43200.0 + i:.1f}, {69.95 + 0.004 * i:.5f}, {315.0 - 0.1 + 0.004 * i:.5f}, {elev0 + i * 0.1:.2f}, "
             f"0.01, 0.01, 4.5, 500, 0, 0, 0" for i in range(25)]
    url = f"https://x/{name}"
    BLOBS[url] = ("# Filename: " + name + "\n" + itrf_header + "\n".join(lines) + "\n").encode()
    return FakeGranule(url)


def test_index_rows_carry_the_granules_realization_and_the_new_version():
    tbl = index_icessn.build_icessn_index(_granule("20110501", "# International Terrestrial Reference Frame: ITRF05\n", 2500))
    assert set(tbl.column("itrf_year").to_pylist()) == {2005}
    meta = pq.read_schema(next(index_icessn._index_dir(5).glob("*.parquet"))).metadata
    assert meta[b"aicesat_icessn_index_version"] == index_icessn.ICESSN_INDEX_VERSION.encode() == b"3"


def test_an_index_file_from_before_the_column_reads_as_unknown_not_an_error():
    """Until the builder replaces them, v2 files (no itrf_year column) must still be readable."""
    index_icessn.build_icessn_index(_granule("20110501", "# International Terrestrial Reference Frame: ITRF05\n", 2500))
    p = next(index_icessn._index_dir(5).glob("*.parquet"))
    pq.write_table(pq.read_table(p).drop(["itrf_year"]), p)
    _cells, rows = index_icessn._index_rows(BBOX, None, 5)
    assert rows and {r["itrf_year"] for r in rows} == {icessn.ITRF_UNKNOWN}


def test_two_campaigns_in_two_realizations_are_each_propagated_through_their_own(monkeypatch):
    """End to end: build, extract, and hand the extract to the time series."""
    index_icessn.build_icessn_index(_granule("20110501", "# International Terrestrial Reference Frame: ITRF05\n", 2500))
    index_icessn.build_icessn_index(_granule("20140501", "# International Terrestrial Reference Frame: ITRF08\n", 2499))
    monkeypatch.setattr(icessn, "_index_covers", lambda bbox, polygon=None: True)
    arrays, meta = icessn.extract(BBOX, ("2009-01-01", "2019-12-31"))
    assert meta["native_frame_years"] == [2005, 2008] and meta["native_frame"].startswith("ITRF (mixed:")
    years = np.asarray(arrays["itrf_year"])
    assert sorted(set(years.tolist())) == [2005, 2008] and years.size == 50

    doc = {"frame": scene.local_frame(BBOX), "series": {"ICESSN": {"cache_key": meta["cache_key"]}}}
    rec = timeseries._load_all(doc, 2005.0)[0]
    assert rec["propagated"] is True, rec["frame_note"]
    moved = coreg.horizontal_displacement_m(arrays["lon"], arrays["lat"], rec["lon"], rec["lat"])
    assert (moved > 0.01).all()
