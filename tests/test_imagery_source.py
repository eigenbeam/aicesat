"""Imagery source selection: region-aware default, and the S2 -> EOX fallback.

"s2" reads real Sentinel-2 L2A scenes from the in-region bucket (fast on the box) but has genuine coverage gaps —
notably at the high latitudes this tool mostly looks at. "eox" is a global cloudless mosaic that always has something,
but is a cross-Atlantic tile fetch. So s2 is preferred in-region and falls back."""
import numpy as np
import pytest

from aicesat import imagery


class _IdentityTransformer:
    """Stands in for pyproj: these tests work directly in the frame's own metres."""
    @staticmethod
    def from_crs(a, b, always_xy=True):
        class _T:
            @staticmethod
            def transform(x, y):
                return x, y
        return _T()


_FRAME = {"crs": "EPSG:3413", "origin_xy": (0.0, 0.0)}

FRAME = {"crs": "EPSG:3413", "origin_xy": [0.0, 0.0]}
EXTENT = (-1000.0, -1000.0, 1000.0, 1000.0)
S2_META = {"source": "s2", "width": 256}
EOX_META = {"source": "eox", "width": 256}


@pytest.fixture
def stub(monkeypatch):
    calls = []
    monkeypatch.setattr(imagery, "_build_s2", lambda *a, **k: (calls.append("s2"), dict(S2_META))[1])
    monkeypatch.setattr(imagery, "_build_eox", lambda *a, **k: (calls.append("eox"), dict(EOX_META))[1])
    monkeypatch.delenv("AICESAT_IMAGERY", raising=False)
    return calls


def _fail_s2(monkeypatch, calls, exc=RuntimeError("Sentinel-2 scene did not cover the requested area")):
    def boom(*a, **k):
        calls.append("s2"); raise exc
    monkeypatch.setattr(imagery, "_build_s2", boom)


def test_default_is_s2_in_region(stub, monkeypatch):
    monkeypatch.setattr("aicesat.access.in_region", lambda: True)
    assert imagery.build(FRAME, EXTENT)["source"] == "s2"
    assert stub == ["s2"]


def test_default_is_eox_out_of_region(stub, monkeypatch):
    monkeypatch.setattr("aicesat.access.in_region", lambda: False)
    assert imagery.build(FRAME, EXTENT)["source"] == "eox"
    assert stub == ["eox"]


def test_env_overrides_the_region_default(stub, monkeypatch):
    monkeypatch.setattr("aicesat.access.in_region", lambda: True)
    monkeypatch.setenv("AICESAT_IMAGERY", "eox")
    assert imagery.build(FRAME, EXTENT)["source"] == "eox"


def test_s2_gap_falls_back_to_eox_when_defaulted(stub, monkeypatch):
    """The real box case: in-region, S2 has no scene covering a Greenland bbox -> imagery must still appear."""
    monkeypatch.setattr("aicesat.access.in_region", lambda: True)
    _fail_s2(monkeypatch, stub)
    assert imagery.build(FRAME, EXTENT)["source"] == "eox"
    assert stub == ["s2", "eox"]                      # tried the fast one first, then fell back


def test_explicit_s2_request_does_not_silently_fall_back(stub, monkeypatch):
    """An explicit source is the user's choice — surface its failure instead of quietly serving something else."""
    monkeypatch.setattr("aicesat.access.in_region", lambda: True)
    _fail_s2(monkeypatch, stub)
    with pytest.raises(RuntimeError):
        imagery.build(FRAME, EXTENT, source="s2")
    assert stub == ["s2"]                             # no eox attempt


# ---------------------------------------------------------------------------- Sentinel-2 mosaic (issue: black imagery)
def _item(iid, bbox, epsg=32622, cloud=5.0, sun=40.0):
    return {"id": iid, "bbox": list(bbox),
            "properties": {"proj:epsg": epsg, "eo:cloud_cover": cloud, "view:sun_elevation": sun,
                           "datetime": "2026-08-24T00:00:00Z"},
            "assets": {k: {"href": f"https://x/{iid}/{k}.tif"} for k in imagery.S2_BANDS}}


