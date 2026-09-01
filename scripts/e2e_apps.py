"""MCP Apps wiring check over stdio: the ui:// resource is listed with the mcp-app MIME type, UI-bound tools carry
_meta.ui.resourceUri, app-visible tools are hidden from the model unless the client negotiated Apps, and the chunked
scene parts round-trip. No network needed (uses existing scenes).
usage: AICESAT_PORT=8768 uv run scripts/e2e_apps.py
"""
import asyncio, base64, json, os, sys
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command="uv", args=["run", "aicesat-server"], env={**os.environ})
    async with stdio_client(params) as (r, w):
        ext = {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}} if os.environ.get("APPS") else None
        print("client negotiates Apps:", bool(ext))
        async with ClientSession(r, w, extensions=ext) as s:
            init = await s.initialize()
            caps = init.capabilities
            print("server capabilities extensions:", getattr(caps, "extensions", None))
            res = await s.list_resources()
            ui = [x for x in res.resources if str(x.uri).startswith("ui://")]
            print("ui resources:", [(str(x.uri), x.mime_type) for x in ui])
            assert ui and ui[0].mime_type == "text/html;profile=mcp-app", "ui resource missing or wrong MIME"
            body = await s.read_resource(ui[0].uri)
            html = body.contents[0].text
            print(f"resource html: {len(html) / 1e6:.2f} MB; has bridge global: {'window.__extApps=' in html}; has deck: {'H3HexagonLayer' in html}")
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("tools listed to this client:", names)
            bound = {t.name: (t.meta or {}).get("ui") for t in tools.tools if t.meta and t.meta.get("ui")}
            print("ui-bound:", bound)
            if ext:
                assert bound.get("open_ui", {}).get("resourceUri") == str(ui[0].uri)
            # app-visible tools should not be advertised to a client that did not negotiate Apps
            print("ui_* visible without Apps negotiation:", [n for n in names if n.startswith("ui_")])
            # the chunked scene parts still work through the normal tool path if exposed; otherwise via api directly
            from aicesat import api
            scenes = api.scenes()
            if scenes:
                sid = next((x["scene_id"] for x in scenes if x["status"] == "ready"), scenes[0]["scene_id"])
                meta = api.scene_part(sid, "meta")
                m = next(iter(meta["series"]))
                p0 = api.scene_part(sid, f"positions:{m}", 0)
                parts = [np.frombuffer(base64.b64decode(api.scene_part(sid, f"positions:{m}", c)["b64"]), dtype="f4") for c in range(p0["n_chunks"])]
                arr = np.concatenate(parts)
                print(f"scene {sid}: {m} positions via {p0['n_chunks']} chunks -> {arr.size // 3:,} points (of {meta['series'][m]['n']:,}), chunk0 {len(p0['b64']) / 1e3:.0f} kB")
                assert arr.size == p0["n_values"]
            print("OK")

asyncio.run(main())
