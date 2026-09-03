"""Run the error-banner classification checks under pytest so they travel with the suite.

The logic is client-side (src/aicesat/ui/app.js); the assertions live in tests/test_error_banner.js.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).with_suffix(".js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_error_banner_names_the_right_subsystem():
    r = subprocess.run(["node", str(JS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"error banner checks failed:\n{r.stdout}\n{r.stderr}"
