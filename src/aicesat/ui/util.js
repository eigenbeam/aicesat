window.AICESAT = window.AICESAT || {};
(function () {
  const U = AICESAT.util = {};
  U.fmtBytes = b => b == null ? '–' : b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(0) + ' MB' : (b / 1e3).toFixed(0) + ' kB';
  U.fmtAge = s => s == null ? '–' : s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${(s / 3600).toFixed(1)} h ago` : `${(s / 86400).toFixed(1)} d ago`;
  U.fmtN = n => n == null ? '–' : Number(n).toLocaleString();
  U.el = (tag, attrs = {}, html = '') => { const e = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) { if (k === 'class') e.className = v; else if (k.startsWith('on')) e[k] = v; else e.setAttribute(k, v); } if (html) e.innerHTML = html; return e; };
  U.bboxOfPolygon = p => [Math.min(...p.map(q => q[0])), Math.min(...p.map(q => q[1])), Math.max(...p.map(q => q[0])), Math.max(...p.map(q => q[1]))];
  U.areaRing = a => a.bbox ? [[a.bbox[0], a.bbox[1]], [a.bbox[2], a.bbox[1]], [a.bbox[2], a.bbox[3]], [a.bbox[0], a.bbox[3]]] : a.polygon;
  // panel manager: each panel gets a header with a collapse caret + close; collapsed state is remembered.
  U.panels = (root, menu) => {
    const panels = [...root.querySelectorAll('.panel, .exag')];
    const store = (id, v) => { try { localStorage.setItem('aicesat.panel.' + id + '.c', v ? '1' : '0'); } catch (e) {} };
    const stored = id => { try { return localStorage.getItem('aicesat.panel.' + id + '.c') === '1'; } catch (e) { return false; } };
    for (const p of panels) {
      if (p.querySelector(':scope > .phead')) continue;             // idempotent
      const heading = p.querySelector(':scope > h1, :scope > h2');
      const title = p.dataset.title || (heading && heading.textContent.trim()) || p.id;
      if (p.dataset.title && heading) heading.classList.add('phead-dupe');   // hide the in-body title we lifted
      const kids = [...p.childNodes];
      const body = U.el('div', {class: 'pbody'});
      kids.forEach(k => body.appendChild(k));
      const head = U.el('div', {class: 'phead'});
      const caret = U.el('button', {class: 'pcaret', title: 'collapse'}, '▾');
      const label = U.el('span', {class: 'ptitle'}, ''); label.textContent = title;
      const close = U.el('button', {class: 'close', title: 'hide'}, '×');
      head.append(caret, label, close);
      p.append(head, body);
      const setCollapsed = (v, persist = true) => { p.classList.toggle('collapsed', v); caret.textContent = v ? '▸' : '▾'; caret.title = v ? 'expand' : 'collapse'; if (persist) store(p.id, v); };
      caret.onclick = () => setCollapsed(!p.classList.contains('collapsed'));
      label.onclick = () => setCollapsed(!p.classList.contains('collapsed'));
      close.onclick = () => { p.hidden = true; refresh(); };
      if (stored(p.id)) setCollapsed(true, false);
    }
    const refresh = () => { if (!menu) return; menu.innerHTML = '<option value="">panels…</option>' + panels.filter(p => p.hidden).map(p => `<option value="${p.id}">show ${p.dataset.title || p.id}</option>`).join(''); };
    if (menu) menu.onchange = e => { const p = root.querySelector('#' + CSS.escape(e.target.value)); if (p) { p.hidden = false; p.dispatchEvent(new Event('reopen')); } e.target.value = ''; refresh(); };
    refresh();
    return refresh;
  };

  // tiny hash router
  U.route = () => { const h = location.hash.replace(/^#\/?/, ''); const [view, ...rest] = h.split('/'); return {view: view || 'explore', arg: rest.join('/')}; };
})();
