"""The data reset must not delete more than it says, and must recover the index build parameters first.

The deletion itself is not the risk — lake, cache and raw are all re-fetchable. The risk is that the bbox/res needed
to REBUILD the indexes lives inside the directory being deleted, so losing it turns a wipe into a guess.
"""
import json
import subprocess
import sys

import pytest

from aicesat import cache


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    for sub in ("lake", "index", "cache", "scenes", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "lake" / "a.parquet").write_bytes(b"x" * 100)
    (root / "scenes" / "abc.json").write_text("{}")
    d = root / "index" / "atl06" / "res5"; d.mkdir(parents=True)
    (d / "_build.json").write_text(json.dumps({"bbox": [-52, 62, -44, 70], "res": 5, "target": 2335}))
    (d / "G1.parquet").write_bytes(b"y" * 10)
    monkeypatch.setenv("AICESAT_DATA_DIR", str(root))
    return root


def _run(root, *args):
    out = subprocess.run([sys.executable, "scripts/reset_data.py", *args], capture_output=True, text=True,
                         env={**__import__("os").environ, "AICESAT_DATA_DIR": str(root)})
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_dry_run_deletes_nothing_and_recovers_the_build_parameters(data):
    out = _run(data)
    assert "Nothing deleted" in out
    assert "bbox=[-52, 62, -44, 70] res=5" in out, out
    assert "build_atl06_index.py -52 62 -44 70 5" in out, out       # ready to paste
    assert (data / "lake" / "a.parquet").exists()
    assert (data / "scenes" / "abc.json").exists()


def test_scene_loss_is_called_out_explicitly(data):
    assert "saved scene doc(s)" in _run(data)
    assert "saved scene doc(s)" not in _run(data, "--keep", "scenes")


def test_keep_preserves_exactly_what_it_names(data):
    _run(data, "--keep", "scenes,cache", "--yes")
    assert (data / "scenes" / "abc.json").exists(), "scenes were deleted despite --keep"
    assert (data / "cache").exists()
    assert not (data / "lake").exists() and not (data / "index").exists()


def test_an_unknown_keep_target_is_refused(data):
    out = subprocess.run([sys.executable, "scripts/reset_data.py", "--keep", "sceens", "--yes"],
                         capture_output=True, text=True,
                         env={**__import__("os").environ, "AICESAT_DATA_DIR": str(data)})
    assert out.returncode != 0 and "unknown --keep" in (out.stderr + out.stdout)
    assert (data / "lake").exists(), "a typo in --keep must not still wipe the data"
