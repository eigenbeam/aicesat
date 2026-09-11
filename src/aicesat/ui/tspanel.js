/* Mission identity + the time-series widgets (ranked candidate list, chart, confidence breakdown).
 *
 * These used to live inside AICESAT.SceneView's constructor, which made them unreachable from anywhere else — so
 * the standalone #ts view would have had to copy them, and two copies of a chart is how two charts come to
 * disagree about the same cell. They take explicit DOM elements instead of closing over a view's `$`.
 */
window.AICESAT = window.AICESAT || {};
(function () {
  // The missions, as users know them (the scene legend doubles as show/hide controls). Keyed by the internal
  // series name; ICESat-2 appears as two products (ATL03 photons + ATL06 land ice).
  const MISSIONS = {
    GLAS:    {name: 'ICESat-1 (GLAS)',          epoch: '2003–2009', gloss: 'ICESat / GLAS laser-altimeter surface heights'},
    ICESSN:  {name: 'IceBridge (ATM)',          epoch: '2009–2019', gloss: 'Operation IceBridge airborne ATM elevations (ICESSN)'},
    GEDI:    {name: 'GEDI',                     epoch: '2019–',     gloss: 'GEDI L2A full-waveform lidar, 25 m footprints — accurate on gentle ground, slope-sensitive above ~30°'},
    ICESAT2: {name: 'ICESat-2 photons (ATL03)', epoch: '2018–',     gloss: 'ICESat-2 ATL03 individual signal photons'},
    ATL06:   {name: 'ICESat-2 land ice (ATL06)', epoch: '2018–',    gloss: 'ICESat-2 ATL06 land-ice height segments'},
  };
  const MISSION_ORDER = ['GLAS', 'ICESSN', 'GEDI', 'ICESAT2', 'ATL06'];   // chronological
  // Display palette (Okabe-Ito subset): distinct, colour-blind-friendly, high-contrast against the charcoal DEM.
  // Applied everywhere (clouds, legend swatches, time-series points) so it also recolours scenes built before it.
  // GLAS yellow, IceBridge vermillion, ATL06 blue — yellow/blue is the CVD-safe axis; ATL03 (rare) takes green.
  const MISSION_COLORS = {GLAS: [240, 228, 66], ICESSN: [230, 75, 60], ATL06: [40, 140, 225], ICESAT2: [40, 200, 120], GEDI: [200, 130, 235]};

  AICESAT.missions = {
    MISSIONS, MISSION_ORDER, MISSION_COLORS,
    // `scene` is the fallback for pre-palette scenes that carried their own colour; the standalone view passes null.
    colorOf: (m, scene) => MISSION_COLORS[m] || (scene && scene.series && scene.series[m] && scene.series[m].color) || [200, 200, 210],
    label: m => (MISSIONS[m] || {}).name || m,
  };

  const H3_EDGE_M = {7: 1220, 8: 461, 9: 174, 10: 66, 11: 25};

  function renderCandList(el, cands, sel, onSelect) {
    el.innerHTML = cands.map((c, i) =>
      '<div class="tscand ' + (i === sel ? 'on' : '') + '" data-i="' + i + '"><span class="conf-badge ' + c.level +
      '" title="confidence ' + c.confidence + '">' + c.level + '</span> <b>' + c.n_bins + ' epochs</b> · ' +
      c.span_years + ' yr · ' + c.slope_deg + '° <span class="small">' + c.n_points + ' pts</span></div>').join('');
    el.querySelectorAll('.tscand').forEach(d => d.onclick = () => onSelect(+d.dataset.i));
  }

  function compRow(label, val, score) {
    return '<div class="comp-row"><span class="comp-lbl">' + label + '</span><span class="comp-val">' + val +
           '</span><span class="comp-bar"><i style="width:' + Math.round((score || 0) * 100) + '%"></i></span></div>';
  }

  function renderConf(el, c) {
    if (!el) return;
    if (!c) { el.innerHTML = ''; return; }
    const m = c.components, sc = m.scores;
    el.innerHTML = '<div class="conf-why"><span class="conf-badge ' + c.level + '">' + c.level + '</span> ' + c.why + '</div>' +
      '<details class="tscomp"><summary>confidence breakdown (' + c.confidence + ')</summary><div class="tscomp-body">' +
      compRow('within-cell roughness', m.roughness_m + ' m', sc.roughness) +
      compRow('epochs (time windows)', m.epochs, sc.epochs) +
      compRow('baseline', m.span_yr + ' yr', sc.span) +
      compRow('reference points', m.ref_pts, sc.density) +
      '</div></details>';
  }

  // The chart: per-window median residual about the cell's reference plane, with the within-window MAD as the
  // error bar and the point coloured by the mission that produced it.
  function drawChart(cv, c, colorOf, readoutEl, height) {
    if (!cv) return;
    if (!c) { cv.hidden = true; if (readoutEl) readoutEl.textContent = ''; return; }
    cv.hidden = false;
    const s = c.series, dpr = devicePixelRatio, Hcss = height || 150;
    const ctx = cv.getContext('2d'); const W = cv.width = cv.clientWidth * dpr;
    cv.style.height = Hcss + 'px'; const H = cv.height = Hcss * dpr;
    ctx.clearRect(0, 0, W, H);
    const padL = 44 * dpr, padR = 8 * dpr, padT = 10 * dpr, padB = 20 * dpr;
    const yrs = s.map(p => p.year), vals = s.map(p => p.value_m), mads = s.map(p => p.mad_m);
    const x0 = Math.min(...yrs), x1 = Math.max(...yrs);
    let ymin = Math.min(...vals.map((v, i) => v - mads[i])), ymax = Math.max(...vals.map((v, i) => v + mads[i]));
    const pd = (ymax - ymin) * 0.15 || 0.1; ymin -= pd; ymax += pd;
    const sx = v => padL + (v - x0) / ((x1 - x0) || 1) * (W - padL - padR);
    const sy = v => padT + (ymax - v) / ((ymax - ymin) || 1) * (H - padT - padB);
    ctx.strokeStyle = '#555'; ctx.lineWidth = dpr; ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, H - padB); ctx.lineTo(W - padR, H - padB); ctx.stroke();
    if (ymin <= 0 && ymax >= 0) { ctx.strokeStyle = '#666'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(padL, sy(0)); ctx.lineTo(W - padR, sy(0)); ctx.stroke(); ctx.setLineDash([]); }
    ctx.fillStyle = '#aaa'; ctx.font = (11 * dpr) + 'px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(ymax.toFixed(2) + ' m', padL - 4 * dpr, sy(ymax) + 8 * dpr); ctx.fillText(ymin.toFixed(2), padL - 4 * dpr, sy(ymin));
    // The end labels sit ON the axis ends, so centring them hangs half of each past the canvas: at the panel width
    // that only nibbled the last digit, at the standalone view's width it read "202(" instead of "2026".
    ctx.textAlign = 'left'; ctx.fillText(x0.toFixed(0), sx(x0), H - 5 * dpr);
    ctx.textAlign = 'right'; ctx.fillText(x1.toFixed(0), sx(x1), H - 5 * dpr);
    ctx.strokeStyle = '#7a7a86'; ctx.lineWidth = 1.2 * dpr; ctx.beginPath(); s.forEach((p, i) => { const X = sx(p.year), Y = sy(p.value_m); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); }); ctx.stroke();
    s.forEach(p => { const X = sx(p.year), col = colorOf(p.missions[0]);
      ctx.strokeStyle = 'rgba(' + col.join(',') + ',0.55)'; ctx.lineWidth = dpr; ctx.beginPath(); ctx.moveTo(X, sy(p.value_m - p.mad_m)); ctx.lineTo(X, sy(p.value_m + p.mad_m)); ctx.stroke();
      ctx.fillStyle = 'rgb(' + col.join(',') + ')'; ctx.beginPath(); ctx.arc(X, sy(p.value_m), 3.4 * dpr, 0, 7); ctx.fill(); });
    if (readoutEl) {
      // trend_cm_yr is computed server-side (timeseries._trend_cm_yr). It used to be a linfit here, which meant the
      // number on the chart and the number an API caller got came from two implementations.
      const missions = [...new Set(s.flatMap(p => p.missions))].map(AICESAT.missions.label).join(' → ');
      readoutEl.innerHTML = 'trend <b>' + Number(c.trend_cm_yr).toFixed(1) + ' cm/yr</b> · ' + s.length +
                            ' epochs over ' + c.span_years + ' yr · ' + missions;
    }
  }

  AICESAT.ts = {H3_EDGE_M, renderCandList, compRow, renderConf, drawChart};
})();
