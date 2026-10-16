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
    meta.update(index.cells_metadata(cells, dirpath) if cells else {})
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
    rec = index.compacted_cellset(children)
    assert len(rec) < len(children), f"{len(children)} cells stored as {len(rec)}"
    assert index.cells_within(set(rec), children[:20])


# --- the record is a hash; the set lives once, beside the files --------------------------------------------------
def _legacy_granule(dirpath, name, cells):
    """A file written before the sidecar: the compacted list inline under CELLS_KEY."""
    dirpath.mkdir(parents=True, exist_ok=True)
    meta = {"aicesat_atl06_index_version": "v9", "h3_res": "5", "built_at": "2026-01-01T00:00:00+00:00",
            index.CELLS_KEY: index.cellset_text(index.compacted_cellset(cells))}
    pq.write_table(pa.table({"granule": [name]}).replace_schema_metadata(meta), dirpath / f"{name}.parquet")
    return dirpath / f"{name}.parquet"


def test_the_footer_holds_a_hash_and_the_set_lives_once_beside_the_files(d):
    """The compacted list was ~49 KB stamped into EVERY granule file (3.3 GiB of a 7 GiB store). Now the footer
    carries a 40-char sha1 and one sidecar per distinct set."""
    p1 = _granule(d, "G1", SMALL)
    p2 = _granule(d, "G2", SMALL)
    p3 = _granule(d, "G3", SMALL + EXTRA)
    for p in (p1, p2, p3):
        meta = pq.read_schema(p).metadata
        assert index.CELLS_KEY.encode() not in meta, "the inline list is back"
        sha = meta[index.CELLSET_KEY.encode()].decode()
        assert len(sha) == 40 and (d / index.CELLSETS_DIR / f"{sha}.json").exists()
    assert len(list((d / index.CELLSETS_DIR).glob("*.json"))) == 2, "one sidecar per DISTINCT set"
    assert index.granule_proves(p1, SMALL) and not index.granule_proves(p1, EXTRA)
    assert index.granule_proves(p3, SMALL + EXTRA)


def test_a_legacy_inline_record_still_proves(d):
    p = _legacy_granule(d, "OLD", SMALL)
    assert index.granule_cells(p) == set(index.compacted_cellset(SMALL))
    assert index.granule_proves(p, SMALL) is True
    assert index.granule_proves(p, EXTRA) is False


def test_a_missing_sidecar_is_unknown_provenance(d):
    """A file whose set was lost cannot vouch for anything; with nothing asked of it, it is still left alone."""
    p = _granule(d, "G1", SMALL)
    sha = pq.read_schema(p).metadata[index.CELLSET_KEY.encode()].decode()
    index._CELLSET_CACHE.clear()
    (d / index.CELLSETS_DIR / f"{sha}.json").unlink()
    assert index.granule_cells(p) is None
    assert index.granule_proves(p, SMALL) is False
    assert index.granule_proves(p, []) is True


def test_migrate_footer_moves_the_list_and_keeps_every_other_key(d):
    # a scattered set that does not compact away, so the inline text is the multi-KB string the real files carry
    many = [h3.latlng_to_cell(60.0 + i * 0.05, -50.0 + (i % 7) * 0.3, 9) for i in range(400)]
    p = _legacy_granule(d, "OLD", many)
    before = pq.read_schema(p).metadata
    inline = before[index.CELLS_KEY.encode()].decode()
    size_before = p.stat().st_size
    assert index.migrate_footer(p) is True
    after = pq.read_schema(p).metadata
    assert index.CELLS_KEY.encode() not in after
    sha = after[index.CELLSET_KEY.encode()].decode()
    assert sha == index.cellset_sha1(inline), "the sidecar must be addressed by the hash of the text that was inline"
    assert json.loads((d / index.CELLSETS_DIR / f"{sha}.json").read_text()) == json.loads(inline)
    for k, v in before.items():
        if k != index.CELLS_KEY.encode():
            assert after[k] == v, f"footer key {k!r} changed"
    assert pq.read_table(p).to_pydict() == {"granule": ["OLD"]}
    assert p.stat().st_size < size_before - 1000, (p.stat().st_size, size_before)
    assert index.granule_proves(p, many) is True
    assert index.migrate_footer(p) is False, "a second pass must be a no-op"
    assert index.migrate_footer(_granule(d, "NEW", SMALL)) is False
    assert index.migrate_footer(_granule(d, "NONE", None)) is False


def test_a_new_build_shares_the_sidecar_a_migration_wrote(d):
    """The hash of the inline text equals the hash cells_metadata computes for the same set, so migrated and freshly
    built files point at ONE sidecar."""
    old = _legacy_granule(d, "OLD", SMALL)
    index.migrate_footer(old)
    new = _granule(d, "NEW", SMALL)
    assert pq.read_schema(old).metadata[index.CELLSET_KEY.encode()] == pq.read_schema(new).metadata[index.CELLSET_KEY.encode()]
    assert len(list((d / index.CELLSETS_DIR).glob("*.json"))) == 1


def test_recording_a_cellset_leaves_the_coverage_manifest_fresh(d):
    """`_cellsets/` is a subdir, so writing into it must not move the index dir's mtime and force a manifest rebuild."""
    from aicesat import coverage
    d.mkdir(parents=True, exist_ok=True)
    tbl = pa.table({"granule": ["G1"], "h3_cell": pa.array([h3.str_to_int(SMALL[0])], pa.uint64())})
    pq.write_table(tbl.replace_schema_metadata(index.cells_metadata(SMALL, d)), d / "G1.parquet")
    coverage._build_manifest(d, "'2019-01'", coverage._source_parquets(d))
    assert coverage._manifest_fresh(d)
    index.record_cellset(d, index.cellset_text(index.compacted_cellset(EXTRA)))
    assert coverage._manifest_fresh(d), "a sidecar write invalidated the manifest"


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
