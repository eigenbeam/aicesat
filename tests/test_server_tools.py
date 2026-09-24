"""The model-facing MCP tools that build or extend a scene, exercised offline with the pipeline stubbed out.

Nothing else calls these two tools, so a change to build_scene's defaults or to a collection's meta shape broke
them silently: show_photons stopped asking for ATL03 when ATL03 became opt-in, and add_glas read a meta key the
index-only GLAS extract no longer produces.
"""
import numpy as np

from aicesat import cache, glas, scene, server


def _icesat2_doc(sid="s1"):
    meta = {"n": 3, "product": "ATL03 v007", "native_frame": "ITRF2014", "height_ref": "WGS84 ellipsoid",
            "window": ["2018-10-14", "2025-12-31"]}
    return {"scene_id": sid, "bbox": [-50.0, 69.0, -49.9, 69.1], "series": {"ICESAT2": {"meta": meta, "granules": []}}}


def test_show_photons_asks_build_scene_for_atl03(monkeypatch):
    seen = {}

    def fake_build_scene(*args, **kwargs):
        seen.update(kwargs)
        return _icesat2_doc()

    monkeypatch.setattr(server, "build_scene", fake_build_scene)
    monkeypatch.setattr(server, "widget_url", lambda sid: "")
    out = server.show_photons(bbox=[-50.0, 69.0, -49.9, 69.1])
    assert seen.get("with_atl03") is True, "ATL03 is opt-in in build_scene; show_photons must ask for it"
    assert seen.get("with_glas") is False
    assert out["n_photons"] == 3


def test_add_glas_returns_provenance_from_the_index_extract(monkeypatch):
    # The meta shape glas.extract produces today: no "granules" key, access stats under "access".
    meta = {"mission": "GLAS", "product": "GLAH06 v034", "native_frame": "ITRF2008",
            "height_ref": "WGS84 ellipsoid (converted)", "ellipsoid_correction": "h = ...", "n": 2,
            "campaigns": {"L2A": 2}, "access": {"requests": 1, "bytes": 10}, "cache_key": "k"}
    arrays = {k: np.zeros(2) for k in ("lon", "lat", "h")} | {"t": np.zeros(2, "datetime64[ms]")}
    monkeypatch.setattr(cache, "load_scene", lambda sid: {"bbox": [-50.0, 69.0, -49.9, 69.1], "series": {}})
    monkeypatch.setattr(cache, "save_scene", lambda sid, doc: None)
    monkeypatch.setattr(glas, "extract", lambda bbox, window: (arrays, meta))
    monkeypatch.setattr(scene, "add_series", lambda *a, **k: None)
    monkeypatch.setattr(server, "widget_url", lambda sid: "")
    out = server.add_glas("s1")
    assert out["n_shots"] == 2 and out["campaigns"] == {"L2A": 2}
    assert out["access"] == {"requests": 1, "bytes": 10}
