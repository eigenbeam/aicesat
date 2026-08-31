"""Offline test for the lake write path.

The whole-granule parser tests that used to live here went with the readers they covered (atl06._extract_granule,
icessn._parse_file, atl06._strong_beams) when the CMR/download fallback was deleted — issue #24 tier 2. Their
semantics are pinned on the LIVE index path instead: ATL06 fill + quality drop by
test_lake_cache.test_atl06_lake_first_matches_direct_golden, the ICESSN RMS cut and off-nadir reject by
test_icessn_lake_first_matches_direct_and_caches, and ILATM2 field parsing (lon 0..360, '****' fill, comment
and short lines) by test_index_missions.test_parse_fields_*.
"""
import numpy as np


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
