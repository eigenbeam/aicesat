"""The scene camera frames the scene's area, not the first data to arrive (tests/test_scene_fit.js, under node)."""
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_camera_frames_the_area_not_the_first_data():
    r = subprocess.run(["node", "tests/test_scene_fit.js"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
