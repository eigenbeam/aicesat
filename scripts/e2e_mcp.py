"""Drive the MCP server over stdio exactly like Claude Desktop: list tools, then run the three-slice sequence.
usage: AICESAT_PORT=8766 uv run scripts/e2e_mcp.py [region]
"""
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

region = sys.argv[1] if len(sys.argv) > 1 else "egig_west_flank"


def show(name, res):
    txt = "\n".join(c.text for c in res.content if getattr(c, "text", None))
    try:
        obj = json.loads(txt)
    except Exception:
        obj = txt
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in ("granules", "dh_native", "dh_coreg", "artifact", "display_positions")}
    print(f"== {name}: {json.dumps(obj, indent=1, default=str)[:1800]}")
    return obj


async def main():
    params = StdioServerParameters(command="uv", args=["run", "aicesat-server"], env={**os.environ})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", [t.name for t in tools.tools])
            show("check_coverage", await s.call_tool("check_coverage", {"region": region}))
            sp = show("show_photons", await s.call_tool("show_photons", {"region": region,
                                                                        "question": "show me ICESat-2 photons over " + region}))
            sid = sp["scene_id"]
            show("add_glas", await s.call_tool("add_glas", {"scene_id": sid}))
            show("coregister (live)", await s.call_tool("coregister", {"scene_id": sid}))
            show("coregister (cached)", await s.call_tool("coregister", {"scene_id": sid}))
            print("widget:", sp["widget_url"])

asyncio.run(main())
