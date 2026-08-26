/* Shell: tabs, hash router, view lifecycle. Views: explore (map + scenes), lake (grid + stats), scene (3-D viewer). */
AICESAT.ready.then(api => {
  const U = AICESAT.util;
  const views = {};
  const $ = id => document.getElementById(id);
  $('adapterInfo').textContent = api.kind === 'app' ? 'inside Claude' : 'localhost';
  function get(name) {
    if (views[name]) return views[name];
    const root = $('view-' + name);
    if (name === 'explore') views[name] = new AICESAT.ExploreView(root, api, id => { location.hash = '#scene/' + id; });
    if (name === 'lake') views[name] = new AICESAT.LakeView(root, api);
    if (name === 'scene') views[name] = new AICESAT.SceneView(root, api, () => { location.hash = '#explore'; });
    return views[name];
  }
  let current = null;
  function route() {
    const r = U.route();
    const name = ['explore', 'lake', 'scene'].includes(r.view) ? r.view : 'explore';
    if (current && current !== name) get(current).hide();
    document.querySelectorAll('#topbar .tab[data-view]').forEach(t => t.classList.toggle('on', t.dataset.view === (name === 'scene' ? 'explore' : name)));
    $('crumb').textContent = name === 'scene' ? '› scene ' + r.arg.split('?')[0] : '';
    const v = get(name);
    if (name === 'scene') { const [id, query] = r.arg.split('?'); v.open(id, query); } else v.show();
    current = name;
  }
  document.querySelectorAll('#topbar .tab[data-view]').forEach(t => t.onclick = () => { location.hash = '#' + t.dataset.view; });
  window.addEventListener('hashchange', route);

  // ---- MCP App specifics
  if (api.kind === 'app') {
    document.documentElement.classList.add('app');
    let fs = false;
    $('fullscreenBtn').hidden = false;
    $('fullscreenBtn').onclick = () => { fs = !fs; document.documentElement.classList.toggle('fs', fs); api.fullscreen(fs ? 'fullscreen' : 'inline'); setTimeout(reportSize, 60); };
    const reportSize = () => { try { const h = document.documentElement.classList.contains('fs') ? window.innerHeight : (parseInt(getComputedStyle(document.documentElement).getPropertyValue('--app-h')) || 660); const a = api.app; if (a && a.sendSizeChanged) a.sendSizeChanged({height: h, width: document.documentElement.clientWidth}); } catch (e) {} };
    window.addEventListener('resize', reportSize); setTimeout(reportSize, 100);
    AICESAT.applyHostContext = ctx => {
      if (!ctx) return;
      if (ctx.theme) document.documentElement.dataset.theme = ctx.theme;
      // if the host tells us its viewport height, use it for the inline size
      const vh = ctx.viewport && ctx.viewport.height || (ctx.safeAreaInsets ? null : null);
      if (vh && !document.documentElement.classList.contains('fs')) document.documentElement.style.setProperty('--app-h', Math.max(420, Math.min(900, vh)) + 'px');
      reportSize();
    };
    // the tool result that launched this instance decides the first view
    const onResult = r => { const sc = r && r.structuredContent; if (!sc) return; if (sc.scene_id) location.hash = '#scene/' + sc.scene_id; else if (sc.view) location.hash = '#' + sc.view; else if (!location.hash) location.hash = '#explore'; route(); };
    AICESAT.onToolResult = onResult;
    (AICESAT.pendingToolResults || []).forEach(onResult);
    if (AICESAT.lastToolResult) onResult(AICESAT.lastToolResult);
    // supersession: the newest instance in this conversation wins; older iframes go quiet
    try {
      const bc = new BroadcastChannel('aicesat-app'); const me = Math.random().toString(36).slice(2);
      bc.postMessage({id: me});
      bc.onmessage = e => { if (e.data && e.data.id !== me) { document.body.innerHTML = '<div style="padding:24px;color:#a6a39a;font:13px ui-sans-serif,system-ui">This view was superseded by a newer one below. Scroll down to the latest altimetry UI.</div>'; } };
    } catch (e) {}
  }
  // legacy deep link: /?scene=<id>
  const legacy = new URLSearchParams(location.search).get('scene');
  if (legacy && !location.hash) location.hash = '#scene/' + legacy;
  route();
});
