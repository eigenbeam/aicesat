"""Water-surface height per ICESat-2 pass, from ATL03 photons over a lake polygon.

Why this exists rather than using ATL13: ATL13 only reports where its inland-water mask finds a water body, and for
Imja Tsho (27.899 N, 86.925 E) it reports nothing — 0 segments within 800 m across 24 granules, while ATL06 sees the
same lake surface at 4966-4968 m on the same passes. Defining the water polygon ourselves removes that dependency.

Why photons rather than ATL06: ATL06 aggregates 20 m land-ice segments, and its along-track slope fit is the wrong
model for water. A pass over a 600 m wide lake yields thousands of ATL03 photons on a mirror-flat surface, where the
return is a razor-thin mode that a histogram finds without any classifier.

## The estimator, and the bias it does not pretend to remove

A lake's photon height distribution is NOT symmetric:

    noise ......  spread across the whole telemetry window, low density
    SURFACE ####  a very dense, near-delta spike
    subsurface ~  a tail BELOW the surface, from volume scattering and refraction in the water column

The mean is meaningless here. A plain median is fine WHILE the surface spike is the majority of returns — medians
are robust, and that is worth being honest about — but it fails once the subsurface tail outweighs the surface,
which is exactly the turbid or shallow-water case. Anchoring on the histogram MODE and then medianing only the
photons within a narrow window around it holds up in both regimes. `bias_note` (mode minus the whole-pass median) is TWO-SIDED, and the sign says which problem you have:

    positive  most photons sit BELOW the surface mode -> a subsurface/volume-scattering tail, i.e. turbid or
              shallow water. The estimate is probably still good; the pass is just murky.
    negative  most photons sit ABOVE it -> the water mask is letting in higher ground (shoreline, moraine), so the
              mode is the lake and the median is the land around it. Tighten the mask; the estimate may be fine but
              the pass is not measuring only water.

Either way a large magnitude is a reason to distrust the pass, not to correct it silently. Correcting refraction
properly needs the water's optical properties and is out of scope.

Heights are whatever the caller passes in. From lake.query_photons that is `native_height`, WGS84 ellipsoidal —
the same datum as ATL06 h_li, so the two are directly comparable without a geoid step.
"""
from __future__ import annotations

import numpy as np

BIN_M = 0.25        # histogram bin: finer than the surface spread, coarser than photon ranging precision (~3 cm)
WIN_M = 0.75        # half-window around the mode that defines "on the surface"
MIN_PHOTONS = 20    # below this a mode is not distinguishable from a noise cluster
MIN_FRAC = 0.10     # the surface must hold this share of the pass's photons, else it is not a surface


def surface_height(h: np.ndarray, bin_m: float = BIN_M, win_m: float = WIN_M,
                   min_photons: int = MIN_PHOTONS, min_frac: float = MIN_FRAC) -> dict | None:
    """One pass's water-surface height from its photon heights, or None if this pass has no usable surface.

    Returns z (the estimate), n_surface, mad, frac (share of photons on the surface) and bias_note (mode minus the
    whole-pass median — how much subsurface/noise tail this pass carried)."""
    h = np.asarray(h, dtype="f8")
    h = h[np.isfinite(h)]
    if h.size < min_photons:
        return None
    lo, hi = np.percentile(h, [0.5, 99.5])          # trim the telemetry window's far noise before binning
    if not np.isfinite([lo, hi]).all() or hi - lo < bin_m:
        lo, hi = h.min(), h.max() + bin_m
    edges = np.arange(lo, hi + bin_m, bin_m)
    if edges.size < 3:
        return None
    counts, _ = np.histogram(h, bins=edges)
    if not counts.any():
        return None
    peak = int(np.argmax(counts))
    mode = 0.5 * (edges[peak] + edges[peak + 1])
    sel = np.abs(h - mode) <= win_m
    n_surf = int(sel.sum())
    if n_surf < min_photons or n_surf / h.size < min_frac:
        return None
    z = float(np.median(h[sel]))
    return {"z": z, "mode": float(mode), "n_surface": n_surf, "n_photons": int(h.size),
            "frac": float(n_surf / h.size),
            "mad": float(np.median(np.abs(h[sel] - z))),
            "spread_m": float(h[sel].max() - h[sel].min()),
            "bias_note": float(mode - np.median(h))}


