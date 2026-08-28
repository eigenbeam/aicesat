"""Offline parser tests for the two added missions: ICESat-2 ATL06 and IceBridge ATM ICESSN (ILATM2)."""
import h5py
import numpy as np

from aicesat import atl06, icessn


def test_atl06_strong_beam_mapping():
    assert atl06._strong_beams([0]) == ["gt1l", "gt2l", "gt3l"]   # backward
    assert atl06._strong_beams([1]) == ["gt1r", "gt2r", "gt3r"]   # forward
    assert atl06._strong_beams([2]) == []                          # yaw-flip transition: side ambiguous
    assert atl06._strong_side([0]) == "l" and atl06._strong_side([1]) == "r"
    assert atl06._strong_side([2]) is None


def _atl06_file(tmp_path, name, sc_orient, beams, n=2):
    """Minimal ATL06 granule with `beams` populated, all rows good and inside the test bbox."""
    p = tmp_path / name
    with h5py.File(p, "w") as f:
        f.create_dataset("orbit_info/sc_orient", data=[sc_orient])
        for b in beams:
            g = f.create_group(f"{b}/land_ice_segments")
            g.create_dataset("latitude", data=np.full(n, 70.0))
            g.create_dataset("longitude", data=np.full(n, -44.0))
            g.create_dataset("h_li", data=np.full(n, 2500.0), dtype="f4")
            g.create_dataset("delta_time", data=np.full(n, 4.0e7))
            g.create_dataset("atl06_quality_summary", data=np.zeros(n), dtype="i1")
            g.create_dataset("segment_id", data=np.arange(n), dtype="i4")
            g.create_dataset("fit_statistics/dh_fit_dx", data=np.full(n, 0.002))
            g.create_dataset("fit_statistics/dh_fit_dy", data=np.full(n, -0.003))
            g.create_dataset("ground_track/seg_azimuth", data=np.full(n, -169.0))
    return p


def test_atl06_reads_all_six_beams_by_default(tmp_path):
    p = _atl06_file(tmp_path, "ATL06_all.h5", 0, atl06.GT_BEAMS)
    with h5py.File(p, "r") as f:
        d = atl06._extract_granule(f, (-45, 69.5, -43, 70.5))
    assert sorted(set(d["beam"].tolist())) == [0, 1, 2, 3, 4, 5]     # every beam, canonical GT_BEAMS order
    # sc_orient == 0 -> the 'l' beams are strong; GT_BEAMS is [1l,1r,2l,2r,3l,3r] so even indices are strong
    strong = {int(b) for b, s in zip(d["beam"], d["beam_strong"]) if s == 1}
    assert strong == {0, 2, 4}
    assert {int(b) for b, s in zip(d["beam"], d["beam_strong"]) if s == 0} == {1, 3, 5}


def test_atl06_strong_only_halves_the_read(tmp_path):
    p = _atl06_file(tmp_path, "ATL06_strong.h5", 1, atl06.GT_BEAMS)     # sc_orient 1 -> 'r' beams strong
    with h5py.File(p, "r") as f:
        d = atl06._extract_granule(f, (-45, 69.5, -43, 70.5), strong_only=True)
    assert sorted(set(d["beam"].tolist())) == [1, 3, 5]                 # the 'r' beams
    assert set(d["beam_strong"].tolist()) == {1}


def test_atl06_yaw_flip_is_read_with_unknown_strength(tmp_path):
    """sc_orient == 2 leaves the strong/weak label ambiguous, but the heights are still valid, so the granule is
    read rather than skipped — the label is marked unknown instead of guessed."""
    p = _atl06_file(tmp_path, "ATL06_yaw.h5", 2, atl06.GT_BEAMS)
    with h5py.File(p, "r") as f:
        d = atl06._extract_granule(f, (-45, 69.5, -43, 70.5))
    assert d is not None and sorted(set(d["beam"].tolist())) == [0, 1, 2, 3, 4, 5]
    assert set(d["beam_strong"].tolist()) == {atl06.STRONG_UNKNOWN}
    with h5py.File(p, "r") as f:                                        # strong_only can't resolve it -> skip
        assert atl06._extract_granule(f, (-45, 69.5, -43, 70.5), strong_only=True) is None


