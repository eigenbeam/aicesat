"""Offline parser tests for the two added missions: ICESat-2 ATL06 and IceBridge ATM ICESSN (ILATM2)."""
import h5py
import numpy as np

from aicesat import atl06, icessn


def test_atl06_strong_beam_mapping():
    assert atl06._strong_beams([0]) == ["gt1l", "gt2l", "gt3l"]   # backward
    assert atl06._strong_beams([1]) == ["gt1r", "gt2r", "gt3r"]   # forward
    assert atl06._strong_beams([2]) == []                          # yaw-flip transition -> skip


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
    with h5py.File(p, "r") as f:
        d = atl06._extract_granule(f, (-45, 69.5, -43, 70.5))
    assert d is not None and d["h"].size == 2                     # rows 0 and 3 survive
    assert np.allclose(sorted(d["h"]), [2500.0, 2520.0])
    assert str(d["t"][0]).startswith("2019")                      # ATLAS epoch 2018 + 4e7 s ~ 2019
    assert set(d["beam"].tolist()) == {0}


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
    assert str(d["t"][0]).startswith("2015-04-01")                # date from the filename


def test_icessn_asterisk_fill_becomes_nan(tmp_path):
    p = tmp_path / "ILATM2_20150401_130000_smooth_nadir3seg_50pt.csv"
    p.write_text(
        "# header\n"
        "43200.0, 70.00, 315.0, ****, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"       # elevation fill -> NaN -> dropped
        "43200.5, 70.05, 315.0, 2500.0, 0.01, 0.01, 4.5, 500, 0, 0, 0\n"
    )
    d = icessn._parse_file(str(p), (-46, 69.5, -44, 70.5))
    assert d is not None and d["h"].size == 1 and abs(d["h"][0] - 2500.0) < 1e-6
