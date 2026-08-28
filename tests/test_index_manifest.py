"""Offline unit tests for the index build manifest — which regions an index claims to cover.

The manifest is the single point where copying an index between machines can silently break (a clobbered
region stops answering) or silently overclaim (a unioned box claims the gap between two disjoint regions
and returns empty data instead of an honest error). These pin both behaviours down.
"""
import json

from aicesat import index_manifest as im

SW = [-52.0, 62.0, -44.0, 70.0]           # SW Greenland — the default build region
NE = [-32.0, 75.5, -29.0, 76.0]           # NE flank — disjoint from SW
JAK = [-50.3, 68.9, -49.2, 69.3]          # Jakobshavn — inside SW
GAP = [-40.0, 72.0, -38.0, 73.0]          # between the two, built by neither


# ---- coverage ---------------------------------------------------------------------------------------
def test_covers_bbox_inside_a_built_region(tmp_path):
    im.record(tmp_path, SW, 5, target=2333, complete=True)
    assert im.covers(tmp_path, JAK)


def test_does_not_cover_a_box_outside_every_region(tmp_path):
    im.record(tmp_path, SW, 5, target=2333, complete=True)
    assert not im.covers(tmp_path, NE)


def test_two_regions_coexist_after_a_second_import(tmp_path):
    im.record(tmp_path, SW, 5, target=2333, complete=True)
    im.record(tmp_path, NE, 5, target=2500, complete=True)
    assert im.covers(tmp_path, JAK)                    # the first region still answers — no clobber
    assert im.covers(tmp_path, NE)
    assert len(im.regions(tmp_path)) == 2


def test_gap_between_disjoint_regions_is_never_claimed(tmp_path):
    """The whole reason regions are kept as a list rather than unioned into one box."""
    im.record(tmp_path, SW, 5, target=2333, complete=True)
    im.record(tmp_path, NE, 5, target=2500, complete=True)
    assert not im.covers(tmp_path, GAP)


def test_missing_and_corrupt_manifests_claim_nothing(tmp_path):
    assert im.read(tmp_path) is None
    assert im.regions(tmp_path) == []
    assert not im.covers(tmp_path, JAK)
    im.path(tmp_path).write_text("{not json")
    assert not im.covers(tmp_path, JAK)
    im.path(tmp_path).write_text('{"bbox": "nonsense"}')
    assert not im.covers(tmp_path, JAK)


# ---- backward compatibility -------------------------------------------------------------------------
def test_legacy_single_bbox_manifest_still_works(tmp_path):
    """Every manifest written before the regions list has only {bbox, res, target, started}."""
    im.path(tmp_path).write_text(json.dumps({"bbox": SW, "res": 5, "target": 2333, "started": 1.0}))
    assert im.regions(tmp_path) == [SW]
    assert im.covers(tmp_path, JAK)
    assert not im.covers(tmp_path, NE)


def test_recording_onto_a_legacy_manifest_keeps_the_old_region(tmp_path):
    im.path(tmp_path).write_text(json.dumps({"bbox": SW, "res": 5, "target": 2333, "started": 1.0}))
    im.record(tmp_path, NE, 5, target=4000, complete=True)
    assert im.covers(tmp_path, JAK) and im.covers(tmp_path, NE)


# ---- record() bookkeeping ---------------------------------------------------------------------------
def test_a_contained_region_is_absorbed_not_appended(tmp_path):
    im.record(tmp_path, SW, 5, target=2333, complete=True)
    im.record(tmp_path, JAK, 5, target=2333, complete=True)      # JAK is inside SW
    assert im.regions(tmp_path) == [SW]


def test_a_larger_region_subsumes_the_smaller_one(tmp_path):
    im.record(tmp_path, JAK, 5, target=40, complete=True)
    im.record(tmp_path, SW, 5, target=2333, complete=True)       # SW contains JAK
    assert im.regions(tmp_path) == [SW]
    assert im.covers(tmp_path, JAK)


def test_legacy_bbox_key_tracks_the_last_build_not_the_union(tmp_path):
    """An un-updated reader looking only at `bbox` must under-claim, never claim the gap."""
    im.record(tmp_path, SW, 5, target=2333, complete=True)
    im.record(tmp_path, NE, 5, target=2500, complete=True)
    assert im.read(tmp_path)["bbox"] == NE


# ---- completeness -----------------------------------------------------------------------------------
def test_a_build_is_incomplete_until_marked(tmp_path):
    im.record(tmp_path, SW, 5, target=2333, complete=False)
    assert not im.is_complete(tmp_path)
    im.mark_complete(tmp_path)
    assert im.is_complete(tmp_path)
    assert im.covers(tmp_path, JAK)                    # completeness is separate from coverage


def test_pre_existing_manifests_read_as_incomplete(tmp_path):
    im.path(tmp_path).write_text(json.dumps({"bbox": SW, "res": 5, "target": 2333, "started": 1.0}))
    assert not im.is_complete(tmp_path)


def test_mark_complete_on_a_missing_manifest_is_a_noop(tmp_path):
    im.mark_complete(tmp_path)
    assert im.read(tmp_path) is None