def test_atl06_extract_granule_quality_and_fill(tmp_path):
    p = tmp_path / "ATL06_x.h5"
    with h5py.File(p, "w") as f:
        f.create_dataset("orbit_info/sc_orient", data=[0])       # strong = l beams
        g = f.create_group("gt1l/land_ice_segments")
        g.create_dataset("latitude", data=[70.0, 70.1, 70.2, 70.3])
        g.create_dataset("longitude", data=[-44.0, -44.0, -44.0, -44.0])
        g.create_dataset("h_li", data=[2500.0, 3.5e38, 2510.0, 2520.0], dtype="f4")   # row1 = fill
        g.create_dataset("delta_time", data=[4.0e7, 4.0e7, 4.0e7, 4.0e7])
        g.create_dataset("atl06_quality_summary", data=[0, 0, 1, 0], dtype="i1")       # row2 = bad quality
        g.create_dataset("segment_id", data=[1, 2, 3, 4], dtype="i4")
        g.create_dataset("fit_statistics/dh_fit_dx", data=[0.002, 0.0, 0.0, 3.5e38])   # row3 = fill
        g.create_dataset("fit_statistics/dh_fit_dy", data=[-0.003, 0.0, 0.0, 0.004])
        g.create_dataset("ground_track/seg_azimuth", data=[-169.0, 0.0, 0.0, -169.2])
    with h5py.File(p, "r") as f:
        d = atl06._extract_granule(f, (-45, 69.5, -43, 70.5))
    assert d is not None and d["h"].size == 2                     # rows 0 and 3 survive
    assert np.allclose(sorted(d["h"]), [2500.0, 2520.0])
    assert str(d["t"][0]).startswith("2019")                      # ATLAS epoch 2018 + 4e7 s ~ 2019
    assert set(d["beam"].tolist()) == {0}                         # gt1l is GT_BEAMS[0]
    assert set(d["beam_strong"].tolist()) == {1}                  # sc_orient 0 -> 'l' is strong
    # the natively measured slope rides along, aligned with the surviving rows
    assert d["dh_fit_dx"].size == d["dh_fit_dy"].size == d["seg_azimuth"].size == 2
    assert abs(d["dh_fit_dx"][0] - 0.002) < 1e-12
    assert abs(d["dh_fit_dy"][0] - (-0.003)) < 1e-12
    assert np.isnan(d["dh_fit_dx"][1])                            # FILL -> NaN, row still kept
    assert abs(d["seg_azimuth"][0] - (-169.0)) < 1e-9


def test_atl06_slope_absent_degrades_to_nan(tmp_path):
    """A granule without the fit_statistics group must still read: no slope, not a failed extraction."""
    p = tmp_path / "ATL06_noslope.h5"
    with h5py.File(p, "w") as f:
        f.create_dataset("orbit_info/sc_orient", data=[0])
        g = f.create_group("gt1l/land_ice_segments")
        g.create_dataset("latitude", data=[70.0, 70.1])
        g.create_dataset("longitude", data=[-44.0, -44.0])
        g.create_dataset("h_li", data=[2500.0, 2510.0], dtype="f4")
        g.create_dataset("delta_time", data=[4.0e7, 4.0e7])
        g.create_dataset("atl06_quality_summary", data=[0, 0], dtype="i1")
        g.create_dataset("segment_id", data=[1, 2], dtype="i4")
    with h5py.File(p, "r") as f:
        d = atl06._extract_granule(f, (-45, 69.5, -43, 70.5))
    assert d is not None and d["h"].size == 2
    assert np.isnan(d["dh_fit_dx"]).all() and np.isnan(d["dh_fit_dy"]).all()


def test_slope_deg_median_from_gradient():
    """Both products deliver slope as a rise/run gradient, so the same reduction serves both."""
    from aicesat import geom
    assert geom.slope_deg_median([0.0], [0.0]) == 0.0
    assert abs(geom.slope_deg_median([1.0], [0.0]) - 45.0) < 1e-9      # 100% grade = 45 deg
    assert abs(geom.slope_deg_median([0.003, 0.005], [0.004, 0.0]) - np.degrees(np.arctan(0.005))) < 1e-9
    assert geom.slope_deg_median([np.nan], [np.nan]) is None           # all fill -> no answer, not 0.0


def test_icessn_parse_nadir_lon_and_rms(tmp_path):
    p = tmp_path / "ILATM2_20150401_120000_smooth_nadir3seg_50pt.csv"
    p.write_text(
        "# Filename: ILATM2_20150401_120000_smooth_nadir3seg_50pt.csv\n"
        "# Number of segments: 3\n"
        "43200.0, 70.00, 315.0, 2500.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"     # nadir, lon 315->-45, rms 4.5cm -> keep
        "43200.0, 70.10, 315.0, 2510.0, 0.01, 0.01, 4.5, 500, 0, 30, 1\n"    # track 1 (off-nadir) -> drop
        "43200.5, 70.20, 315.0, 2520.0, 0.01, 0.01, 80.0, 500, 0, 0, 0\n"    # nadir but rms 80cm > 50 -> drop
    )
    d = icessn._parse_file(str(p), (-46, 69.5, -44, 70.5))
    assert d is not None and d["h"].size == 1
    assert abs(d["lon"][0] - (-45.0)) < 1e-6                       # 0..360 E normalized
    assert abs(d["h"][0] - 2500.0) < 1e-6
    # cols 4/5 are ATM's measured SN/WE slope components, kept rather than parsed past
    assert abs(d["sn_slope"][0] - 0.01) < 1e-12
    assert abs(d["we_slope"][0] - 0.01) < 1e-12


