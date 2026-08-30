"""The public extract() wrappers, exercised for real — the layer nothing else covered.

A bug shipped here took GLAS and IceBridge out of every scene: threading `on_plan` through the wrappers, a blanket
replace of "on_granule=on_granule" hit BOTH call sites in glas/icessn (extract -> _extract_via_index -> fetch_bbox)
while only extract's signature was widened, so `_extract_via_index() got an unexpected keyword argument 'on_plan'`.
ATL06 has one call site and no intermediate, which is exactly why it was the only collection that still worked.

Nothing caught it: test_lake_cache calls index_*.fetch_bbox directly, and test_build_scene MOCKS glas.extract. The
wrappers — the functions the server actually calls — were never run. These tests run them.
"""
import json

import numpy as np
import pytest

from aicesat import atl06, cache, glas, icessn, index_atl06, index_glas, index_icessn, regions

from test_lake_cache import _build_atl06, _build_glas, _build_icessn, _lake_env, _same   # noqa: F401

BBOX = (-45.5, 69.5, -44.5, 71.5)
N = 24


def _scene(kind):
    """Lay down one collection's synthetic index PLUS the _build.json that _index_covers reads — without it the
    wrapper silently diverts to the CMR + whole-granule fallback and never reaches the code under test."""
    lat = np.linspace(69.6, 71.4, N); lon = np.full(N, -45.0); elev = np.linspace(2400.0, 2430.0, N)
    if kind == "GLAS":
        _build_glas(lat, lon, elev)
        d, mod, win = index_glas._index_dir(index_glas.GLAS_RES), glas, regions.DEFAULT_GLAS_WINDOW
    elif kind == "ICESSN":
        _build_icessn(lat, lon, elev, np.full(N, 4.5), np.zeros(N))
        d, mod, win = index_icessn._index_dir(index_icessn.ICESSN_RES), icessn, regions.DEFAULT_ICESSN_WINDOW
    else:
        _build_atl06(lat, lon, elev.astype("f4"), np.zeros(N, "i1"))
        d, mod, win = index_atl06._index_dir(index_atl06.ATL06_RES), atl06, regions.DEFAULT_ATL06_WINDOW
    d.mkdir(parents=True, exist_ok=True)
    (d / "_build.json").write_text(json.dumps({"bbox": [-50, 60, -40, 80], "res": 5, "target": 1}))
    return mod, win


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    """extract() saves an npz; keep it out of the real data directory."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "extract_cache")


@pytest.mark.parametrize("kind", ["GLAS", "ICESSN", "ATL06"])
def test_extract_accepts_and_delivers_both_callbacks(kind):
    """The server always passes on_granule AND on_plan. Both must survive the whole delegation chain."""
    mod, win = _scene(kind)
    plans, grans = [], []
    arr, meta = mod.extract(BBOX, win, on_granule=grans.append, on_plan=plans.append)
    assert arr["lon"].size > 0, f"{kind}: extract returned no points"
    assert meta["n"] == arr["lon"].size
    assert plans, f"{kind}: on_plan never fired — the progress UI would have no denominator"
    assert set(plans[0]) >= {"granules", "chunks", "cached"}, plans[0]
    assert grans, f"{kind}: on_granule never fired — nothing would stream"
    assert all("granule" in g for g in grans), grans[0].keys()


@pytest.mark.parametrize("kind", ["GLAS", "ICESSN", "ATL06"])
def test_extract_works_without_callbacks(kind):
    """The callbacks are opt-in; the default path must not depend on them."""
    mod, win = _scene(kind)
    arr, _ = mod.extract(BBOX, win)
    assert arr["lon"].size > 0


def test_every_wrapper_and_its_delegate_accept_the_same_callbacks():
    """A signature check, because the failure it guards is a TypeError raised only when the server calls it.

    extract() and the private helper it delegates to must agree on the keyword arguments, or widening one and not the
    other breaks the collection at run time while every direct-to-fetch_bbox test keeps passing.
    """
    import inspect
    for mod in (atl06, glas, icessn):
        for name in ("extract", "_extract_via_index"):
            fn = getattr(mod, name, None)
            if fn is None:
                continue                                  # atl06 calls fetch_bbox directly, with no intermediate
            params = inspect.signature(fn).parameters
            for kw in ("on_granule", "on_plan"):
                assert kw in params, f"{mod.__name__}.{name} does not accept {kw}"
