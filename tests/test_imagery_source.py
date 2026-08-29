"""Imagery source selection: region-aware default, and the S2 -> EOX fallback.

"s2" reads real Sentinel-2 L2A scenes from the in-region bucket (fast on the box) but has genuine coverage gaps —
notably at the high latitudes this tool mostly looks at. "eox" is a global cloudless mosaic that always has something,
but is a cross-Atlantic tile fetch. So s2 is preferred in-region and falls back."""
import pytest

from aicesat import imagery

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
