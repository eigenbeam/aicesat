"""Earthdata Login: load a bearer token from ~/.edl/token.prod into EARTHDATA_TOKEN and log in once.

The token file format is not standardised; we accept, in order:
  * JSON with a "token" / "access_token" / "EARTHDATA_TOKEN" key
  * a shell-style line   EARTHDATA_TOKEN=...  (optionally prefixed with `export`)
  * a key=value line     token=...
  * a bare token on the first non-empty line
Everything is logged to stderr only (stdio MCP transport must never see stdout).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

EDL_FILE = Path(os.environ.get("AICESAT_EDL_FILE", "~/.edl/token.prod")).expanduser()
_TOKEN_KEYS = ("token", "access_token", "EARTHDATA_TOKEN", "edl_token")
_auth = None


def read_edl_token(path: Path = EDL_FILE) -> str | None:
    if path.is_dir():
        path = path / "token.prod"
    if not path.exists():
        return None
    text = path.read_text().strip()
    if not text:
        return None
    # JSON
    if text[0] in "{[":
        try:
            obj = json.loads(text)
            if isinstance(obj, list) and obj:
                obj = obj[0]
            if isinstance(obj, dict):
                for k in _TOKEN_KEYS:
                    if obj.get(k):
                        return str(obj[k]).strip()
        except json.JSONDecodeError:
            pass
    # key=value lines
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_]+)\s*[=:]\s*['\"]?([^'\"\s]+)['\"]?\s*$", line)
        if m and m.group(1).lower() in {k.lower() for k in _TOKEN_KEYS}:
            return m.group(2)
    # bare token
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" not in line and " " not in line:
            return line
    return None


def login():
    """Authenticate with Earthdata once per process; returns the earthaccess Auth object."""
    global _auth
    if _auth is not None:
        return _auth
    import earthaccess

    if not os.environ.get("EARTHDATA_TOKEN"):
        tok = read_edl_token()
        if tok:
            os.environ["EARTHDATA_TOKEN"] = tok
            log.info("loaded EARTHDATA_TOKEN from %s", EDL_FILE)
        else:
            log.warning("no token found in %s; falling back to earthaccess strategy='all'", EDL_FILE)
    strategy = "environment" if os.environ.get("EARTHDATA_TOKEN") else "all"
    _auth = earthaccess.login(strategy=strategy)
    if not getattr(_auth, "authenticated", False):
        raise RuntimeError("Earthdata login failed (strategy=%s)" % strategy)
    log.info("Earthdata login ok (strategy=%s)", strategy)
    return _auth
