"""Integrity check: does each index's coverage CLAIM match the granule files actually on disk?

An index makes two independent assertions, and a killed build can break the link between them:

  * the granule files in the index dir  -- the rows a scene will actually read
  * the claim in _build.json           -- the ground coverage reports as indexed

When the claim covers ground the files do not, a scene there builds "successfully" from missing data. That is the
failure this catches, and it is silent by construction: nothing errors, the scene is just short. Builds before
2ab3d38 stamped the claim BEFORE indexing, so every interrupted run of that vintage over-claims.

`_build.json`'s `target` is the granule count the build intended, so `target` vs. the on-disk count at the current
schema version is a completeness check needing no network.

Read-only by default. `--fix` drops claims that the files do not back; it never deletes granule files, and the next
build re-stamps exactly what it rebuilt.

usage: uv run scripts/check_index.py [--fix]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import pyarrow.parquet as pq

from aicesat import index, index_atl06, index_glas, index_icessn

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

# (label, dir, current version, the schema-metadata key that stamps it)
COLLECTIONS = (
    ("ATL03", index.ATL03_INDEX_DIR, index.INDEX_SCHEMA_VERSION, b"aicesat_index_version"),
    ("ATL06", index_atl06._index_dir(index_atl06.ATL06_RES), index_atl06.ATL06_INDEX_VERSION,
     b"aicesat_atl06_index_version"),
    ("GLAS", index_glas._index_dir(index_glas.GLAS_RES), index_glas.GLAS_INDEX_VERSION,
     b"aicesat_glas_index_version"),
    ("ICESSN", index_icessn._index_dir(index_icessn.ICESSN_RES), index_icessn.ICESSN_INDEX_VERSION,
     b"aicesat_icessn_index_version"),
)


def survey(d, version: str, key: bytes) -> dict:
    """Count what is on disk, WITHOUT the side effects of indexed_*_granules().

    Those helpers delete stale/unreadable files and invalidate the claim as they scan -- correct for a build, wrong
    for a check, which must be able to report a problem without also changing it.
    """
    if not d.exists():
        return {"exists": False}
    cur = old = bad = 0
    for p in d.glob("*.parquet"):
        try:
            meta = pq.read_schema(p).metadata or {}
        except Exception:
            bad += 1          # a half-written file from a killed build
            continue
        if meta.get(key, b"").decode(errors="replace") == version:
            cur += 1
        else:
            old += 1
    doc = {}
    mf = d / "_build.json"
    if mf.exists():
        try:
            doc = json.loads(mf.read_text())
        except Exception as e:
            doc = {"_unreadable": str(e)}
    return {"exists": True, "current": cur, "old_schema": old, "unreadable": bad,
            "tmp": len(list(d.glob(".*.tmp"))), "target": doc.get("target"),
            "claim_cells": len(doc.get("cells") or []), "claimed": bool(doc.get("cells"))}


def verdict(s: dict) -> tuple[str, bool]:
    """-> (message, claim_is_unbacked). The bool is what --fix acts on."""
    target, cur = s["target"], s["current"]
    problems = []
    if s["old_schema"]:
        problems.append(f"{s['old_schema']} file(s) at an old schema (the next build deletes these and drops the claim)")
    if s["unreadable"]:
        problems.append(f"{s['unreadable']} unreadable file(s) -- a killed build mid-write")
    if s["tmp"]:
        problems.append(f"{s['tmp']} leftover .tmp file(s)")

    if target is None:
        return ("no claim stamped" + (f"; {'; '.join(problems)}" if problems else ""), False)
    missing = target - cur
    if missing > 0 and s["claimed"]:
        problems.insert(0, f"OVER-CLAIMED: claims {s['claim_cells']} cells but {missing} of {target} "
                           f"granules are not indexed")
        return ("; ".join(problems), True)
    if missing > 0:
        problems.insert(0, f"incomplete: {missing} of {target} granules not indexed (claim correctly withheld)")
        return ("; ".join(problems), False)
    return ("complete" + (f"; {'; '.join(problems)}" if problems else ""), False)


a = argparse.ArgumentParser()
a.add_argument("--fix", action="store_true", help="drop claims the granule files do not back")
args = a.parse_args()

bad_any = False
for label, d, version, key in COLLECTIONS:
    s = survey(d, version, key)
    if not s["exists"]:
        print(f"{label:<7} no index directory")
        continue
    msg, unbacked = verdict(s)
    print(f"{label:<7} files={s['current']:>6} current  old={s['old_schema']:>4}  unreadable={s['unreadable']:>3}"
          f"  target={s['target']}")
    print(f"        {msg}")
    if unbacked:
        bad_any = True
        if args.fix:
            index.invalidate_claim(d, "granule files on disk do not back the claim (interrupted build)")
            print("        -> claim dropped; re-run the build to re-stamp what it rebuilds")

if bad_any and not args.fix:
    print("\nRe-run with --fix to drop the unbacked claim(s). Granule files are kept either way, so the "
          "build resumes rather than restarting.")
sys.exit(1 if bad_any else 0)
