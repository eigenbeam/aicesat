#!/usr/bin/env bash
# Build the ATL06 sub-granule index on CryoCloud (AWS us-west-2 JupyterHub) and package it for download.
#
#   git clone <repo> ~/aicesat && cd ~/aicesat
#   bash scripts/cryocloud_build_index.sh                    # SW Greenland, the default region
#   bash scripts/cryocloud_build_index.sh -51 66.9 -29 76.2  # or any W S E N
#
# Why here and not on a laptop: NSIDC S3-direct access is gated to us-west-2. Off-region the build is
# latency-bound on NASA byte-range round trips (~0.78 gran/s measured, commit a581613) and more cores
# buy nothing; in-region the same reads go straight to S3 with STS credentials.
#
# No sudo, no apt, no systemd — this is a JupyterHub home directory, not a box you own.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Default region: SW Greenland — Jakobshavn + K-transect, 2,333 granules. Overridable as W S E N [res] [workers].
BBOX=("${1:--52}" "${2:-62}" "${3:--44}" "${4:-70}")
RES="${5:-5}"
WORKERS="${6:-$( (nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 8) )}"

# 1. uv — installs to ~/.local/bin, no root needed.
if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || { echo "!! uv still not on PATH" >&2; exit 1; }

# 2. Dependencies. uv fetches its own Python 3.13 (pyproject pins >=3.13,<3.14) — do NOT reuse the
#    notebook kernel, which is usually older.
echo "==> uv sync (fetches Python 3.13 + native wheels; a few minutes on a cold home dir)"
uv sync

# 3. THE critical env var. access.in_region() reads only AWS_REGION/AWS_DEFAULT_REGION (or
#    AICESAT_S3_DIRECT) and never queries IMDS — so on a JupyterHub kernel where it is unset, the build
#    silently falls back to slow HTTPS and you get laptop performance on a cloud box.
export AWS_REGION="${AWS_REGION:-us-west-2}"
echo "==> AWS_REGION=$AWS_REGION"

# 4. Pre-flight. Asserts in_region() and fetches STS credentials. On a fresh box it reports
#    "no index built here yet — skip" for each collection; that is expected before the build.
echo "==> pre-flight: in-region S3 check"
uv run python deploy/verify_region.py

# 5. Auth note: nothing interactive. CryoCloud normally already has ~/.netrc, which auth.login() uses
#    via strategy="all". Otherwise export EARTHDATA_TOKEN or drop a token at ~/.edl/token.prod.

echo "==> building ATL06 index over ${BBOX[*]} at res $RES with $WORKERS workers"
time uv run python scripts/build_atl06_index.py "${BBOX[@]}" "$RES" "$WORKERS"

# 6. Package for the browser download.
echo "==> packaging"
uv run python scripts/export_index.py --res "$RES"

cat <<'EOF'

Done. Next:
  1. Find the .tar in the JupyterLab file browser (left panel) and download it.
  2. On the laptop, from the repo root:
       uv run python scripts/import_index.py ~/Downloads/<name>.tar --dry-run
       uv run python scripts/import_index.py ~/Downloads/<name>.tar
EOF
