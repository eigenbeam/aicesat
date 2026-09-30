/* The mission timeline strip -- legend AND show/hide control at every level of the demo ladder -- plus the pure
   helpers the change map colours with. Each mission is a bar on one 2003-2027 axis, so the strip also says WHEN it
   flew. Pure helpers are tested by tests/test_timeline.js. */
window.AICESAT = window.AICESAT || {};
(function () {
  const T0 = 2003, T1 = 2027;
  const SPANS = {GLAS: [2003, 2009.8], ICESSN: [2009, 2019.9], ATL06: [2018.8, 2026.9], ICESAT2: [2018.8, 2026.9],
                 GEDI: [2019.3, 2026.9], GPSTRUTH: [2006.6, 2025.9]};
  const SHORT = {GLAS: 'ICESat', ICESSN: 'IceBridge', ATL06: 'ICESat-2 · land ice', ICESAT2: 'ICESat-2 · photons',
                 GEDI: 'GEDI', GPSTRUTH: 'GPS traverse'};
  const place = m => { const s = SPANS[m] || [T0, T1]; return {left: (s[0] - T0) / (T1 - T0) * 100, width: (s[1] - s[0]) / (T1 - T0) * 100}; };

  // Diverging change colour: red = surface fell, blue = surface rose, near-white = no change, clipped at +-lim.
  // Low confidence is grey whatever its trend (the confidence gate's verdict is the colour's too).
  const GREY = [150, 150, 158, 90];
  function trendColor(trendCmYr, level, lim) {
    if (level === 'low' || !Number.isFinite(trendCmYr)) return GREY;
    const t = Math.max(-1, Math.min(1, trendCmYr / (lim || 1))), a = level === 'high' ? 215 : 170;
    return t < 0 ? [Math.round(245 - 35 * -t), Math.round(245 - 185 * -t), Math.round(245 - 205 * -t), a]
                 : [Math.round(245 - 195 * t), Math.round(245 - 120 * t), Math.round(245 - 15 * t), a];
  }
  // Symmetric colour limit: the 98th percentile of |trend| over cells that passed the gate.
  function trendLimit(cands) {
    const v = (cands || []).filter(c => c.level !== 'low' && Number.isFinite(c.trend_cm_yr))
      .map(c => Math.abs(c.trend_cm_yr)).sort((a, b) => a - b);
    return v.length ? Math.max(1, v[Math.floor(0.98 * (v.length - 1))]) : 1;
  }
  // Convex hull (Andrew's monotone chain) of a set of hexes' vertices, as [[lon, lat], ...]: a Study area's polygon.
  function hullOfCells(cells) {
    const pts = [];
    for (const c of cells) for (const [lat, lon] of h3.cellToBoundary(c)) pts.push([lon, lat]);
    pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const half = src => { const h = []; for (const p of src) { while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], p) <= 0) h.pop(); h.push(p); } return h; };
    const lo = half(pts), up = half(pts.slice().reverse());
    return lo.slice(0, -1).concat(up.slice(0, -1)).map(p => [+p[0].toFixed(6), +p[1].toFixed(6)]);
  }
  function mount(host, onToggle) {
    host.classList.add('timeline');
    return {update(missions, visible) {
      const M = AICESAT.missions;
      const ticks = [2003, 2009, 2018, 2026].map(y => `<span class="tl-tick" style="left:${(y - T0) / (T1 - T0) * 100}%">${y}</span>`).join('');
      host.style.height = (26 + missions.length * 22) + 'px';
      host.innerHTML = `<div class="tl-axis">${ticks}</div>` + missions.map((m, i) => {
        const p = place(m), c = M.colorOf(m, null).join(','), on = visible[m] !== false;
        return `<button class="tl-chip${on ? '' : ' off'}" data-m="${m}" title="${((M.MISSIONS[m] || {}).gloss || m)} — click to show/hide"` +
          ` style="left:${p.left}%;width:${p.width}%;top:${24 + i * 22}px;--c:rgb(${c})">${SHORT[m] || m}</button>`;
      }).join('');
      host.querySelectorAll('.tl-chip').forEach(b => b.onclick = () => onToggle(b.dataset.m));
    }};
  }
  // Basemap of the change and study levels: 'dem' = the hillshaded DEM, no imagery; 'imagery' = the satellite image
  // draped on the DEM's heights, unlit, without the DEM wireframe. No imagery for the scene -> the DEM.
  const basemap = (mode, hasImagery) => (mode === 'imagery' && hasImagery) ? {imagery: true, surface: false}
                                                                           : {imagery: false, surface: true};
  // At the change and study levels the measurements draw last and ignore depth. The ice has thinned since the DEM was
  // made, so recent points lie below the terrain and the hex fills draped on it (ATL06 on 69606ee845: median 4.7 m
  // under the DEM, 93% under the fills), and depth testing hid them - worse the closer the camera, as the depth buffer
  // resolved the gap. Not pickable there, or a point drawn on top would steal the click meant for its hex.
  const cloudProps = level => level ? {parameters: {depthTest: false}, pickable: false} : {pickable: true};
  const stack = level => level
    ? ['surface', 'hexgrid', 'graticule', 'candidates', 'clouds', 'axes', 'markers', 'names']
    : ['surface', 'hexgrid', 'graticule', 'clouds', 'candidates', 'axes', 'markers', 'names'];

  AICESAT.timeline = {mount, place, trendColor, trendLimit, hullOfCells, basemap, cloudProps, stack, SPANS, T0, T1};

  // Physical place-name labels, shared by the globe and the scene views. Rows are geonames_data.js's
  // [name, lat, lon, code, rank, id]. Cartographic convention: water and ice in cool blue, land features in warm tan.
  const LAND = new Set(['ISL', 'PK', 'MT', 'MTS', 'NTK']);
  AICESAT.nameLabels = (id, data, getPosition, extra = {}) => new deck.TextLayer({id, data, getPosition,
    getText: r => (r.r || r)[0],
    getColor: d => LAND.has((d.r || d)[3]) ? [232, 208, 165, 240] : [165, 208, 255, 240],
    getSize: d => (d.r || d)[4] === 0 ? 15 : (d.r || d)[4] <= 3 ? 12.5 : 11, sizeUnits: 'pixels',
    fontFamily: 'ui-sans-serif, system-ui, sans-serif', fontWeight: 600, characterSet: 'auto', billboard: true,
    background: true, getBackgroundColor: [10, 14, 22, 175], backgroundPadding: [4, 2],   // the style scene markers use
    parameters: {depthTest: false},   // labels sit at the surface: with depth on they z-fight the terrain and vanish
    ...extra});

  // Greedy screen-space decluttering: keep names in rank order whose label boxes do not overlap one already kept.
  // `project` maps a row to [x, y] pixels (or null when off screen). Pure; tested in tests/test_timeline.js.
  // `occupied` holds keep-out rectangles [x0, y0, x1, y1] (the page title, the timeline strip). Boxes are roomier than
  // the text, so the names read as sparse labels rather than a wall of text over the data.
  AICESAT.declutter = (rows, project, w, h, occupied = []) => {
    const kept = [], boxes = occupied.slice();
    for (const r of rows.slice().sort((a, b) => a[4] - b[4])) {
      const p = project(r); if (!p) continue;
      const [x, y] = p, hw = (r[0].length * 7.5 + 28) / 2, hh = 15;
      if (x < 0 || y < 0 || x > w || y > h) continue;
      if (boxes.some(b => x - hw < b[2] && x + hw > b[0] && y - hh < b[3] && y + hh > b[1])) continue;
      boxes.push([x - hw, y - hh, x + hw, y + hh]); kept.push(r);
    }
    return kept;
  };
})();