def test_icessn_slope_columns_are_not_confused_with_rms(tmp_path):
    """Guard the column indices: SN=4, WE=5, RMS=6. Distinct values so a shifted index fails loudly."""
    p = tmp_path / "ILATM2_20150401_140000_smooth_nadir3seg_50pt.csv"
    p.write_text(
        "# header\n"
        "43200.0, 70.00, 315.0, 2500.0, -0.0026165, 0.0066448, 3.85, 558, 5, 0, 0\n"
        "43200.5, 70.01, 315.0, 2501.0, -0.0030000, 0.0070000, 4.10, 560, 4, 0, 0\n"
    )
    d = icessn._parse_file(str(p), (-46, 69.5, -44, 70.5))
    assert d is not None and d["h"].size == 2
    assert abs(d["sn_slope"][0] - (-0.0026165)) < 1e-12
    assert abs(d["we_slope"][0] - 0.0066448) < 1e-12
    assert abs(d["rms_cm"][0] - 3.85) < 1e-12
    assert str(d["t"][0]).startswith("2015-04-01")                # date from the filename


def test_icessn_itrf_parsed_from_header(tmp_path):
    """The ITRF realization really does change mid-record (ITRF05 in 2011, ITRF08 2012-16, ITRF14 from 2017),
    and the 2019 granules write it lowercase — so parsing is per granule and case-insensitive."""
    rows = ("43200.0, 70.00, 315.0, 2500.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"
            "43200.5, 70.01, 315.0, 2501.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n")
    for header, expect in (
        ("# International Terrestrial Reference Frame: ITRF05\n", 2005),
        ("# International Terrestrial Reference Frame: ITRF08\n", 2008),
        ("# International Terrestrial Reference Frame: itrf14\n", 2014),   # 2019 granules are lowercase
        ("# International Terrestrial Reference Frame: ITRF2008\n", 2008),  # four-digit form
        ("# International Terrestrial Reference Frame: ITRF97\n", 1997),    # two-digit 1900s
        ("# no frame line here\n", icessn.ITRF_UNKNOWN),
    ):
        p = tmp_path / f"ILATM2_20150401_1200{expect % 100:02d}_smooth_nadir3seg_50pt.csv"
        p.write_text(header + rows)
        assert icessn._itrf_year(str(p)) == expect, header
        d = icessn._parse_file(str(p), (-46, 69.5, -44, 70.5))
        assert set(d["itrf_year"].tolist()) == {expect}      # carried per row, not just in meta


def test_icessn_itrf_only_read_from_the_header_block(tmp_path):
    """A frame string appearing after the header must not be picked up."""
    p = tmp_path / "ILATM2_20150401_121500_smooth_nadir3seg_50pt.csv"
    p.write_text("# Filename: x\n"
                 "43200.0, 70.00, 315.0, 2500.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"
                 "43200.5, 70.01, 315.0, 2501.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"
                 "# International Terrestrial Reference Frame: ITRF08\n")
    assert icessn._itrf_year(str(p)) == icessn.ITRF_UNKNOWN


def test_icessn_frame_name():
    assert icessn._frame_name(2008) == "ITRF2008"
    assert "unknown" in icessn._frame_name(icessn.ITRF_UNKNOWN)


def test_icessn_asterisk_fill_becomes_nan(tmp_path):
    p = tmp_path / "ILATM2_20150401_130000_smooth_nadir3seg_50pt.csv"
    p.write_text(
        "# header\n"
        "43200.0, 70.00, 315.0, ****, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"       # elevation fill -> NaN -> dropped
        "43200.5, 70.05, 315.0, 2500.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"
    )
    d = icessn._parse_file(str(p), (-46, 69.5, -44, 70.5))
    assert d is not None and d["h"].size == 1 and abs(d["h"][0] - 2500.0) < 1e-6


def test_lake_write_points_materializes(tmp_path, monkeypatch):
    """A point collection (GLAS/ATL06/ICESSN) written to the lake shows up in cell_stats and missions()."""
    import numpy as np
    from aicesat import lake
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    n = 300
    arrays = {"lon": np.linspace(-45, -43, n), "lat": np.linspace(69.8, 70.2, n), "h": np.full(n, 2500.0),
              "t": np.array(["2005-05-01"] * n, dtype="datetime64[ms]"), "granule_idx": np.zeros(n, "i2")}
    meta = {"granules": [{"granule": "GLAH06_x.h5"}], "height_ref": "WGS84 ellipsoid"}
    cells = lake.write_points("GLAS", arrays, meta)
    assert len(cells) > 0
    st = lake.cell_stats("GLAS")
    assert len(st) == len(cells)
    assert sum(c["rows"] for c in st.values()) == n
    assert any(c["granules"] == ["GLAH06_x"] for c in st.values())          # .h5 stripped, filename-safe
    assert "GLAS" in [m["mission"] for m in lake.missions()]
