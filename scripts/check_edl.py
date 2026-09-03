#!/usr/bin/env python
"""Why did Earthdata auth fail? Answers it without ever printing the token.

    uv run python scripts/check_edl.py

Run it AS THE SERVICE USER and with the service's environment, or it will check a different token than the app uses:

    sudo -u ubuntu env $(grep -v '^#' /opt/aicesat/aicesat.env | xargs) \
        /home/ubuntu/.local/bin/uv run python scripts/check_edl.py

Four things can go wrong and they need different fixes, so the script names which one:
  1. no token found at all              -> nothing to load; scp one up, or set EARTHDATA_TOKEN
  2. token found but EXPIRED            -> mint a new one at urs.earthdata.nasa.gov (they last at most 60 days)
  3. token valid but login() fails      -> earthaccess/network problem, not the token
  4. login ok but S3 credentials fail   -> the DAAC rejected it (wrong account, or no EULA accepted for that data)

Only metadata is printed: file path, byte length, expiry date, HTTP status. Never the token, never a fragment of it.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone


def jwt_expiry(tok: str) -> tuple[datetime | None, datetime | None]:
    """(issued_at, expires_at) from a JWT's payload. The payload is not the secret — the signature is — but nothing
    from it is printed except the two timestamps. Returns (None, None) for an opaque (non-JWT) token."""
    parts = tok.split(".")
    if len(parts) != 3:
        return None, None
    try:
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
    except Exception:
        return None, None
    at = lambda k: datetime.fromtimestamp(claims[k], timezone.utc) if isinstance(claims.get(k), (int, float)) else None
    return at("iat"), at("exp")


def main() -> int:
    from aicesat import auth

    print("== token source ==")
    env_tok = os.environ.get("EARTHDATA_TOKEN")
    if env_tok:
        print(f"  EARTHDATA_TOKEN is set in the environment ({len(env_tok)} chars) — this WINS over the file")
        tok, where = env_tok, "environment"
    else:
        print(f"  EARTHDATA_TOKEN not set; reading {auth.EDL_FILE}")
        print(f"  AICESAT_EDL_FILE={os.environ.get('AICESAT_EDL_FILE', '(unset, so the ~/.edl default applies)')}")
        print(f"  HOME={os.environ.get('HOME')}  (the default path is relative to this)")
        if not auth.EDL_FILE.exists():
            print(f"\n  VERDICT: no token file at {auth.EDL_FILE} and no EARTHDATA_TOKEN.")
            print("  Fix: scp your token there, or set EARTHDATA_TOKEN in /opt/aicesat/aicesat.env.")
            return 1
        st = auth.EDL_FILE.stat()
        print(f"  file exists, {st.st_size} bytes, modified {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}")
        tok, where = auth.read_edl_token(), str(auth.EDL_FILE)
        if not tok:
            print(f"\n  VERDICT: {auth.EDL_FILE} exists but no token could be parsed out of it.")
            print("  Accepted forms: JSON with a token/access_token key, EARTHDATA_TOKEN=..., token=..., or a bare token.")
            return 1
        print(f"  parsed a token from the file ({len(tok)} chars)")

    print("\n== expiry ==")
    iat, exp = jwt_expiry(tok)
    if exp is None:
        print("  not a JWT (or unparseable claims) — cannot read an expiry; relying on the live checks below")
    else:
        now = datetime.now(timezone.utc)
        left = exp - now
        print(f"  issued  {iat:%Y-%m-%d %H:%M UTC}" if iat else "  issued  (unknown)")
        print(f"  expires {exp:%Y-%m-%d %H:%M UTC}")
        if left.total_seconds() <= 0:
            print(f"\n  VERDICT: THE TOKEN EXPIRED {-left.days} day(s) ago. This is the usual cause.")
            print("  Fix: mint a new token at https://urs.earthdata.nasa.gov/profile -> Generate Token,")
            print(f"       then replace {where} and restart: sudo systemctl restart aicesat-web")
            return 1
        print(f"  {left.days} day(s), {left.seconds // 3600} hour(s) remaining")
        if left.days < 7:
            print("  NOTE: expires soon — worth rotating before it bites mid-demo.")

    print("\n== earthaccess login ==")
    os.environ["EARTHDATA_TOKEN"] = tok
    try:
        auth.login()
        print("  login OK")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}")
        print("\n  VERDICT: the token was found and is unexpired, but Earthdata rejected the login.")
        print("  Check network egress from the box, then re-mint the token.")
        return 1

    print("\n== S3 credentials (what the fetch path actually needs) ==")
    try:
        from aicesat import access

        c = access.s3_credentials(refresh=True)
        keys = sorted(k for k in c if "secret" not in k.lower() and "key" not in k.lower())
        print(f"  NSIDC S3 credentials OK (fields: {', '.join(keys) or 'opaque'})")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {str(e)[:160]}")
        print("\n  VERDICT: login works but the DAAC would not issue S3 credentials.")
        print("  Usually an un-accepted EULA on the account, or a token minted for a different Earthdata account.")
        return 1

    print("\nAll checks passed — auth is not the problem. If a build still fails, the selection may genuinely have")
    print("no data: 'no collection returned data over this area' is also what an empty area returns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
