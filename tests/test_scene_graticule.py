"""Run the graticule/hover JS unit tests under node (see tests/test_scene_graticule.js)."""
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_scene_graticule_js():
    r = subprocess.run(["node", "tests/test_scene_graticule.js"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
