"""Run the JS adapter chunk-reassembly checks under pytest so they travel with the suite.

The logic under test is client-side (src/aicesat/ui/adapter.js), so the assertions live in
tests/test_adapter_chunking.js and are executed here with node. Skipped when node is unavailable.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).with_suffix(".js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_adapter_chunk_reassembly():
    r = subprocess.run(["node", str(JS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"adapter chunking checks failed:\n{r.stdout}\n{r.stderr}"
