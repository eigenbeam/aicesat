"""Enlarging an indexed area must extend the DATA, not just the claim.

The bug: a granule's rows are filtered to the cells its build asked for, but the file recorded only a schema version.
A later build over a larger bbox skipped that granule BY NAME, so its rows for the added ring were never written —
while write_build_manifest stamped a claim covering the ring anyway. Scenes there then built "successfully" from
missing data. Silent by construction: no error, just a short scene.

The fix records the filter set per granule and asks whether a file can PROVE it covers the new ground. Two properties
have to hold together, and they pull in opposite directions:

  * re-running the SAME area must re-index NOTHING (or every resume becomes a full rebuild), and
  * enlarging must re-index the granules that cannot prove they cover the added ring.

A file with no recorded cells (built before this change) can never prove anything, so it is rebuilt on demand rather
than deleted up front — which is what keeps an existing index usable instead of requiring rm *.parquet.
"""
import json

import h3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import index

SMALL = [h3.latlng_to_cell(69.00, -50.00, 9), h3.latlng_to_cell(69.01, -50.01, 9)]
EXTRA = [h3.latlng_to_cell(69.50, -49.50, 9)]          # the ring added by enlarging


@pytest.fixture
def d(tmp_path):
    return tmp_path / "res5"


def _granule(dirpath, name, cells):
    """Write a granule file the way build_*_index does; `cells=None` is a legacy file with no provenance."""
    dirpath.mkdir(parents=True, exist_ok=True)
    tbl = pa.table({"granule": [name]})
    meta = {"aicesat_atl06_index_version": "v9", "h3_res": "5"}
    meta.update(index.cells_metadata(cells) if cells else {})
    pq.write_table(tbl.replace_schema_metadata(meta), dirpath / f"{name}.parquet")
    return dirpath / f"{name}.parquet"


# --- the ground arithmetic -----------------------------------------------------------------------------------
def test_rerunning_the_same_area_finds_no_new_ground(d):
    index.write_build_manifest(d, [-50.1, 68.9, -49.9, 69.1], 5, None, 1, cells=SMALL)
    assert index.unclaimed_cells(d, SMALL) == [], "a same-area re-run must not look like new ground"


def test_enlarging_reports_only_the_added_ring(d):
    index.write_build_manifest(d, [-50.1, 68.9, -49.9, 69.1], 5, None, 1, cells=SMALL)
    needed = index.unclaimed_cells(d, SMALL + EXTRA)
    assert needed == EXTRA, f"only the added cells are new ground, got {needed}"


def test_an_unbuilt_index_reports_everything_as_new(d):
    assert index.unclaimed_cells(d, SMALL) == SMALL


# --- per-granule provenance ----------------------------------------------------------------------------------
def test_a_granule_proves_the_cells_it_was_built_for(d):
    p = _granule(d, "G1", SMALL)
    assert index.granule_cells(p) is not None
    assert index.granule_proves(p, SMALL) is True


def test_a_granule_cannot_prove_ground_it_was_not_built_for(d):
    p = _granule(d, "G1", SMALL)
    assert index.granule_proves(p, EXTRA) is False, "this is the skip that silently lost rows"


def test_a_legacy_granule_proves_nothing(d):
    """Files written before the provenance record. They are not deleted — they are rebuilt only when a build
    actually needs ground they cannot vouch for."""
    p = _granule(d, "G1", None)
    assert index.granule_cells(p) is None
    assert index.granule_proves(p, EXTRA) is False


def test_nothing_is_asked_of_any_granule_when_there_is_no_new_ground(d):
    """The property that keeps a plain resume cheap: with `needed` empty, even a legacy file is left alone."""
    p = _granule(d, "G1", None)
    assert index.granule_proves(p, []) is True


def test_a_coarser_recorded_cell_covers_its_children(d):
    """Records are compacted, so a res-5 parent may stand for the res-9 cells inside it. Ancestry, not equality."""
    parent = h3.cell_to_parent(SMALL[0], 5)
    p = _granule(d, "G1", [parent])
    assert index.granule_proves(p, [SMALL[0]]) is True
    assert index.granule_proves(p, EXTRA) is False


def test_the_recorded_set_is_compacted(d):
    """A whole-region build must store a handful of ids, not millions: the metadata rides in every granule file."""
    parent = h3.cell_to_parent(SMALL[0], 5)
    children = h3.cell_to_children(parent, 9)
    rec = json.loads(index.cells_metadata(children)[index.CELLS_KEY])
    assert len(rec) < len(children), f"{len(children)} cells stored as {len(rec)}"
    assert index.cells_within(set(rec), children[:20])


# --- the decision the build scripts make ------------------------------------------------------------------------
def _todo(dirpath, names, done, needed):
    """Exactly the expression in scripts/build_*_index.py."""
    return [n for n in names
            if n not in done or not index.granule_proves(dirpath / f"{n}.parquet", needed)]


def test_same_area_rerun_rebuilds_nothing_even_for_legacy_files(d):
    index.write_build_manifest(d, [-50.1, 68.9, -49.9, 69.1], 5, None, 1, cells=SMALL)
    _granule(d, "G1", None)                     # legacy
    _granule(d, "G2", SMALL)
    needed = index.unclaimed_cells(d, SMALL)
    assert _todo(d, ["G1", "G2"], {"G1", "G2"}, needed) == []


def test_enlarging_rebuilds_exactly_the_granules_that_cannot_prove_the_new_ring(d):
    index.write_build_manifest(d, [-50.1, 68.9, -49.9, 69.1], 5, None, 1, cells=SMALL)
    _granule(d, "OLD", SMALL)                   # built for the small area only -> must be rebuilt
    _granule(d, "WIDE", SMALL + EXTRA)          # already covers the ring -> must be left alone
    _granule(d, "LEGACY", None)                 # unknown provenance -> must be rebuilt
    needed = index.unclaimed_cells(d, SMALL + EXTRA)
    todo = _todo(d, ["OLD", "WIDE", "LEGACY"], {"OLD", "WIDE", "LEGACY"}, needed)
    assert sorted(todo) == ["LEGACY", "OLD"], f"got {sorted(todo)}"


def test_a_granule_that_was_never_indexed_is_always_built(d):
    index.write_build_manifest(d, [-50.1, 68.9, -49.9, 69.1], 5, None, 1, cells=SMALL)
    needed = index.unclaimed_cells(d, SMALL)
    assert _todo(d, ["NEW"], set(), needed) == ["NEW"]
