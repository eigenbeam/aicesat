"""The demo profile is the default: with AICESAT_PROFILE unset the server registers only the ladder's three model
tools and injects the demo look; AICESAT_PROFILE=full brings back every tool and the full UI."""
import json
import os
import subprocess
import sys

PROBE = r"""
import asyncio, json
from aicesat import server
tools = asyncio.run(server.mcp.list_tools())
model = sorted(t.name for t in tools if ((t.meta or {}).get("ui") or {}).get("visibility") != ["app"])
print(json.dumps({"profile": server.PROFILE, "model": model,
                  "injects": 'window.AICESAT_PROFILE="demo"' in server._with_profile("<head></head>")}))
"""


def _probe(profile):
    env = {k: v for k, v in os.environ.items() if k != "AICESAT_PROFILE"}
    if profile is not None:
        env["AICESAT_PROFILE"] = profile
    r = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_unset_means_demo():
    out = _probe(None)
    assert out["profile"] == "demo"
    assert out["model"] == ["elevation_change", "show_timeseries", "survey_coverage"]
    assert out["injects"]


def test_full_is_still_one_variable_away():
    out = _probe("full")
    assert out["profile"] == "full"
    assert {"elevation_change", "show_timeseries", "survey_coverage", "find_timeseries_candidates"} <= set(out["model"])
    assert not out["injects"]
