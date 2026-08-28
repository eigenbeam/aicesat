# aicesat

Cross-mission altimetry: compare ICESat-1/GLAS, IceBridge ATM and ICESat-2 elevations over an area.
Python 3.13 + `uv`, no Node. An MCP server (`aicesat.server:main`) that also serves a self-contained
widget UI over plain HTTP.

## Running

```bash
uv sync
uv run pytest                  # offline unit tests
uv run scripts/serve.py        # widget only, http://127.0.0.1:8765/  (use THIS to view the UI)
```

Do **not** background `uv run aicesat-server` to view the UI — it is a stdio MCP server and exits at
EOF, taking the HTTP thread with it. `scripts/serve.py` is the widget-only variant. The `run-aicesat`
skill in `.claude/skills/` wraps start/stop.

Data (`data/`) is gitignored. `AICESAT_DATA_DIR` relocates the whole data root.

## Earthdata auth

Token resolution order (`src/aicesat/auth.py`): `EARTHDATA_TOKEN` env → `~/.edl/token.prod` (override
with `AICESAT_EDL_FILE`) → `~/.netrc`. Nothing is interactive. Tokens expire ~60 days; regenerate at
https://urs.earthdata.nasa.gov → User Profile → Generate Token.

Browsing an existing lake needs no token. Fetching new NASA data does.

---

# ⛅ Resuming on CryoCloud: build the ATL06 index

**If you are on a CryoCloud / us-west-2 instance, this is almost certainly the task.**
Full runbook: `docs/cryocloud-index-build.md`.

## Why this exists

`atl06.extract()` is **index-only** — no whole-granule fallback (`src/aicesat/atl06.py:91-94`). With no
index, scene builds silently drop ATL06 and return only GLAS + IceBridge. Building the index is
latency-bound on NASA byte-range round trips (~0.78 gran/s measured, commit `a581613`), not CPU-bound,
so more laptop cores buy nothing. NSIDC S3-direct access is gated to **AWS us-west-2** — which is why
the build belongs here and not on the laptop.

The index is portable: every row stores both the HTTPS `url` and the `s3://` `s3url`
(`src/aicesat/index_atl06.py:130`), and `access.access_url()` picks between them at query time. So an
index built in-region works unchanged on a laptop over HTTPS.

## The whole workflow

```bash
# 1. On CryoCloud — clone, build, package (installs uv to ~/.local/bin; no sudo needed)
git clone https://github.com/eigenbeam/aicesat ~/aicesat && cd ~/aicesat
bash scripts/cryocloud_build_index.sh                     # SW Greenland default: -52 62 -44 70
bash scripts/cryocloud_build_index.sh -51 66.9 -29 76.2   # or any W S E N [res] [workers]

# 2. Download the printed .tar via the JupyterLab file browser (left panel, right-click → Download)

# 3. On the laptop
uv run python scripts/import_index.py ~/Downloads/atl06-index-res5-*.tar --dry-run
uv run python scripts/import_index.py ~/Downloads/atl06-index-res5-*.tar
```

Verify after import: `curl 'http://127.0.0.1:8765/api/index_status?collection=ATL06'` shows
`granules` > 0, then build a scene and confirm its `series` contains `ATL06`.

## Gotchas that actually bite

- **`AWS_REGION` unset is the top failure, and it fails *silently*** into the slow HTTPS path.
  `access.in_region()` (`src/aicesat/access.py:72-78`) reads only `AWS_REGION` / `AWS_DEFAULT_REGION` /
  `AICESAT_S3_DIRECT` — it never queries IMDS. `deploy/verify_region.py` is the pre-flight; the build
  script runs it and aborts if `in_region()` is false. `presigns 0` in its output means S3-direct is live.
- **Let `uv` fetch its own Python 3.13** (`pyproject.toml` pins `>=3.13,<3.14`). Do not reuse the
  JupyterHub notebook kernel — it is usually older.
- **Push before cloning.** `data/` is gitignored and the instance clones from GitHub, so any local work
  must be pushed first.
- **Builds are resumable.** Re-run the same command; finished granules are skipped via a version-tagged
  per-granule Parquet. A build with any failures stays `complete: false` and `export_index.py` refuses
  to package it (`--force` overrides).

## Region sizes (CMR, ATL06 v007, full record)

| Region | bbox | Granules |
|---|---|---|
| SW Greenland (default) — Jakobshavn + K-transect | `-52 62 -44 70` | 2,333 |
| All six `regions.py` presets | `-51 66.9 -29 76.2` | 5,713 |
| All Greenland | `-73 59 -11 84` | 32,060 |

Start small. **Regions are additive** — build and import a second region later without rebuilding or
invalidating the first.

## Index coverage semantics — do not "simplify" this

`_build.json` records a `regions` list; a query is covered when it fits inside **one** region, never
their union. A union would claim the gap between two disjoint regions and return empty data instead of
an honest "not indexed" error — a silent wrong answer instead of a loud one.

`src/aicesat/index_manifest.py` is the single implementation; `atl06.py`, `glas.py`, `icessn.py` and
`coverage.py` all delegate to it. Before it existed, each had its own copy and importing region B
clobbered region A's manifest — A's Parquet files stayed on disk and would have answered fine, but A's
queries began failing. `scripts/import_index.py` merges region lists rather than overwriting.
