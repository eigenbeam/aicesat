"""The scene's browser-side geometry against the server's: runs tests/test_scene_geo.js under node with pyproj reference
cases for every kind of frame scene.local_frame produces."""
import json
import shutil
import subprocess

import pytest

from aicesat import scene

FRAMES = {"polar north (EPSG:3413)": (-49.90, 69.10, -49.30, 69.25),
          "polar south (EPSG:3031)": (160.0, -77.8, 162.0, -77.4),
          "azimuthal equidistant": (85.44, 28.21, 85.62, 28.37)}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_geometry_matches_the_server(tmp_path):
    cases = []
    for label, bbox in FRAMES.items():
        fr = scene.local_frame(bbox)
        w, s, e, n = bbox
        # well past the box, where a local approximation drifts most
        pts = [(w + (e - w) * u, s + (n - s) * v) for u in (-0.5, 0, 0.5, 1, 1.5) for v in (-0.5, 0, 0.5, 1, 1.5)]
        x, y = scene.to_local(fr, [p[0] for p in pts], [p[1] for p in pts])
        cases.append({"label": label, "frame": fr, "pts": pts, "x": [float(v) for v in x], "y": [float(v) for v in y]})
    p = tmp_path / "cases.json"
    p.write_text(json.dumps(cases))
    r = subprocess.run(["node", "tests/test_scene_geo.js", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
