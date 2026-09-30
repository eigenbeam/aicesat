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
  AICESAT.timeline = {mount, place, trendColor, trendLimit, hullOfCells, SPANS, T0, T1};
})();
