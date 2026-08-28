"""Offline parser tests for the two added missions: ICESat-2 ATL06 and IceBridge ATM ICESSN (ILATM2)."""
import h5py
import numpy as np

from aicesat import atl06, icessn


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
