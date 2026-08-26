/* Shell: tabs, hash router, view lifecycle. Views: explore (map + scenes), lake (grid + stats), scene (3-D viewer). */
(function () {
  const U = AICESAT.util, api = AICESAT.api;
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
    document.querySelectorAll('#topbar .tab').forEach(t => t.classList.toggle('on', t.dataset.view === (name === 'scene' ? 'explore' : name)));
    $('crumb').textContent = name === 'scene' ? '› scene ' + r.arg.split('?')[0] : '';
    const v = get(name);
    if (name === 'scene') { const [id, query] = r.arg.split('?'); v.open(id, query); } else v.show();
    current = name;
  }
  document.querySelectorAll('#topbar .tab').forEach(t => t.onclick = () => { location.hash = '#' + t.dataset.view; });
  window.addEventListener('hashchange', route);
  // legacy deep link: /?scene=<id>
  const legacy = new URLSearchParams(location.search).get('scene');
  if (legacy && !location.hash) location.hash = '#scene/' + legacy + (new URLSearchParams(location.search).get('state') === 'on' ? '?state=on' : '');
  route();
})();
