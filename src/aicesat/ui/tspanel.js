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
    GPSTRUTH: {name: 'Summit GPS traverse',     epoch: '2006–',     gloss: 'IS2TGPSSS monthly kinematic-GPS surface elevations along a 15 km transect at Summit — ground truth, not altimetry'},
    ICESSN:  {name: 'IceBridge (ATM)',          epoch: '2009–2019', gloss: 'Operation IceBridge airborne ATM elevations (ICESSN)'},
    GEDI:    {name: 'GEDI',                     epoch: '2019–',     gloss: 'GEDI L2A full-waveform lidar, 25 m footprints — accurate on gentle ground, slope-sensitive above ~30°'},
    ICESAT2: {name: 'ICESat-2 photons (ATL03)', epoch: '2018–',     gloss: 'ICESat-2 ATL03 individual signal photons'},
    ATL06:   {name: 'ICESat-2 land ice (ATL06)', epoch: '2018–',    gloss: 'ICESat-2 ATL06 land-ice height segments'},
  };
  const MISSION_ORDER = ['GLAS', 'GPSTRUTH', 'ICESSN', 'GEDI', 'ICESAT2', 'ATL06'];   // chronological
  // Display palette, applied everywhere (clouds, strip, globe, time-series points) so it also recolours older scenes.
  // Red and blue belong to the change map (surface fell / rose), so no mission may use them: ATL06 was the ramp's blue
  // and IceBridge its red, and a track read as change the data did not show. GLAS yellow, IceBridge magenta, ATL06
  // teal, ATL03 green — each >= 35 deg of hue from both ramp ends and from each other (tests/test_timeline.js).
  const MISSION_COLORS = {GLAS: [240, 228, 66], ICESSN: [222, 102, 222], ATL06: [24, 196, 176], ICESAT2: [120, 220, 90], GEDI: [200, 130, 235], GPSTRUTH: [230, 159, 0]};

  AICESAT.missions = {
    MISSIONS, MISSION_ORDER, MISSION_COLORS,
    // `scene` is the fallback for pre-palette scenes that carried their own colour; the standalone view passes null.
    colorOf: (m, scene) => MISSION_COLORS[m] || (scene && scene.series && scene.series[m] && scene.series[m].color) || [200, 200, 210],
    label: m => (MISSIONS[m] || {}).name || m,
  };

  // A cell's average edge length, from h3 itself -- the Controls grid label already read it there. A hardcoded
  // table here held H3 v3's averages (461 m at res 8) while h3-js v4 reports 531 m, so the two panels disagreed.
  const cellEdgeM = r => (typeof h3 !== 'undefined' && h3.getHexagonEdgeLengthAvg)
    ? Math.round(h3.getHexagonEdgeLengthAvg(r, 'm')) : null;

  const fmtLatLon = (lat, lon, dp) => Math.abs(lat).toFixed(dp) + '°' + (lat >= 0 ? 'N' : 'S') + ' ' +
                                      Math.abs(lon).toFixed(dp) + '°' + (lon >= 0 ? 'E' : 'W');

  // Find the candidate a user means by an H3 cell id (any resolution: its centre is used) or "lat, lon". Returns
  // {index, exact: true} for the candidate containing that point at `res`; otherwise the nearest candidate with
  // {exact: false, km}; {error} for input it cannot read; null when there are no candidates to search.
  function findCandidate(cands, query, res) {
    if (!cands || !cands.length) return null;
    const q = String(query == null ? '' : query).trim();
    let lat, lon;
    if (h3.isValidCell(q)) {
      [lat, lon] = h3.cellToLatLng(q);
    } else {
      const m = q.match(/^(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)$/);
      if (!m) return {error: 'enter an H3 cell id, or a position as "lat, lon"'};
      lat = +m[1]; lon = +m[2];
      if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return {error: 'latitude must be within ±90 and longitude ±180'};
    }
    const target = h3.latLngToCell(lat, lon, res);
    const hit = cands.findIndex(c => c.h3 === target);
    if (hit >= 0) return {index: hit, exact: true};
    let best = -1, km = Infinity;
    cands.forEach((c, i) => { const d = h3.greatCircleDistance([lat, lon], [c.lat, c.lon], 'km'); if (d < km) { km = d; best = i; } });
    return {index: best, exact: false, km};
  }

  function renderCandList(el, cands, sel, onSelect) {
    el.innerHTML = cands.map((c, i) =>
      '<div class="tscand ' + (i === sel ? 'on' : '') + '" data-i="' + i + '"><span class="conf-badge ' + c.level +
      '" title="confidence ' + c.confidence + '">' + c.level + '</span> <b>' + c.n_bins + ' epochs</b> · ' +
      c.span_years + ' yr · ' + c.slope_deg + '° <span class="small">' + c.n_points + ' pts</span>' +
      '<div class="small tscand-where">' + fmtLatLon(c.lat, c.lon, 3) + '</div></div>').join('');
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
    const pe = m.plane_err_max_m;
    el.innerHTML = '<details class="tscomp" open><summary>confidence breakdown (score ' + c.confidence + ')</summary><div class="tscomp-body">' +
      (pe != null ? compRow('slope-removal error', pe.toFixed(2) + ' m', Math.max(0, 1 - pe)) : '') +
      compRow('scatter in the cell', m.roughness_m + ' m', sc.roughness) +
      compRow('time windows', m.epochs, sc.epochs) +
      compRow('record length', m.span_yr + ' yr', sc.span) +
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
      const row = (k, v) => '<div class="ro-row"><span class="ro-k">' + k + '</span><span class="ro-v">' + v + '</span></div>';
      readoutEl.innerHTML = row('Centre', fmtLatLon(c.lat, c.lon, 4)) +
        row('Trend', '<b>' + (c.trend_cm_yr >= 0 ? '+' : '−') + Math.abs(c.trend_cm_yr / 100).toFixed(2) + ' m/yr</b>') +
        row('Record', Math.floor(x0) + '–' + Math.floor(x1) + ' · ' + s.length + ' one-year windows') +
        row('Missions', missions) +
        row('Confidence', '<span class="conf-badge ' + c.level + '">' + c.level + '</span>');
    }
  }

  AICESAT.ts = {cellEdgeM, findCandidate, fmtLatLon, renderCandList, compRow, renderConf, drawChart};
})();
