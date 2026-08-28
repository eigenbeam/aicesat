"""Import an ATL06 index tar built on another machine (see scripts/export_index.py).

  uv run python scripts/import_index.py INDEX.tar [--dry-run] [--force]

Parquet files merge as a pure set union — every consumer globs the index directory, so regions built
on different machines coexist. The manifest is the part that needs care: coverage is decided by
_build.json alone, so a naive copy would clobber the regions already claimed here. This merges them
via index_manifest.record() instead, and never unions disjoint regions into one box.
"""
import argparse
import json
import logging
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from aicesat import index_atl06, index_manifest


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("import_index")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tar", type=Path, help="the .tar produced by scripts/export_index.py")
    ap.add_argument("--dry-run", action="store_true", help="report what would change and exit")
    ap.add_argument("--force", action="store_true", help="import despite an index-version or res mismatch")
    args = ap.parse_args()

    tar_path = args.tar.expanduser()
    if not tar_path.exists():
        log.error("no such file: %s", tar_path); sys.exit(2)

    with tempfile.TemporaryDirectory(prefix="aicesat-import-") as td:
        tmp = Path(td)
        with tarfile.open(tar_path) as tar:
            tar.extractall(tmp, filter="data")

        ex_path = tmp / "EXPORT.json"
        if not ex_path.exists():
            log.error("%s has no EXPORT.json — not an index tar from scripts/export_index.py", tar_path); sys.exit(2)
        export = json.loads(ex_path.read_text())

        res = int(export.get("res", index_atl06.ATL06_RES))
        # A stale-schema parquet is not deleted by the resume logic and DuckDB scans the whole directory
        # with read_parquet('dir/*.parquet'), so mixing index versions can break schema unification.
        if str(export.get("index_version")) != str(index_atl06.ATL06_INDEX_VERSION):
            msg = (f"index version mismatch: tar has {export.get('index_version')!r}, this checkout expects "
                   f"{index_atl06.ATL06_INDEX_VERSION!r} — rebuild on the same commit, or pass --force")
            if not args.force:
                log.error("%s", msg); sys.exit(2)
            log.warning("%s (forced)", msg)
        if res != index_atl06.ATL06_RES and not args.force:
            log.error("res mismatch: tar is res %d, this checkout defaults to res %d — pass --force if deliberate",
                      res, index_atl06.ATL06_RES)
            sys.exit(2)

        src = tmp / "atl06" / f"res{res}"
        incoming = sorted(src.glob("*.parquet"))
        if not incoming:
            log.error("no parquet files inside %s", tar_path); sys.exit(2)
        src_mf = index_manifest.read(src)
        if src_mf is None:
            log.error("tar has no %s — the index would claim no coverage", index_manifest.MANIFEST); sys.exit(2)
        if not src_mf.get("complete") and not args.force:
            log.error("the exported build was not complete — it claims coverage it may not have. Pass --force "
                      "to import anyway.")
            sys.exit(2)

        dst = index_atl06._index_dir(res)
        have = {p.stem for p in dst.glob("*.parquet")} if dst.exists() else set()
        new = [p for p in incoming if p.stem not in have]
        overlap = len(incoming) - len(new)
        local_only = len(have - {p.stem for p in incoming})
        before = index_manifest.regions(dst)
        add = index_manifest.regions(src)

        print(f"tar     : {tar_path}")
        print(f"built at: commit {str(export.get('commit'))[:8]} · res {res} · {len(incoming)} granules")
        print(f"target  : {dst}")
        print(f"  {len(new)} new granule(s), {overlap} already present, {local_only} local-only kept")
        print(f"  regions here now : {before or '(none)'}")
        print(f"  regions incoming : {add}")
        if args.dry_run:
            print("\n--dry-run: nothing written")
            return

        dst.mkdir(parents=True, exist_ok=True)
        for p in new:
            shutil.copy2(p, dst / p.name)

        # target must count every granule the directory holds, not just the incoming region's.
        target = int(src_mf.get("target") or len(incoming)) + local_only
        for r in add:
            index_manifest.record(dst, r, res, target=target, complete=True)

        after = index_manifest.regions(dst)
        print(f"\nimported {len(new)} granule(s) into {dst}")
        print(f"  regions now: {after}")
        print(f"  total granules on disk: {len(list(dst.glob('*.parquet')))}")
        print("\nVerify:  curl 'http://127.0.0.1:8765/api/index_status?collection=ATL06'")


if __name__ == "__main__":
    main()
