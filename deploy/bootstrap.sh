#!/usr/bin/env bash
# One-time (and re-runnable) setup for the AIcesat box: Ubuntu 22.04/24.04 EC2 in us-west-2.
# Run AS the `ubuntu` user on the instance:  bash bootstrap.sh
# Assumes: this repo's `deploy/` was copied up, OR run the git-clone block below first. Edit REPO/BRANCH if needed.
set -euo pipefail

REPO="${REPO:-https://github.com/eigenbeam/aicesat.git}"   # public clone URL, or use git@ with a deploy key
BRANCH="${BRANCH:-main}"
APP=/opt/aicesat

echo "== packages (git, caddy) =="
sudo apt-get update -y
sudo apt-get install -y git curl debian-keyring debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update -y && sudo apt-get install -y caddy
fi

echo "== uv =="
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "== code at $APP ($BRANCH) =="
sudo mkdir -p "$APP" && sudo chown -R "$USER":"$USER" "$APP"
if [ -d "$APP/.git" ]; then git -C "$APP" fetch origin "$BRANCH" && git -C "$APP" checkout "$BRANCH" && git -C "$APP" pull --ff-only origin "$BRANCH"
else git clone --branch "$BRANCH" "$REPO" "$APP"; fi

echo "== deps (uv sync — installs h5py/rasterio/h3ronpy/sliderule; a few minutes) =="
cd "$APP" && uv sync

echo "== data dir + Earthdata token =="
mkdir -p "$APP/data/index" "$APP/.edl"
[ -f "$APP/.edl/token.prod" ] || echo "  !! scp your ~/.edl/token.prod to $APP/.edl/token.prod  (or set EARTHDATA_TOKEN in aicesat.env)"

echo "== env file =="
[ -f "$APP/aicesat.env" ] || { cp "$APP/deploy/aicesat.env.example" "$APP/aicesat.env"; echo "  !! edit $APP/aicesat.env (set AICESAT_PUBLIC_URL + AICESAT_ACCESS_CODE)"; }

echo "== build the single-file UI =="
uv run python scripts/build_ui.py

echo "== systemd service (public web app) =="
sudo cp "$APP/deploy/aicesat-web.service" /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable aicesat-web

echo "== Caddy =="
echo "  !! edit $APP/deploy/Caddyfile — set your hostname — then:"
echo "     sudo cp $APP/deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl restart caddy"

echo
echo "DONE. Next:"
echo "  1) edit $APP/aicesat.env         (public URL + access code)"
echo "  2) put the token at $APP/.edl/token.prod"
echo "  3) set the hostname in the Caddyfile and install it (line above)"
echo "  4) sudo systemctl start aicesat-web && journalctl -u aicesat-web -f"
echo "  5) verify S3-direct in-region:   cd $APP && uv run python deploy/verify_region.py"
