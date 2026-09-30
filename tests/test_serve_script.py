"""scripts/serve.py under multiprocessing's spawn start method (the macOS default): every ProcessPoolExecutor worker -
the ATL03 planner's, the index builders' - imports the main script as __mp_main__. Unguarded, each worker started a
second web server and then slept forever, so the ATL03 leg of any build started from the web UI never returned."""
import os
import socket
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "serve.py"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_spawned_worker_importing_serve_returns_instead_of_serving(tmp_path):
    # What multiprocessing.spawn does to the parent's main script in each worker (spawn._fixup_main_from_path).
    code = f"import runpy; runpy.run_path({str(SCRIPT)!r}, run_name='__mp_main__'); print('returned')"
    env = {**os.environ, "AICESAT_PORT": str(_free_port()), "AICESAT_DATA_DIR": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env)
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("importing scripts/serve.py as __mp_main__ never returned: it started a server")
    assert proc.returncode == 0, err
    assert "returned" in out
    assert "serving on" not in err
