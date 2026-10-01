"""The UWG demo's MCP surface, over stdio exactly as Claude Desktop sees it: which tools the MODEL can see, then the
three-level question at Jakobshavn.
usage: AICESAT_PROFILE=demo AICESAT_PORT=8793 AICESAT_DATA_DIR=/abs/path/data-uwg uv run scripts/e2e_demo.py [lat lon]
"""
import asyncio, json, os, sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LAT, LON = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else (69.1752, -49.3276)
EXT = {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}


def payload(res):
    return json.loads("\n".join(c.text for c in res.content if getattr(c, "text", None)))


async def main():
    params = StdioServerParameters(command="uv", args=["run", "aicesat-server"], env={**os.environ})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, extensions=EXT) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            vis = lambda t: ((t.meta or {}).get("ui") or {}).get("visibility")
            model = sorted(t.name for t in tools if not vis(t) or "model" in vis(t))
            print("model-visible tools:", model)
            assert model == ["elevation_change", "show_timeseries", "survey_coverage"], model
            sv = payload(await s.call_tool("survey_coverage", {"lat": LAT, "lon": LON, "radius_km": 50}))
            print("survey_coverage:", json.dumps({k: sv[k] for k in ("missions", "indexed", "long_record")}, indent=1))
            ch = payload(await s.call_tool("elevation_change", {"lat": LAT, "lon": LON}))
            while ch.get("status") == "building":
                print("  building…")
                ch = payload(await s.call_tool("elevation_change", {"lat": LAT, "lon": LON}))
            print("elevation_change:", json.dumps({k: ch.get(k) for k in ("status", "scene_id", "n_cells", "n_reliable",
                                                                          "n_low_confidence", "summary")}, indent=1))
            top = ch["cells"][0]
            ts = payload(await s.call_tool("show_timeseries", {"scene_id": ch["scene_id"], "h3": top["h3"]}))
            b = ts["series"][0]["value_m"]
            print("show_timeseries:", top["h3"], ts["level"], ts["trend_cm_yr"],
                  [(round(p["year"], 1), round(p["value_m"] - b, 1), p.get("plane_err_m")) for p in ts["series"]])
            if "sample_geometry" in ts:
                print("sample_geometry:", json.dumps(ts["sample_geometry"], indent=1)[:1500])
            print("OK")

asyncio.run(main())
