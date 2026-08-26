"""Build the single-file UI (src/aicesat/widget/dist/aicesat.html) from src/aicesat/ui/* and the vendored libraries.
Pure Python, no Node. Run after editing anything under src/aicesat/ui/. The server also rebuilds on start if sources
are newer than the dist file.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI, VENDOR, DIST = ROOT / "ui", ROOT / "widget" / "vendor", ROOT / "widget" / "dist" / "aicesat.html"
SOURCES = ["util.js", "adapter.js", "map.js", "explore.js", "lake.js", "scene.js", "app.js"]  # order matters


def build() -> Path:
    shell = (UI / "shell.html").read_text()
    css = (UI / "shell.css").read_text()
    js = "\n;\n".join((UI / f).read_text() for f in SOURCES if (UI / f).exists())
    deck = next(VENDOR.glob("deck.gl-*.min.js")).read_text()
    h3js = next(VENDOR.glob("h3-js-*.umd.js")).read_text()
    land = (VENDOR / "ne_land_50m.js").read_text()
    bridge = bridge_as_classic_script(next(VENDOR.glob("ext-apps-*.app-with-deps.js")).read_text())
    # </script> inside inlined code would terminate the tag
    safe = lambda s: s.replace("</script", "<\\/script")
    html = (shell.replace("{{CSS}}", css).replace("{{VENDOR_H3}}", safe(h3js)).replace("{{VENDOR_LAND}}", safe(land)).replace("{{VENDOR_DECK}}", safe(deck))
            .replace("{{VENDOR_BRIDGE}}", safe(bridge)).replace("{{APP_JS}}", safe(js)))
    DIST.parent.mkdir(parents=True, exist_ok=True)
    DIST.write_text(html)
    return DIST


def bridge_as_classic_script(esm: str) -> str:
    """The ext-apps bundle is a self-contained ES module ending in one `export{a as b,...}` statement. Rewrite that
    statement into a global so the bundle can be inlined as a classic <script> under the app sandbox's CSP."""
    m = list(re.finditer(r"export\{([^}]*)\};?\s*$", esm.strip()))
    if not m:
        raise ValueError("ext-apps bundle: no trailing export statement found")
    m = m[-1]
    pairs = []
    for item in m.group(1).split(","):
        item = item.strip()
        if not item:
            continue
        local, _, exported = item.partition(" as ")
        pairs.append(f"{(exported or local).strip()}:{local.strip()}")
    body = esm.strip()[: m.start()]
    return "(function(){\n" + body + "\nwindow.__extApps={" + ",".join(pairs) + "};\n})();"


def needs_build() -> bool:
    if not DIST.exists():
        return True
    newest = max([p.stat().st_mtime for p in UI.glob("*")] + [p.stat().st_mtime for p in VENDOR.glob("*")] + [Path(__file__).stat().st_mtime])
    return newest > DIST.stat().st_mtime


if __name__ == "__main__":
    out = build()
    print(f"built {out} ({out.stat().st_size / 1e6:.2f} MB)")
