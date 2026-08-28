"""Package a built ATL06 index into one tar for transfer to another machine.

Run this on the machine that built the index (CryoCloud), then download the tar through the JupyterLab
file browser and unpack it on the laptop with scripts/import_index.py.

  uv run python scripts/export_index.py [--res 5] [--out DIR] [--force]

The index is portable by construction: every row stores both the HTTPS `url` and the `s3://` `s3url`
(index_atl06.py:130), and access.access_url() picks between them at query time — so an index built
in-region on S3 works unchanged on a laptop over HTTPS.
"""
import argparse
import hashlib
import io
import json
import logging
import sys
import tarfile
import time
from pathlib import Path

from aicesat import index_atl06, index_manifest


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _commit() -> str:
    """The checkout's commit, so import can refuse an index built from incompatible code."""
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parents[1]).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("export_index")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", type=int, default=index_atl06.ATL06_RES, help="H3 resolution of the index to export")
    ap.add_argument("--out", type=Path, default=Path.cwd(), help="directory to write the tar into")
    ap.add_argument("--force", action="store_true", help="export even if the build did not finish cleanly")
    args = ap.parse_args()

    d = index_atl06._index_dir(args.res)
    if not d.exists():
        log.error("no index at %s — build it first with scripts/build_atl06_index.py", d)
        sys.exit(2)
    parquets = sorted(d.glob("*.parquet"))          # glob skips the hidden .NAME.parquet.tmp files by design
    if not parquets:
        log.error("no parquet files in %s — nothing to export", d)
        sys.exit(2)
    mf = index_manifest.read(d)
    if mf is None:
        log.error("no %s in %s — the index would be unusable on the other machine", index_manifest.MANIFEST, d)
        sys.exit(2)
    if not index_manifest.is_complete(d) and not args.force:
        log.error("build is not marked complete (interrupted, or granules failed) — re-run the build, or pass "
                  "--force to ship a partial index that will still claim full coverage of its region")
        sys.exit(2)

    regions = index_manifest.regions(d)
    export = {"collection": "ATL06", "index_version": index_atl06.ATL06_INDEX_VERSION, "res": args.res,
              "regions": regions, "granules": len(parquets), "target": mf.get("target"),
              "complete": bool(mf.get("complete")), "commit": _commit(), "created": time.time()}

    w, s, e, n = regions[-1] if regions else (0, 0, 0, 0)
    stamp = f"w{w:g}_s{s:g}_e{e:g}_n{n:g}".replace("-", "m").replace(".", "p")
    # Parquet is already snappy-compressed, so a plain tar costs nothing and saves the CPU/wall time of gzip.
    out = args.out.expanduser().resolve() / f"atl06-index-res{args.res}-{stamp}-{export['commit'][:8]}.tar"
    out.parent.mkdir(parents=True, exist_ok=True)

    log.info("packing %d parquet files + %s from %s", len(parquets), index_manifest.MANIFEST, d)
    tmp = out.with_suffix(".tar.partial")
    with tarfile.open(tmp, "w") as tar:
        for p in parquets:
            tar.add(p, arcname=f"atl06/res{args.res}/{p.name}")
        tar.add(index_manifest.path(d), arcname=f"atl06/res{args.res}/{index_manifest.MANIFEST}")
        info = tarfile.TarInfo("EXPORT.json")
        blob = json.dumps(export, indent=1).encode()
        info.size = len(blob)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(blob))
    tmp.replace(out)

    mb = out.stat().st_size / 1e6
    print(f"\nwrote {out}")
    print(f"  {mb:.1f} MB · {len(parquets)} granules · res {args.res} · regions {regions}")
    print(f"  sha256 {_sha256(out)}")
    print("\nDownload it from the JupyterLab file browser, then on the laptop:")
    print(f"  uv run python scripts/import_index.py ~/Downloads/{out.name} --dry-run")
    print(f"  uv run python scripts/import_index.py ~/Downloads/{out.name}")


if __name__ == "__main__":
    main()
