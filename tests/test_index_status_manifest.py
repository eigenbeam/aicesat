"""index_status must come from the coverage manifest, and must report OBSERVATION SPAN rather than "cycles".

Cost: index_status derived per-cell coverage by opening every per-granule index parquet — 33,064 files for ATL06 on
the deployed box, 43.5 s cold, and it is the reason the Data Lake page took ~30 s on its first load after a restart.
The manifest (_coverage/manifest.parquet, DISTINCT h3_cell/granule/ym) already holds exactly this, and answers in
0.35 s with an identical cell set (14,482 cells; GLAS 23,142 and ICESSN 7,782 also identical).

Metric: the old per-cell number was "distinct cycles", but only ICESat-2 has repeat cycles — for GLAS and IceBridge
the code silently substituted the YEAR, so one colour scale meant two different things. Span (first to last
observation) means the same thing for every collection and is the claim a cross-mission tool exists to make.
"""
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import api, coverage, index_atl06

CELL_A, CELL_B = 600000000000000000, 600000000000000001


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path / "atl06")
    api._INDEX_CACHE.clear()
    d = index_atl06._index_dir(index_atl06.ATL06_RES)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _granule(d, yyyymmdd, cells, seq="11760601"):
    """One per-granule index parquet, named and columned the way a real ATL06 index build writes it."""
    name = f"ATL06_{yyyymmdd}000000_{seq}_007_01.h5"
    t = pa.table({"h3_cell": pa.array(cells, type=pa.uint64()),
                  "granule": pa.array([name] * len(cells)),
                  "cycle": np.full(len(cells), 7, "i4")})
    tmp = d / f".{name}.tmp"
    pq.write_table(t, tmp)
    tmp.replace(d / f"{name}.parquet")      # atomic rename, as the real build does
    return name


def _cell(st, cell):
    import h3
    want = h3.int_to_str(cell)
    return next(c for c in st["cells"] if c["h"] == want)


def test_span_is_first_to_last_observation_in_years(idx):
    _granule(idx, "20190115", [CELL_A])
    _granule(idx, "20250715", [CELL_A], seq="11760602")
    c = _cell(api.index_status("ATL06"), CELL_A)
    assert (c["y0"], c["y1"]) == (2019, 2025)
    assert c["sp"] == pytest.approx(6.5, abs=0.1), "span should be first-to-last in years, from the manifest's ym"
    assert c["e"] == 2, "two distinct observation epochs"
    assert c["g"] == 2


def test_a_cell_seen_once_has_no_span(idx):
    _granule(idx, "20190115", [CELL_A])
    c = _cell(api.index_status("ATL06"), CELL_A)
    assert c["sp"] == 0.0 and c["e"] == 1, "a single epoch cannot measure change; span must read 0"


def test_span_max_is_scaled_to_this_collection(idx):
    _granule(idx, "20190115", [CELL_A, CELL_B])
    _granule(idx, "20250715", [CELL_A], seq="11760602")
    st = api.index_status("ATL06")
    assert st["span_max"] == pytest.approx(6.5, abs=0.1), "the ramp needs the collection's own widest span"
    assert _cell(st, CELL_B)["sp"] == 0.0


def test_it_does_not_open_the_per_granule_index_files(idx, monkeypatch):
    _granule(idx, "20190115", [CELL_A])
    _granule(idx, "20250715", [CELL_A, CELL_B], seq="11760602")
    first = api.index_status("ATL06")
    assert first["indexed"] and first["granules"] == 2

    api._INDEX_CACHE.clear()          # prove the SOURCE, not the dir-mtime cache in front of it
    opened = []
    for fn in ("read_table", "read_schema", "read_metadata"):
        real = getattr(pq, fn)
        monkeypatch.setattr(pq, fn, lambda p, *a, _r=real, **k: (opened.append(str(p)), _r(p, *a, **k))[1])
    again = api.index_status("ATL06")
    assert again == first, "the manifest answer differs from the first one"
    per_granule = [p for p in opened if p.endswith(".parquet") and "_coverage" not in p]
    assert not per_granule, f"opened {len(per_granule)} per-granule index files: {per_granule[:2]}"


def test_a_newly_indexed_granule_is_picked_up(idx):
    _granule(idx, "20190115", [CELL_A])
    assert api.index_status("ATL06")["granules"] == 1
    _granule(idx, "20250715", [CELL_B], seq="11760602")
    after = api.index_status("ATL06")
    assert after["granules"] == 2, "a stale manifest hid a newly indexed granule"
    assert {c["h"] for c in after["cells"]} == {__import__("h3").int_to_str(c) for c in (CELL_A, CELL_B)}


def test_the_manifest_and_a_direct_read_agree_on_the_cell_set(idx):
    """Equivalence: the manifest is a rollup, not a different answer."""
    _granule(idx, "20190115", [CELL_A, CELL_B])
    _granule(idx, "20250715", [CELL_A], seq="11760602")
    cov = coverage.cell_coverage("ATL06")
    direct = set()
    for p in idx.glob("*.parquet"):
        direct |= {int(x) for x in pq.read_table(p, columns=["h3_cell"])["h3_cell"].to_pylist()}
    assert set(cov) == direct
