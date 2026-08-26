window.AICESAT = window.AICESAT || {};
(function () {
  const U = AICESAT.util = {};
  U.fmtBytes = b => b == null ? '–' : b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(0) + ' MB' : (b / 1e3).toFixed(0) + ' kB';
  U.fmtAge = s => s == null ? '–' : s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${(s / 3600).toFixed(1)} h ago` : `${(s / 86400).toFixed(1)} d ago`;
  U.fmtN = n => n == null ? '–' : Number(n).toLocaleString();
  U.el = (tag, attrs = {}, html = '') => { const e = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) { if (k === 'class') e.className = v; else if (k.startsWith('on')) e[k] = v; else e.setAttribute(k, v); } if (html) e.innerHTML = html; return e; };
  U.bboxOfPolygon = p => [Math.min(...p.map(q => q[0])), Math.min(...p.map(q => q[1])), Math.max(...p.map(q => q[0])), Math.max(...p.map(q => q[1]))];
  U.areaRing = a => a.bbox ? [[a.bbox[0], a.bbox[1]], [a.bbox[2], a.bbox[1]], [a.bbox[2], a.bbox[3]], [a.bbox[0], a.bbox[3]]] : a.polygon;
  // panel manager: every .panel gets a close button; a select lists hidden panels to reopen
  U.panels = (root, menu) => {
    const panels = [...root.querySelectorAll('.panel, .exag')];
    const refresh = () => { if (!menu) return; menu.innerHTML = '<option value="">panels…</option>' + panels.filter(p => p.hidden).map(p => `<option value="${p.id}">show ${p.dataset.title || p.id}</option>`).join(''); };
    for (const p of panels) {
      if (p.querySelector(':scope > .close')) continue;
      const b = U.el('button', {class: 'close', title: 'hide'}, '×'); b.onclick = () => { p.hidden = true; refresh(); }; p.appendChild(b);
    }
    if (menu) menu.onchange = e => { const p = root.querySelector('#' + CSS.escape(e.target.value)); if (p) { p.hidden = false; p.dispatchEvent(new Event('reopen')); } e.target.value = ''; refresh(); };
    refresh();
    return refresh;
  };
  // tiny hash router
  U.route = () => { const h = location.hash.replace(/^#\/?/, ''); const [view, ...rest] = h.split('/'); return {view: view || 'explore', arg: rest.join('/')}; };
})();