def passes(arrays: dict, **kw) -> list[dict]:
    """Group photons into passes — one (granule, beam) crossing — and solve each for a water level.

    A "pass" must be one granule AND one beam: the six beams cross the lake at different points and times within a
    granule, so pooling them would average away exactly the per-crossing detail that makes a level series useful.
    """
    lon, lat, h, t = (np.asarray(arrays[k]) for k in ("lon", "lat", "h", "t"))
    gi = np.asarray(arrays.get("granule_idx", np.zeros(h.size, "i2")))
    bi = np.asarray(arrays.get("beam_idx", np.zeros(h.size, "i1")))
    out = []
    for key in sorted({(int(a), int(b)) for a, b in zip(gi, bi)}):
        m = (gi == key[0]) & (bi == key[1])
        s = surface_height(h[m], **kw)
        if s is None:
            continue
        ts = t[m]
        s.update({"granule_idx": key[0], "beam_idx": key[1],
                  "t": ts[len(ts) // 2], "lon": float(np.median(lon[m])), "lat": float(np.median(lat[m]))})
        out.append(s)
    out.sort(key=lambda r: r["t"])
    return out


def series(rows: list[dict]) -> dict:
    """Collapse passes into a level time series and fit a trend.

    Passes within the same day are averaged: the six beams of one overpass are not independent samples of the lake's
    level, and counting them as six would shrink the reported uncertainty by ~sqrt(6) for free."""
    if not rows:
        return {"epochs": [], "n_passes": 0, "trend_m_per_yr": None}
    day = {}
    for r in rows:
        d = np.datetime64(r["t"], "D")
        day.setdefault(d, []).append(r)
    epochs = []
    for d in sorted(day):
        zs = np.array([r["z"] for r in day[d]])
        epochs.append({"date": str(d), "z": float(np.median(zs)), "n_passes": len(zs),
                       "beam_spread_m": float(zs.max() - zs.min()) if zs.size > 1 else 0.0,
                       "n_surface": int(sum(r["n_surface"] for r in day[d]))})
    out = {"epochs": epochs, "n_passes": len(rows), "trend_m_per_yr": None, "resid_sd_m": None, "span_yr": None}
    if len(epochs) >= 3:
        yr = np.array([np.datetime64(e["date"]).astype("datetime64[D]").astype(int) / 365.25 for e in epochs])
        z = np.array([e["z"] for e in epochs])
        slope, icept = np.polyfit(yr, z, 1)
        resid = z - (slope * yr + icept)
        out.update({"trend_m_per_yr": float(slope), "resid_sd_m": float(resid.std(ddof=1)),
                    "span_yr": float(yr.max() - yr.min())})
    return out


# --- photon-derived water mask -----------------------------------------------------------------------------------
# A circle or a hand-drawn ring has to be guessed and then re-guessed. The photons already know where the water is:
# a lake is the one surface in a mountain scene that is both DENSE and HORIZONTAL over hundreds of metres. So find
# the water elevation from a seed, then grow the mask to every grid cell dominated by a flat population at that
# elevation. On Imja a 400 m seed circle caught 2 crossings out of a 2.8 km lake; the constraint is epochs, and
# epochs come from crossings.
CELL_M = 100.0      # mask grid: finer than the lake, coarser than the ~0.7 m along-track photon spacing
BAND_M = 0.5        # a photon is "at the water elevation" within this
MIN_IN_BAND = 25    # photons in-band per cell before the cell can be called water
MIN_CELL_FRAC = 0.5 # in-band photons must be this share of the cell's photons: excludes a hillside merely crossing z


def _grid(lon, lat, cell_m=CELL_M):
    lo0, la0 = float(np.median(lon)), float(np.median(lat))
    mx = 111e3 * np.cos(np.radians(la0))
    return (np.floor((lon - lo0) * mx / cell_m).astype("i8"),
            np.floor((lat - la0) * 111e3 / cell_m).astype("i8"))


def water_mask(lon, lat, h, z: float, cell_m: float = CELL_M, band_m: float = BAND_M,
               min_in_band: int = MIN_IN_BAND, min_cell_frac: float = MIN_CELL_FRAC) -> np.ndarray:
    """Boolean over the photons: True for those in a grid cell dominated by flat returns at elevation `z`.

    Returns EVERY photon of a qualifying cell, not just the in-band ones — the per-pass estimator needs the full
    height distribution of the water to judge its own noise and subsurface tail.

    `min_cell_frac` is what keeps a hillside out. A slope crossing the lake's elevation contributes a few in-band
    photons to a cell whose photons are mostly elsewhere in height; a water cell is in-band almost throughout.
    """
    lon, lat, h = (np.asarray(x, dtype="f8") for x in (lon, lat, h))
    gx, gy = _grid(lon, lat, cell_m)
    in_band = np.abs(h - z) <= band_m
    keys = (gx.astype("i8") << 32) + gy.astype("i8")
    uniq, inv = np.unique(keys, return_inverse=True)
    total = np.bincount(inv, minlength=uniq.size)
    band = np.bincount(inv, weights=in_band.astype("f8"), minlength=uniq.size)
    good = (band >= min_in_band) & (band / np.maximum(total, 1) >= min_cell_frac)
    return good[inv]


def find_water(lon, lat, h, seed_lon: float, seed_lat: float, seed_radius_m: float = 400.0,
               **kw) -> tuple[np.ndarray, dict] | tuple[None, dict]:
    """Locate the water elevation from a seed neighbourhood, then grow a mask over the whole footprint.

    The seed only has to land ON the lake — it does not have to contain it. That is the point: a small, confidently
    placed seed beats a large guessed outline.
    """
    lon, lat, h = (np.asarray(x, dtype="f8") for x in (lon, lat, h))
    d = np.hypot((lon - seed_lon) * 111e3 * np.cos(np.radians(seed_lat)), (lat - seed_lat) * 111e3)
    near = d <= seed_radius_m
    info = {"seed_photons": int(near.sum())}
    if not near.any():
        return None, info | {"why": "no photons within the seed radius"}
    s = surface_height(h[near], **{k: v for k, v in kw.items() if k in ("bin_m", "win_m", "min_photons", "min_frac")})
    if s is None:
        return None, info | {"why": "no flat surface found in the seed neighbourhood"}
    z = s["z"]
    m = water_mask(lon, lat, h, z, **{k: v for k, v in kw.items()
                                      if k in ("cell_m", "band_m", "min_in_band", "min_cell_frac")})
    cell_m = kw.get("cell_m", CELL_M)
    gx, gy = _grid(lon, lat, cell_m)
    n_cells = len({(int(a), int(b)) for a, b in zip(gx[m], gy[m])}) if m.any() else 0
    # SAMPLED area, not lake area. The mask is built FROM photons, so it can only cover ground a beam actually
    # crossed — roughly (track length through the water) x cell_m, never the whole surface. On Imja this reads
    # ~0.19 km2 against a lake of ~1.3 km2, and that is correct behaviour, not a truncated mask.
    info.update({"z_water": z, "seed_mad_m": s["mad"], "n_photons": int(m.sum()), "n_cells": n_cells,
                 "sampled_km2": n_cells * (cell_m / 1000.0) ** 2})
    return m, info


# --- the ground beside the water ----------------------------------------------------------------------------------
# Brencher et al. (2026) argue the NE moraine bordering Imja will subside BELOW the lake level, letting the lake
# expand west and narrowing the dam. That argument is framed against "the current lake level", which their InSAR
# cannot measure (open water decorrelates). Measuring the margin ground from the SAME photons as the water puts both
# sides of the gap in one instrument and one datum (WGS84 ellipsoidal), so no cross-dataset registration is assumed.
MARGIN_WIN_M = 2.0      # ground is rough: a wider window than water, or the mode test rejects every land cell
MARGIN_MIN_PHOTONS = 40


def _neighbours(key: int) -> list[int]:
    gx, gy = key >> 32, key & 0xFFFFFFFF
    return [((gx + dx) << 32) + (gy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx or dy)]


MAX_WATER_FRAC = 0.25   # above this share of SIGNAL photons at the water elevation, a "margin" cell is really lake
SIGNAL_M = 50.0         # photons within this of the mode are the surface neighbourhood; the rest is telemetry noise
MIN_GROUND_RELIEF_M = 0.5   # below this a "margin" cell is too flat to be moraine — it is another water body


def margin(lon, lat, h, z_water: float, water: np.ndarray, cell_m: float = CELL_M,
           win_m: float = MARGIN_WIN_M, min_photons: int = MARGIN_MIN_PHOTONS,
           band_m: float = BAND_M, max_water_frac: float = MAX_WATER_FRAC) -> list[dict]:
    """Ground elevation in each cell ADJACENT to the water, as height above the water surface.

    Returns one row per margin cell, lowest first — the lowest ground is what the lake reaches first, so the minimum
    is the number the hazard argument turns on, not the mean. `bearing_deg` (0 = N, 90 = E) is measured from the
    sampled water's centroid, so a specific sector like "the northeastern margin" can be selected.

    Rough ground has no razor-thin mode, so this uses a wider window than the water estimator. `relief_m` is the
    p5-p95 height range of the SURFACE NEIGHBOURHOOD (within SIGNAL_M of the mode) — two earlier attempts failed
    here and both are worth remembering: the range inside the mode window saturates at 2*win_m and read 3-4 m for
    every cell, and the range over ALL photons read ~1300 m because at low signal-confidence most of a cell is
    background spread over the telemetry window. Even inside the neighbourhood a p5-p95 is pulled by the residual
    noise in its tails (29 m for a cell whose ground spans 2 m), so the quartiles do the work: the surface is the
    bulk of the band, and the middle 50% is it. `p5_p95_all_m` is kept in the output so the noise level stays
    visible rather than hidden.

    Cells whose photons are largely AT the water elevation are dropped, not reported as very low ground. The mask
    threshold is a hard edge on a soft boundary, so a cell can be mostly lake and still fall outside it; on Imja
    seven such cells came back as "ground" 0.02-0.30 m BELOW the water, with crossing times of 3-5 years attached.
    Dry ground cannot sit below the lake it borders, so that number was the lake measuring itself.
    """
    lon, lat, h = (np.asarray(x, dtype="f8") for x in (lon, lat, h))
    gx, gy = _grid(lon, lat, cell_m)
    keys = (gx.astype("i8") << 32) + gy.astype("i8")
    wet = set(np.unique(keys[water]).tolist())
    if not wet:
        return []
    clon, clat = float(np.median(lon[water])), float(np.median(lat[water]))
    cand = {n for k in wet for n in _neighbours(k)} - wet
    out = []
    for k in cand:
        m = keys == k
        n = int(m.sum())
        if n < min_photons:
            continue
        hm = h[m]
        s = surface_height(hm, win_m=win_m, min_photons=min_photons, min_frac=0.05)
        if s is None:
            continue
        # Both diagnostics are computed over the SURFACE NEIGHBOURHOOD, not the whole cell. At min_conf 0 most of a
        # cell's photons are background spread across the telemetry window: on Imja that made "height range inside a
        # cell" read 1300 m, and diluted the water fraction of genuinely wet cells to 21-25% — just under the
        # threshold meant to catch them. Measuring relative to the mode removes the noise from both.
        sig = hm[np.abs(hm - s["mode"]) <= SIGNAL_M]
        if sig.size < min_photons:
            continue
        wfrac = float(np.mean(np.abs(sig - z_water) <= band_m))
        if wfrac > max_water_frac:
            continue                       # mostly lake: not margin ground
        lo, la = float(np.median(lon[m])), float(np.median(lat[m]))
        dx = (lo - clon) * 111e3 * np.cos(np.radians(clat))
        dy = (la - clat) * 111e3
        relief = float(np.subtract(*np.percentile(sig, [75, 25])))
        out.append({"lon": lo, "lat": la, "n_photons": n, "n_signal": int(sig.size), "water_frac": wfrac,
                    # Nothing on a moraine is flat to a few decimetres over a 100 m cell. water_frac cannot catch a
                    # SEPARATE pond, because it only asks whether photons sit at the MAIN lake's elevation — four
                    # cells 0.65 m above Imja, flat to 0.2-0.3 m, sailed through it and ranked near the top with a
                    # 5-year crossing time. Flatness is the test that catches a water body at any level.
                    "flat_like_water": bool(relief < MIN_GROUND_RELIEF_M),
                    "z_ground": s["z"], "above_water_m": s["z"] - z_water,
                    "relief_m": relief,
                    "p5_p95_all_m": float(np.subtract(*np.percentile(hm, [95, 5]))), "spread_m": s["spread_m"],
                    "bearing_deg": float(np.degrees(np.arctan2(dx, dy)) % 360.0),
                    "range_m": float(np.hypot(dx, dy))})
    out.sort(key=lambda r: r["above_water_m"])
    return out


def years_to_crossing(above_water_m: float, subsidence_m_per_yr: float) -> float | None:
    """How long until ground this far above the water reaches it, at a given subsidence rate.

    Sign convention: `subsidence_m_per_yr` is NEGATIVE for sinking (as an InSAR vertical velocity is reported).
    Returns None when the ground is not sinking or is already below the water — the linear extrapolation is only
    meaningful in the one case, and returning a number for the others invites reading a rate into a non-event.
    """
    if subsidence_m_per_yr >= 0 or above_water_m <= 0:
        return None
    return float(above_water_m / -subsidence_m_per_yr)
