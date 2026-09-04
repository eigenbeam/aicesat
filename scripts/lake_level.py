#!/usr/bin/env python
"""ICESat-2 water-level time series for a lake, from ATL03 photons.

    # 1. index ATL03 over a TIGHT box around the lake (ATL03 granules are large; you only need the water)
    uv run python scripts/build_index.py --bbox 86.90 27.88 86.95 27.92 --workers 8

    # 2. the level series — --auto-mask grows the water footprint from a small seed
    uv run python scripts/lake_level.py --bbox 86.90 27.88 86.95 27.92 --centre 86.925 27.899 --auto-mask

Run it where the index and lake live (the EC2 box), and with AICESAT_S3_DIRECT=1 so the fetch is in-region.

Masks, best first:
  --auto-mask   find the water elevation from a --centre seed, then grow to every grid cell dominated by flat
                returns at that elevation. The seed only has to LAND on the lake. On Imja a 400 m circle caught 2
                crossings of a 2.8 km lake, and crossings are what epochs are made of.
  --polygon     a digitised shoreline, if you have one
  --centre/--radius  a plain circle: fine to sanity-check a spot, wrong for an elongated lake

Why not ATL13: its inland-water mask reports nothing at Imja Tsho (0 segments within 800 m across 24 granules) even
though ATL06 sees the surface on the same passes. Defining the mask here removes that dependency. See lakelevel.py
for the estimator and the subsurface bias it does not silently correct.

NOTE ON CONFIDENCE: this deliberately queries with a LOW signal-confidence threshold. The lake stores only
`signal_conf_landice` (the five ATL03 surface-type columns are collapsed at ingest), and a land-ice classifier is
not the right judge of a water return — the default 3 would discard the surface. The histogram mode does the
discriminating instead, which is why noise in the query is affordable.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--centre", nargs=2, type=float, metavar=("LON", "LAT"), help="circular water mask centre")
    ap.add_argument("--radius", type=float, default=400.0, help="circular mask radius in metres (default 400)")
    ap.add_argument("--polygon", help="file with a [[lon,lat], ...] ring")
    ap.add_argument("--auto-mask", action="store_true",
                    help="RECOMMENDED. Find the water elevation from a small --centre seed, then grow the mask to "
                         "every grid cell dominated by flat returns at that elevation. The seed only has to LAND "
                         "on the lake; it does not have to contain it.")
    ap.add_argument("--seed-radius", type=float, default=300.0, help="seed radius for --auto-mask (default 300 m)")
    ap.add_argument("--cell-m", type=float, default=None, help="mask grid size (default 100 m)")
    ap.add_argument("--window", nargs=2, default=["2018-10-01", "2027-01-01"], metavar=("START", "END"))
    ap.add_argument("--min-conf", type=int, default=0,
                    help="ATL03 land-ice signal confidence floor (default 0: keep almost everything and let the "
                         "histogram mode discriminate; the land-ice classifier is not a judge of water)")
    ap.add_argument("--json", help="write the full result here")
    a = ap.parse_args()

    from aicesat import geom, lake, lakelevel, planner

    bbox = tuple(a.bbox)
    print(f"planning ATL03 over {bbox} {a.window} ...", file=sys.stderr)
    plan = planner.ensure(bbox, tuple(a.window))
    print(f"  {len(plan['granules'])} granules, {len(plan['cells'])} cells", file=sys.stderr)

    q = lake.query_photons(bbox, plan["cells"], a.min_conf, granules=plan["granules"])
    glist = q.pop("_granules")
    n0 = q["lon"].size
    print(f"  {n0:,} photons in the bbox at conf >= {a.min_conf}", file=sys.stderr)
    if not n0:
        print("no photons — is ATL03 indexed over this box?", file=sys.stderr)
        return 1

    # --- water mask
    if a.auto_mask:
        if not a.centre:
            print("--auto-mask needs --centre as the seed", file=sys.stderr)
            return 2
        kw = {"cell_m": a.cell_m} if a.cell_m else {}
        keep, info = lakelevel.find_water(q["lon"], q["lat"], q["h"], a.centre[0], a.centre[1],
                                          seed_radius_m=a.seed_radius, **kw)
        if keep is None:
            print(f"auto-mask failed: {info.get('why')} ({info})", file=sys.stderr)
            return 1
        how = (f"auto ({info['n_cells']} cells, {info['sampled_km2']:.2f} km2 sampled) at "
               f"z={info['z_water']:.3f} m from a {a.seed_radius:.0f} m seed")
        print(f"  water elevation {info['z_water']:.3f} m (seed MAD {info['seed_mad_m']*100:.1f} cm); "
              f"{info['sampled_km2']:.2f} km2 SAMPLED over {info['n_cells']} cells — this is the ground the beams "
              f"crossed, not the lake's area", file=sys.stderr)
    elif a.polygon:
        ring = json.load(open(a.polygon))
        ring = ring.get("coordinates", ring) if isinstance(ring, dict) else ring
        while isinstance(ring[0][0], (list, tuple)):
            ring = ring[0]
        keep = geom.points_in_polygon(q["lon"], q["lat"], ring)
        how = f"polygon ({len(ring)} vertices)"
    elif a.centre:
        lo, la = a.centre
        d = np.hypot((q["lon"] - lo) * 111e3 * np.cos(np.radians(la)), (q["lat"] - la) * 111e3)
        keep = d <= a.radius
        how = f"circle r={a.radius:.0f} m at {lo}, {la}"
    else:
        print("give --centre/--radius or --polygon: the whole bbox is not a lake", file=sys.stderr)
        return 2
    arrays = {k: v[keep] for k, v in q.items()}
    print(f"  {int(keep.sum()):,} photons inside the water mask [{how}]\n", file=sys.stderr)
    if not keep.any():
        print("no photons inside the mask", file=sys.stderr)
        return 1

    rows = lakelevel.passes(arrays)
    out = lakelevel.series(rows)
    print(f"{'date':12} {'z (m, WGS84)':>14} {'passes':>7} {'beam spread':>12} {'photons':>9}")
    for e in out["epochs"]:
        print(f"{e['date']:12} {e['z']:14.3f} {e['n_passes']:7} {e['beam_spread_m']:11.3f} m {e['n_surface']:9,}")
    print(f"\npasses solved: {out['n_passes']} of {len({(r['granule_idx'], r['beam_idx']) for r in rows}) or 0} "
          f"crossings; epochs: {len(out['epochs'])}")
    if out["trend_m_per_yr"] is not None:
        print(f"trend {out['trend_m_per_yr']*100:+.1f} cm/yr over {out['span_yr']:.1f} yr "
              f"(residual sd {out['resid_sd_m']*100:.1f} cm)")
        print("\nA trend from few epochs at mixed seasons conflates seasonal and secular change; treat it as a")
        print("first look, not a rate. Per-pass detail is in --json.")
    else:
        print("too few epochs for a trend")
    worst = sorted(rows, key=lambda r: -abs(r["bias_note"]))[:3]
    if worst:
        print("\nbias_note (mode minus whole-pass median, m): "
              + ", ".join(f"{r['bias_note']:+.2f}" for r in worst))
        print("  positive = subsurface/volume scattering (turbid or shallow water)")
        print("  negative = the mask is admitting ground ABOVE the water; tighten it or use --auto-mask")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"bbox": list(bbox), "mask": how, "granules": glist,
                       "passes": [{k: (str(v) if k == "t" else v) for k, v in r.items()} for r in rows],
                       "series": out}, f, indent=1)
        print(f"\nwrote {a.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
