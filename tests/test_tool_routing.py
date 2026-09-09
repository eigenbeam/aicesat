"""Run the tool-result routing checks under pytest so they travel with the suite.

The logic is client-side (src/aicesat/ui/app.js); the assertions live in tests/test_tool_routing.js.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).with_suffix(".js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_tool_result_routes_the_inline_app():
    r = subprocess.run(["node", str(JS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"tool routing checks failed:\n{r.stdout}\n{r.stderr}"
