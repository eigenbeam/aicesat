/* Standalone elevation time-series view (#ts/<scene_id>[?sel=<h3>]).
 *
 * The scene view carries this same panel bolted to a 3-D point cloud. That cloud travels over the push stream,
 * which the MCP App transport cannot carry (tools/call is request/response), so inside Claude Desktop the scene
 * view renders terrain and metadata and no points. This view needs neither: the mission list comes from the scene
 * METADATA and the candidates from one tool call, both of which work over either transport. It is the analysis,
 * without the parts that cannot make the trip.
 */
AICESAT.TsView = class {
  constructor(root, api, back) {
    root.innerHTML = '<div class="tsview">\n' +
      '  <div class="tshead">\n' +
      '    <h2>Elevation time series</h2>\n' +
      '    <div class="small tsintro">Cells observed across time. Height is plotted as a residual about a local reference plane, so surface slope is removed rather than mistaken for change.</div>\n' +
      '    <div class="tscontrols">\n' +
      '      <label class="ctl-row"><span class="ctl-lbl">Cell size</span><input id="tsRes" type="range" min="7" max="11" step="1" value="9" class="ctl-range"><b id="tsResLbl" class="ctl-val"></b></label>\n' +
      '      <label class="ctl-row"><span class="ctl-lbl">Time window</span><input id="tsDt" type="range" min="0.25" max="3" step="0.25" value="1" class="ctl-range"><b id="tsDtLbl" class="ctl-val"></b></label>\n' +
      '      <div class="ctl-row tsrefrow"><span class="ctl-lbl">Reference</span><span id="tsRef" class="tsref"></span></div>\n' +
      '      <div class="row"><button id="tsFind">Find candidates</button><span id="tsStatus" class="small"></span></div>\n' +
      '    </div>\n' +
      '    <div class="tsctx"><canvas id="tsCtx" hidden></canvas><div><button id="tsOpen3d">Open in 3D ↗</button><div class="small" id="tsCtxNote"></div></div></div>\n' +
      '  </div>\n' +
      '  <div class="tsbody">\n' +
      '    <div id="tsList" class="tslist tslist-wide"></div>\n' +
      '    <div class="tsmain">\n' +
      '      <canvas id="tsChart" class="tschart" hidden></canvas>\n' +
      '      <div id="tsReadout" class="small"></div>\n' +
      '      <div id="tsConf" class="small"></div>\n' +
      '      <div id="tsCaveat" class="small tscaveat" hidden>Heights are residuals about each cell’s reference plane. No inter-campaign / inter-sensor bias adjustment and no GIA correction is applied, so the trend inherits any offset between missions.</div>\n' +
      '    </div>\n' +
      '  </div>\n' +
      '</div>';

    const $ = id => root.querySelector('#' + id);
    const TS = AICESAT.ts, M = AICESAT.missions;
    const colorOf = m => M.colorOf(m, null);
    let sceneId = null, present = [], candidates = [], sel = -1;

    const labels = () => {
      const r = +$('tsRes').value;
      $('tsResLbl').textContent = 'res ' + r + ' · ~' + (TS.cellEdgeM(r) || '?') + ' m';
      $('tsDtLbl').textContent = (+$('tsDt').value).toFixed(2) + ' yr';
    };
    const refMissions = () => [...$('tsRef').querySelectorAll('input:checked')].map(i => i.value);

    let meta = null;
    // Where the cell is: the scene's own imagery, every candidate outlined faintly, the selected one bright.
    const drawCtx = async () => {
      const cv = $('tsCtx');
      if (!meta || !meta.imagery || !meta.frame) { cv.hidden = true; return; }
      const src = await api.imageryDataUrl(sceneId); if (!src) { cv.hidden = true; return; }
      const im = new Image();
      im.onload = () => {
        const Wc = 220, Hc = Math.round(Wc * im.height / im.width), d = devicePixelRatio || 1;
        cv.width = Wc * d; cv.height = Hc * d; cv.style.width = Wc + 'px'; cv.style.height = Hc + 'px'; cv.hidden = false;
        const ctx = cv.getContext('2d'); ctx.drawImage(im, 0, 0, cv.width, cv.height);
        const ex = meta.imagery, px = (x, y) => [(x - ex.x0) / (ex.x1 - ex.x0) * cv.width, (1 - (y - ex.y0) / (ex.y1 - ex.y0)) * cv.height];
        const ring = (c, style, lw) => { ctx.beginPath(); c.xy.forEach((p, k) => { const q = px(p[0], p[1]); if (k) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
          ctx.closePath(); ctx.strokeStyle = style; ctx.lineWidth = lw * d; ctx.stroke(); };
        candidates.forEach(c => ring(c, 'rgba(255,255,255,0.35)', 0.8));
        if (sel >= 0) {   // a 530 m cell is ~2 px on a 50 km image: mark it so "where" reads at a glance
          const c = candidates[sel]; ring(c, 'rgb(150,235,255)', 2.5);
          const [qx, qy] = px(c.center[0], c.center[1]), r = 11 * d;
          ctx.lineWidth = 2 * d; ctx.strokeStyle = 'rgba(10,14,22,0.9)'; ctx.beginPath(); ctx.arc(qx, qy, r + d, 0, 2 * Math.PI); ctx.stroke();
          ctx.strokeStyle = 'rgb(150,235,255)'; ctx.beginPath(); ctx.arc(qx, qy, r, 0, 2 * Math.PI); ctx.stroke();
          ctx.beginPath(); ctx.moveTo(qx - 2 * r, qy); ctx.lineTo(qx - r, qy); ctx.moveTo(qx + r, qy); ctx.lineTo(qx + 2 * r, qy);
          ctx.moveTo(qx, qy - 2 * r); ctx.lineTo(qx, qy - r); ctx.moveTo(qx, qy + r); ctx.lineTo(qx, qy + 2 * r); ctx.stroke();
        }
      };
      im.src = src;
    };
    $('tsOpen3d').onclick = () => api.openLink(AICESAT.lastOpenUrl || (location.origin + '/#scene/' + sceneId + '?level=region'));

    // Selection is local: the view already holds every candidate's series, so picking a different cell is a
    // redraw, not a round-trip. That is what makes the list usable inside a chat transport.
    const select = i => {
      sel = i;
      TS.renderCandList($('tsList'), candidates, sel, select);
      const detail = {detail: AICESAT.profile !== 'demo'};   // the demo's #ts embed stays compact (no H3 ids)
      TS.drawChart($('tsChart'), sel < 0 ? null : candidates[sel], colorOf, $('tsReadout'), 220, detail);
      TS.renderConf($('tsConf'), sel < 0 ? null : candidates[sel], detail);
      drawCtx();
    };

    const find = async (selectH3) => {
      if (!sceneId) return;
      $('tsStatus').innerHTML = '<span class="spin-sm"></span>'; $('tsFind').disabled = true; AICESAT.clearError();
      try {
        const d = await api.candidates(sceneId, {h3_res: +$('tsRes').value, delta_t: +$('tsDt').value,
                                                 ref_missions: refMissions(), min_bins: 3});
        candidates = d.candidates || []; sel = -1;
        $('tsStatus').textContent = candidates.length + (candidates.length === 1 ? ' cell' : ' cells');
        $('tsCaveat').hidden = !candidates.length;
        if (!candidates.length) {
          TS.renderCandList($('tsList'), [], -1, select); select(-1);
          $('tsReadout').textContent = 'no cells with 3+ time windows — try a larger cell size or a wider time window';
        } else {
          const i = selectH3 ? candidates.findIndex(c => c.h3 === selectH3) : 0;
          select(i >= 0 ? i : 0);
          if (selectH3 && i < 0) AICESAT.showError('cell ' + selectH3 + ' is not a candidate under these settings; showing the best one instead');
        }
      } catch (e) { $('tsStatus').textContent = 'error'; AICESAT.showError(e); }
      $('tsFind').disabled = false;
    };

    $('tsFind').onclick = () => find();
    $('tsRes').oninput = labels; $('tsDt').oninput = labels;
    $('tsRes').onchange = () => find(); $('tsDt').onchange = () => find();
    labels();

    this.hide = () => { root.classList.remove('on'); };
    this.show = () => { root.classList.add('on'); };

    // `query` is the raw hash query (sel=<h3>&res=&dt=), mirroring how SceneView.open takes its own.
    this.open = async (id, query) => {
      this.show();
      const q = new URLSearchParams(query || '');
      const wantRes = q.get('res'), wantDt = q.get('dt'), wantSel = q.get('sel');
      if (wantRes) $('tsRes').value = wantRes;
      if (wantDt) $('tsDt').value = wantDt;
      labels();
      const sameScene = id === sceneId;
      if (!sameScene) {
        sceneId = id; candidates = []; sel = -1;
        TS.renderCandList($('tsList'), [], -1, select); select(-1);
        $('tsStatus').innerHTML = '<span class="spin-sm"></span>';
        try {
          meta = await api.sceneMeta(id);        // metadata only — no points, no stream
          present = M.MISSION_ORDER.filter(m => meta.series && meta.series[m]);
        } catch (e) { $('tsStatus').textContent = 'error'; AICESAT.showError(e); return; }
        // One era's plane (api.CHANGE_REF): fitted across missions, the plane mistakes change for slope.
        const defRef = present.includes('ATL06') ? ['ATL06'] : present.includes('GLAS') ? ['GLAS'] : present;   // one era's plane: api.CHANGE_REF
        $('tsRef').innerHTML = present.map(m => '<label class="tsref-item"><input type="checkbox" value="' + m + '"' +
          (defRef.includes(m) ? ' checked' : '') + '> ' + M.label(m) + '</label>').join('');
        $('tsRef').querySelectorAll('input').forEach(i => i.onchange = () => find());
        return find(wantSel);
      }
      // Same scene: a re-select is free, since every candidate's series is already here.
      if (wantSel) {
        const i = candidates.findIndex(c => c.h3 === wantSel);
        if (i >= 0) return select(i);
      }
      if (!candidates.length) return find(wantSel);
    };

    this.back = back;
  }
};
