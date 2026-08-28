# Building the ATL06 index on CryoCloud

Build the ICESat-2 sub-granule index in AWS us-west-2, where NSIDC S3-direct access works, then bring
the result back to the laptop.

## Why

`atl06.extract()` is index-only — there is no whole-granule fallback (`src/aicesat/atl06.py:91-94`). With
no index, every scene build silently drops ATL06 and returns only GLAS + IceBridge.

Building locally is latency-bound, not CPU-bound: commit `a581613` measured ~0.78 gran/s and notes the
remainder is "NASA byte-range latency, not CPU", so throwing more laptop cores at it buys nothing.
NSIDC S3-direct is gated to us-west-2, which is where CryoCloud runs — reads go straight to S3 with STS
credentials, no presign and no CloudFront hop (`docs/tasks/aws-us-west-2-deploy.md:6-11`).

The index is portable by construction: every row stores **both** the HTTPS `url` and the `s3://` `s3url`
(`src/aicesat/index_atl06.py:130`), and `access.access_url()` chooses between them at query time. An
index built in-region works unchanged on the laptop — it just falls back to HTTPS for the actual reads.

## Region sizes

CMR counts for ATL06 v007 over the full record:

| Region | bbox | Granules |
|---|---|---|
| SW Greenland (default) — Jakobshavn + K-transect | `-52 62 -44 70` | 2,333 |
| All six `regions.py` presets | `-51 66.9 -29 76.2` | 5,713 |
| All Greenland | `-73 59 -11 84` | 32,060 |

Start small. Regions are additive — a second region can be built and imported later without rebuilding
or invalidating the first.

## 1. On CryoCloud

The code must be on a branch the instance can clone, and `data/` is gitignored — **push first**.

The repo is public, so the clone needs no credentials on the instance. `-b` is required until this
branch merges: `main` has none of this workflow.

```bash
git clone -b cryocloud-index-workflow https://github.com/eigenbeam/aicesat ~/aicesat
cd ~/aicesat
bash scripts/cryocloud_build_index.sh                     # SW Greenland default
bash scripts/cryocloud_build_index.sh -51 66.9 -29 76.2   # or any W S E N [res] [workers]
```

Confirm you have the right state — `git log --oneline -2` should show the index and dismiss commits,
and `scripts/cryocloud_build_index.sh` should exist.

The script installs `uv` to `~/.local/bin` if needed (no sudo), runs `uv sync` (which fetches its own
Python 3.13 — do **not** reuse the notebook kernel), exports `AWS_REGION=us-west-2`, runs the in-region
pre-flight, builds, and packages a tar.

> **The single most likely failure is `AWS_REGION` being unset**, and it fails *silently* into the slow
> HTTPS path rather than erroring. `access.in_region()` (`src/aicesat/access.py:72-78`) reads only
> `AWS_REGION` / `AWS_DEFAULT_REGION` / `AICESAT_S3_DIRECT` — it never queries IMDS. The pre-flight
> (`deploy/verify_region.py`) exists to catch this: it asserts `in_region()` before any building starts.

Auth needs nothing interactive. CryoCloud normally already has a `~/.netrc`, which `auth.login()` picks
up via `strategy="all"`. Otherwise export `EARTHDATA_TOKEN` or drop a token at `~/.edl/token.prod`.

The build is resumable — re-run the same command and finished granules are skipped, because each
granule writes its own version-tagged Parquet.

## 2. Transfer

The build ends by printing a tar path, size and sha256. Find it in the JupyterLab file browser (left
panel), right-click → Download.

Plain `.tar`, not `.tar.gz`: Parquet is already snappy-compressed, so gzip costs CPU and saves little.

## 3. On the laptop

```bash
uv run python scripts/import_index.py ~/Downloads/atl06-index-res5-*.tar --dry-run
uv run python scripts/import_index.py ~/Downloads/atl06-index-res5-*.tar
```

Then verify:

```bash
curl 'http://127.0.0.1:8765/api/index_status?collection=ATL06'      # granules > 0
curl 'http://127.0.0.1:8765/api/coverage?bbox=\[-50.3,68.9,-49.2,69.3\]'
```

Build a scene over the region and confirm its `series` now contains `ATL06`.

## How coverage is decided (and how copying used to break it)

`_build.json` in the index directory records which regions were built. A query is covered when it fits
inside **one** of them.

It is deliberately *not* the union of the regions. A union would claim the gap between two disjoint
regions, and queries there would return empty data instead of an honest "not indexed" error — a silent
wrong answer rather than a loud one.

Before `src/aicesat/index_manifest.py` existed, the manifest held a single `bbox` and four call sites
each re-implemented the containment test. Copying a region-B index onto a laptop holding region A
overwrote A's manifest: every A Parquet was still on disk and would answer perfectly, but A's queries
started failing. `scripts/import_index.py` merges the region lists instead.

Two related guards:

- The manifest is written *before* the build starts, so the UI can show progress. A build therefore only
  sets `complete: true` when it finishes with zero failures. `export_index.py` refuses to package an
  incomplete index (`--force` overrides), so a half-built index cannot quietly claim full coverage of
  its region on another machine.
- `import_index.py` refuses a tar whose `ATL06_INDEX_VERSION` or `res` does not match this checkout.
  Stale-schema Parquet files are not auto-deleted for ATL06, and DuckDB scans the whole directory with
  `read_parquet('dir/*.parquet')`, so mixing versions can break schema unification.

## Optional: squeeze the build further

Branch `origin/integration/scene-speed` carries `4b6a899` (pluggable in-region S3 fetch mechanisms plus
`scripts/bench_fetch_mechanisms.py`, which is designed to be run in-region to pick the fastest) and
`698d107` (parallel multi-granule fan-out). Neither is on `main`; cherry-pick if build time still
disappoints.