def test_candidates_put_fully_covering_scenes_first_but_keep_the_rest():
    """The rest matter: no single 110 km granule can cover a larger area, so the mosaic needs the partial ones."""
    area = (-51.0, 69.0, -49.0, 69.4)
    full = _item("full", (-52, 68, -48, 70))
    part = _item("part", (-51.5, 68.9, -50.0, 69.5))
    miss = _item("miss", (10, 10, 11, 11))
    cands = imagery._s2_candidates([part, full, miss], area)
    assert cands[0]["id"] == "full", "a covering scene should still be preferred first"
    assert {c["id"] for c in cands} == {"full", "part", "miss"}, "partial scenes must remain available to the mosaic"


def test_mosaic_fills_gaps_from_later_scenes(monkeypatch, tmp_path):
    """Two half-covering scenes must produce a whole image, where the old single-pick produced half black."""
    monkeypatch.setattr(imagery, "IMG_DIR", tmp_path)
    # One synthetic coordinate space throughout: _to_ll and Transformer are both identity, so the frame's metres ARE
    # the lon/lat the item bboxes are written in. Otherwise the gap test compares real polar lon/lat against made-up
    # boxes and skips every scene.
    monkeypatch.setattr(imagery, "_to_ll", lambda crs: _IdentityTransformer.from_crs(crs, "EPSG:4326"))
    monkeypatch.setattr(imagery, "Transformer", _IdentityTransformer)
    west = _item("west", (-2000, -2000, 0, 2000))
    east = _item("east", (0, -2000, 2000, 2000))
    monkeypatch.setattr(imagery, "_s2_search", lambda *a, **k: [west, east])

    def fake_sample(url, ux, uy):
        out = np.full(ux.shape, np.nan)
        out[ux < 0 if "west" in url else ux >= 0] = 1000.0
        return out

    monkeypatch.setattr(imagery, "_sample_utm", fake_sample)
    meta = imagery._build_s2(_FRAME, (-1000.0, -1000.0, 1000.0, 1000.0), width_px=64)
    assert meta["coverage"] == 1.0, meta
    assert sorted(meta["scenes"]) == ["east", "west"], meta["scenes"]
    assert "mosaic of 2 scenes" in meta["source"]


def test_a_mosaic_that_cannot_cover_the_area_raises_rather_than_going_black(monkeypatch, tmp_path):
    """A gap must reach build()'s EOX fallback, not be painted as dark ground."""
    monkeypatch.setattr(imagery, "IMG_DIR", tmp_path)
    monkeypatch.setattr(imagery, "_to_ll", lambda crs: _IdentityTransformer.from_crs(crs, "EPSG:4326"))
    monkeypatch.setattr(imagery, "Transformer", _IdentityTransformer)
    half = _item("half", (-2000, -2000, 0, 2000))
    monkeypatch.setattr(imagery, "_s2_search", lambda *a, **k: [half])
    monkeypatch.setattr(imagery, "_sample_utm", lambda url, ux, uy: np.where(ux < 0, 1000.0, np.nan))
    with pytest.raises(RuntimeError, match="covered only"):
        imagery._build_s2(_FRAME, (-1000.0, -1000.0, 1000.0, 1000.0), width_px=64)


def test_a_candidate_that_cannot_touch_the_remaining_gap_is_not_read(monkeypatch, tmp_path):
    """Each candidate costs three COG reads; one that only clips already-filled ground must be skipped."""
    monkeypatch.setattr(imagery, "IMG_DIR", tmp_path)
    monkeypatch.setattr(imagery, "_to_ll", lambda crs: _IdentityTransformer.from_crs(crs, "EPSG:4326"))
    monkeypatch.setattr(imagery, "Transformer", _IdentityTransformer)
    whole = _item("whole", (-2000, -2000, 2000, 2000))
    corner = _item("corner", (-2000, -2000, -1500, -1500))     # inside the area, but wholly within what `whole` fills
    monkeypatch.setattr(imagery, "_s2_search", lambda *a, **k: [whole, corner])
    read = []

    def fake_sample(url, ux, uy):
        read.append(url.split("/")[-2])
        return np.full(ux.shape, 1000.0)

    monkeypatch.setattr(imagery, "_sample_utm", fake_sample)
    meta = imagery._build_s2(_FRAME, (-1000.0, -1000.0, 1000.0, 1000.0), width_px=32)
    assert meta["coverage"] == 1.0
    assert set(read) == {"whole"}, f"corner should never have been read: {set(read)}"
