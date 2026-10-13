"""A declared-collapsed panel starts collapsed unless the reader chose otherwise (tests/test_panel_default.js)."""
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_panel_can_start_collapsed_and_a_remembered_choice_wins():
    r = subprocess.run(["node", "tests/test_panel_default.js"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
