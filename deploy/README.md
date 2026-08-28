# Deploying AIcesat to AWS us-west-2

Two access paths on **one** t3.large in us-west-2, cleanly separated by env vars — no code fork:

- **You (owner) → Claude Desktop** — MCP over an **SSH stdio** bridge. Ungated (your SSH key is the auth). Runs on demand.
- **Beta users → public web app** — the deck.gl globe app in a browser, behind **Caddy** (auto-TLS) and a **shared access code**. Always-on via systemd. Binds `127.0.0.1`; Caddy is the only public listener.

Why us-west-2 specifically: NASA NSIDC **S3-direct** access is gated to AWS us-west-2. That's the whole performance win (no presigns, no CloudFront hop, free egress) — and it only works from real AWS in that region.

---

## 0. Prereqs (once)

- The code must be on a branch the box can clone. Push it first (ask Claude, or `git push -u origin main`).
- Have your `~/.edl/token.prod` Earthdata token handy to `scp` up.

## 1. Launch the instance

Console or CLI — **us-west-2**, Ubuntu 24.04, **t3.large**, 20 GB gp3, your SSH key.

```bash
aws ec2 run-instances --region us-west-2 \
  --image-id resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --instance-type t3.large --key-name YOUR_KEY \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=aicesat}]'
```

Note the **public IP**.

## 2. Security group

- **22/tcp** from **your IP only** (SSH).
- **80 + 443/tcp** from **0.0.0.0/0** (public web app + Let's Encrypt ACME).

## 3. Hostname

Easiest (free, instant TLS): use **sslip.io** — for IP `54.201.9.8` the hostname is `54-201-9-8.sslip.io`, no DNS setup. A real domain you point at the IP is more robust for a long-lived beta.

## 4. Bootstrap the box

```bash
scp deploy/bootstrap.sh ubuntu@HOST:/tmp/          # or let it git-clone (see top of the script)
ssh ubuntu@HOST 'REPO=https://github.com/eigenbeam/aicesat.git BRANCH=main bash /tmp/bootstrap.sh'
```

Installs Caddy + uv, clones to `/opt/aicesat`, `uv sync`, builds the UI, installs the systemd unit. It prints the remaining manual bits:

## 5. Token, env, Caddy

```bash
scp ~/.edl/token.prod ubuntu@HOST:/opt/aicesat/.edl/token.prod
ssh ubuntu@HOST
  nano /opt/aicesat/aicesat.env         # set AICESAT_PUBLIC_URL=https://<host>  and  AICESAT_ACCESS_CODE=<code>
  sed -i 's/REPLACE-ME.sslip.io/<host>/' /opt/aicesat/deploy/Caddyfile
  sudo cp /opt/aicesat/deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl restart caddy
```

## 6. Get the index onto the box

Either `scp` the ~75 MB index up, **or** rebuild in-region (fast there):

```bash
# copy from your laptop:
rsync -av data/index/ ubuntu@HOST:/opt/aicesat/data/index/
# or rebuild on the box (example: Jakobshavn):
ssh ubuntu@HOST 'cd /opt/aicesat && uv run python scripts/build_glas_index.py -50.3 68.9 -49.2 69.3 5 8'
```

## 7. Start + verify

```bash
ssh ubuntu@HOST
  sudo systemctl start aicesat-web && journalctl -u aicesat-web -f    # public app
  cd /opt/aicesat && uv run python deploy/verify_region.py            # <-- first real S3-direct test
```

`verify_region.py` green (presigns = 0) means the whole point of us-west-2 is delivering. Then open `https://<host>/` in a browser → gate page → enter the code → the globe app.

## 8. Your Claude Desktop (owner path)

Merge `deploy/claude-desktop.json` into your `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`), replacing `HOST`. Restart Claude Desktop. It SSHes in and runs the MCP server; the `-L` forward makes widget links open in your browser. This path is **ungated** and independent of the public app.

---

## Operations

- **Save money:** `aws ec2 stop-instances` between sessions — you pay compute only while running (~$0.083/hr for t3.large; EBS persists). `start-instances` to resume; the public IP changes unless you attach an Elastic IP (and the sslip.io host changes with it — an Elastic IP or real domain avoids that).
- **Rotate the beta code:** edit `AICESAT_ACCESS_CODE` in `aicesat.env`, `sudo systemctl restart aicesat-web`. Existing cookies stop working.
- **Update code:** `ssh ubuntu@HOST 'cd /opt/aicesat && git pull && uv run python scripts/build_ui.py && sudo systemctl restart aicesat-web'`.
- **Logs:** `journalctl -u aicesat-web -f` (app), `journalctl -u caddy -f` (TLS/proxy).

## Security notes

- Beta users' queries fetch NASA data under **your** EDL token and run scene builds on **your** box. Public data, but a shared code leaks easily — treat the box as disposable and don't put anything sensitive on it.
- The access code is a single shared secret (HMAC cookie, HttpOnly/Secure/SameSite). It gates every page and `/api`. It is **not** per-user and has no rate limit — if the beta grows, add per-code tokens or a limiter before widening it.
- Only Caddy is public. The app binds `127.0.0.1`; SSH is restricted to your IP. Keep it that way.
