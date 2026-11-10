/* pointersearch route planner. Plain JS, no build step. */
(function () {
  const $ = id => document.getElementById(id);
  const state = { mode: 'line', pts: [], markers: [], plan: null, seq: 0, timer: null, currentId: null, dirty: false };
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const slug = n => (n.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 50).replace(/-+$/, '')) || 'route';
  // the working drawing is remembered per mission
  const STORE_KEY = 'ps-plan:' + location.pathname.replace(/plan\/?$/, '');
  if (location.pathname.startsWith('/m/')) $('home-link').hidden = false;

  // ---- map ---------------------------------------------------------------
  const map = L.map('map', { maxZoom: 22, doubleClickZoom: false });
  map.setView([-27, 134], 4);
  L.control.scale({ imperial: false }).addTo(map);
  const canvas = L.canvas({ padding: 0.3 });
  const imagery = L.layerGroup();
  const shapeLayer = L.layerGroup().addTo(map);
  const routeLayer = L.layerGroup().addTo(map);
  const vertexLayer = L.layerGroup().addTo(map);

  const online = {
    'Satellite (online)': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      { maxNativeZoom: 19, maxZoom: 22, attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics' }),
    'Street (online)': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      { maxNativeZoom: 19, maxZoom: 22, attribution: '&copy; OpenStreetMap contributors' }),
  };

  async function init() {
    const s = await fetch('api/summary').then(r => r.json());
    $('mission').textContent = `${s.mission} · route planner`;
    document.title = `${s.mission} · route planner`;
    const bases = {};
    if (s.tiles) bases['Offline tiles'] = L.tileLayer('tiles/{z}/{x}/{y}.png', { maxNativeZoom: 19, maxZoom: 22 });
    Object.assign(bases, online);
    Object.values(bases)[0].addTo(map);
    L.control.layers(bases, { 'Mission imagery': imagery }, { collapsed: true }).addTo(map);

    // defaults from the mission config
    const r = s.route;
    $('p-height').value = r.height_m; $('p-height').max = r.max_height_m;
    $('p-speed').value = r.speed_ms;
    $('p-lens').value = r.coverage_lens;
    $('p-front').value = Math.round(r.front_overlap * 100);
    $('p-side').value = Math.round(r.side_overlap * 100);
    const stored = r.photo_lenses.split(',');
    $('l-wide').checked = stored.includes('wide'); $('l-zoom').checked = stored.includes('zoom'); $('l-ir').checked = stored.includes('ir');

    restore();

    const [overlays, images] = await Promise.all([
      fetch('api/overlays').then(r => r.json()), fetch('api/images').then(r => r.json())]);
    overlays.forEach(o => L.imageOverlay('files/' + o.png, o.bounds, { opacity: 0.9, interactive: false }).addTo(imagery));
    if (overlays.length) imagery.addTo(map);
    if (state.pts.length) fit();
    else {
      const pts = images.filter(i => i.lat).map(i => [i.lat, i.lon]);
      if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.3));
    }
    setMode(state.mode, true);
    redraw(true);
    loadSaved();
    zoomHint();
  }

  // ---- persistence ---------------------------------------------------------
  const FIELDS = ['p-name', 'p-height', 'p-speed', 'p-lens', 'p-front', 'p-side', 'p-imode', 'p-ival', 'p-amode', 'p-angle'];
  const CHECKS = ['l-wide', 'l-zoom', 'l-ir'];
  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        mode: state.mode, pts: state.pts, currentId: state.currentId, dirty: state.dirty,
        fields: Object.fromEntries(FIELDS.map(id => [id, $(id).value])),
        checks: Object.fromEntries(CHECKS.map(id => [id, $(id).checked])),
      }));
    } catch (e) { /* storage unavailable */ }
  }
  function restore() {
    try {
      const d = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      if (!d) return;
      state.currentId = d.currentId || null; state.dirty = !!d.dirty;
      state.mode = d.mode === 'area' ? 'area' : 'line';
      state.pts = Array.isArray(d.pts) ? d.pts.filter(p => Array.isArray(p) && p.length === 2) : [];
      Object.entries(d.fields || {}).forEach(([id, v]) => { if ($(id) && v !== '') $(id).value = v; });
      Object.entries(d.checks || {}).forEach(([id, v]) => { if ($(id)) $(id).checked = !!v; });
    } catch (e) { /* ignore */ }
  }

  // ---- drawing ------------------------------------------------------------
  function setMode(mode, quiet) {
    state.mode = mode;
    $('mode-line').classList.toggle('on', mode === 'line');
    $('mode-area').classList.toggle('on', mode === 'area');
    document.querySelector('.planner').classList.toggle('line', mode === 'line');
    syncInputs();
    if (!quiet) redraw();
  }
  function syncInputs() {
    $('p-ival').disabled = $('p-imode').value === 'auto';
    $('p-front').disabled = $('p-imode').value !== 'auto';
    $('p-angle').disabled = $('p-amode').value === 'auto';
  }

  function redraw(keepClean) {
    if (!keepClean) touched();
    vertexLayer.clearLayers();
    shapeLayer.clearLayers();
    state.markers = state.pts.map((p, i) => {
      const m = L.marker(p, {
        draggable: true, bubblingMouseEvents: false,
        icon: L.divIcon({ className: '', iconSize: [20, 20], iconAnchor: [10, 10], html: `<div class="vtx">${i + 1}</div>` }),
      });
      m.on('drag', e => { state.pts[i] = [e.latlng.lat, e.latlng.lng]; touched(); drawShape(); schedule(); });
      m.on('dragend', () => { save(); schedule(0); });
      m.on('contextmenu', () => { state.pts.splice(i, 1); redraw(); });
      m.on('click', () => {});          // swallow so a click on a vertex does not add a point
      return m.addTo(vertexLayer);
    });
    drawShape();
    save();
    schedule(0);
  }

  function drawShape() {
    shapeLayer.clearLayers();
    if (state.mode === 'area' && state.pts.length >= 2) {
      L.polygon(state.pts, { color: '#f5a623', weight: 2, dashArray: '4 6', fillOpacity: 0.06, interactive: false }).addTo(shapeLayer);
    } else if (state.mode === 'line' && state.pts.length >= 2 && !state.plan) {
      L.polyline(state.pts, { color: '#4fc3f7', weight: 2, dashArray: '4 6', interactive: false }).addTo(shapeLayer);
    }
  }

  const MIN_DRAW_ZOOM = 12;
  const HINT = $('hint').textContent;
  function zoomHint() {
    const far = map.getZoom() < MIN_DRAW_ZOOM;
    $('hint').textContent = far ? 'Zoom in to the search area to start drawing. Use the lat, lon box to jump there.' : HINT;
    $('hint').style.color = far ? 'var(--accent)' : '';
    $('map').style.cursor = far ? 'grab' : '';
  }
  map.on('zoomend', zoomHint);
  map.on('click', e => {
    if (map.getZoom() < MIN_DRAW_ZOOM) { zoomHint(); return; }       // avoid continent-sized routes
    state.pts.push([e.latlng.lat, e.latlng.lng]); redraw();
  });

  function fit() {
    if (state.pts.length === 1) map.setView(state.pts[0], 17);
    else if (state.pts.length > 1) map.fitBounds(L.latLngBounds(state.pts).pad(0.25));
  }

  // ---- planning -------------------------------------------------------------
  function request() {
    const req = {
      mode: state.mode, points: state.pts, name: $('p-name').value || 'search',
      height_m: +$('p-height').value, speed_ms: +$('p-speed').value, lens: $('p-lens').value,
      side_overlap: (+$('p-side').value || 0) / 100,
      photo_lenses: ['wide', 'zoom', 'ir'].filter(l => $('l-' + l).checked).join(','),
    };
    const im = $('p-imode').value;
    if (im === 'm') req.interval_m = +$('p-ival').value;
    else if (im === 's') req.interval_s = +$('p-ival').value;
    else req.front_overlap = (+$('p-front').value || 0) / 100;
    if (state.mode === 'area' && $('p-amode').value === 'set') req.angle_deg = +$('p-angle').value;
    return req;
  }

  function schedule(ms) {
    clearTimeout(state.timer);
    state.timer = setTimeout(replan, ms == null ? 200 : ms);
  }

  async function replan() {
    const seq = ++state.seq;
    const need = state.mode === 'line' ? 2 : 3;
    if (state.pts.length < need) {
      state.plan = null; routeLayer.clearLayers(); drawShape();
      $('dl-btn').classList.add('disabled'); $('save-btn').classList.add('disabled');
      $('result').innerHTML = `<div class="hint">${state.mode === 'line'
        ? 'Click the map to draw the line the aircraft should follow. At least two points.'
        : 'Click the map to mark the corners of the search area. At least three corners.'}</div>`;
      return;
    }
    const r = await fetch('api/route/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request()) });
    if (seq !== state.seq) return;
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      state.plan = null; routeLayer.clearLayers(); drawShape();
      $('dl-btn').classList.add('disabled'); $('save-btn').classList.add('disabled');
      $('result').innerHTML = `<div class="err">${typeof e.detail === 'string' ? e.detail : 'Check the settings: a value is missing or out of range.'}</div>`;
      return;
    }
    const p = await r.json();
    if (seq !== state.seq) return;
    state.plan = p;
    drawRoute(p);
    showResult(p);
    $('dl-btn').classList.remove('disabled'); $('save-btn').classList.remove('disabled');
  }

  function drawRoute(p) {
    routeLayer.clearLayers();
    drawShape();
    L.polyline(p.path, { color: '#4fc3f7', weight: 3, interactive: false }).addTo(routeLayer);
    p.photos.forEach(pt => L.circleMarker(pt, { renderer: canvas, radius: 2.5, color: '#06222e', weight: 1, fillColor: '#fff', fillOpacity: 1, interactive: false }).addTo(routeLayer));
    const cap = (pt, text) => L.marker(pt, { interactive: false, icon: L.divIcon({ className: '', iconSize: [0, 0], iconAnchor: [-12, 24], html: `<div class="endcap">${text}</div>` }) }).addTo(routeLayer);
    cap(p.path[0], 'START');
    cap(p.path[p.path.length - 1], 'END');
  }

  function showResult(p) {
    const every = p.trigger === 'multipleTiming'
      ? `every ${p.interval_s} s (about ${p.interval_m} m)` : `every ${p.interval_m} m (about ${p.interval_s} s)`;
    const lensName = p.lens === 'thermal' ? 'Thermal' : 'Wide';
    const rows = [
      ['Photos', `${p.n_photos}, ${every}`],
      ['Route length', `${(p.distance_m / 1000).toFixed(2)} km, ${p.waypoints} waypoints`],
      p.track_spacing_m != null ? ['Sweep lines', `${p.track_spacing_m} m apart, bearing ${p.sweep_angle_deg}°`] : null,
      [`${lensName} frame on ground`, `${p.footprint_m[0]} × ${p.footprint_m[1]} m`],
      [`${lensName} detail`, `${p.gsd_cm} cm per pixel, a person is about ${p.px_across_person} px wide`],
      ['Lenses stored', p.photo_lenses.replace('ir', 'thermal').split(',').join(', ')],
    ].filter(Boolean);
    const shown = p.photos.length < p.n_photos ? `<div class="note">Showing the first ${p.photos.length} photo positions on the map.</div>` : '';
    const [caveats, warns] = [[], []];
    p.warnings.forEach(w => (/^(the estimate|heights are)/.test(w) ? caveats : warns).push(w));
    $('result').innerHTML = `<div class="big">About ${p.duration_min} min at ${p.height_m} m and ${p.speed_ms} m/s</div>`
      + `<table>${rows.map(r => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join('')}</table>` + shown
      + warns.map(w => `<div class="warn">⚠ ${w}</div>`).join('')
      + caveats.map(w => `<div class="note">${w.charAt(0).toUpperCase() + w.slice(1)}.</div>`).join('');
  }

  async function download() {
    if ($('dl-btn').classList.contains('disabled')) return;
    const r = await fetch('api/route/kmz', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request()) });
    if (!r.ok) { replan(); return; }
    const m = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob());
    a.download = m ? m[1] : 'search_route.kmz';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  // ---- saved routes ----------------------------------------------------------
  function touched() { state.dirty = true; $('save-note').textContent = ''; }

  async function loadSaved() {
    const list = await fetch('api/routes').then(r => r.json()).catch(() => []);
    $('saved').innerHTML = list.length ? list.map(r => {
      const sm = r.summary || {};
      const sub = [r.mode === 'area' ? 'search area' : 'line route',
        sm.duration_min != null ? `about ${sm.duration_min} min` : null,
        sm.n_photos != null ? `${sm.n_photos} photos` : null,
        sm.height_m != null ? `${sm.height_m} m` : null].filter(Boolean).join(' · ');
      return `<li data-id="${r.id}" class="${r.id === state.currentId ? 'cur' : ''}">
        <div class="r-main"><div class="r-name" title="${esc(r.name)}">${esc(r.name)}</div><div class="r-sub">${sub}</div></div>
        <button class="btn" data-act="open">Open</button>
        <a class="btn" href="api/routes/${r.id}/kmz" download title="download the KMZ for DJI Pilot 2">KMZ</a>
        <button class="btn" data-act="delete" title="delete this saved route">✕</button></li>`;
    }).join('') : '<li class="hint">No saved routes yet.</li>';
  }

  $('saved').addEventListener('click', async e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const li = btn.closest('li'); const id = li.dataset.id; const name = li.querySelector('.r-name').textContent;
    if (btn.dataset.act === 'delete') {
      if (!confirm(`Delete the saved route "${name}"? This cannot be undone.`)) return;
      await fetch('api/routes/' + id, { method: 'DELETE' });
      if (state.currentId === id) { state.currentId = null; save(); }
      loadSaved();
    } else {
      if (state.dirty && state.pts.length && !confirm('Open this route and discard the unsaved changes to the current drawing?')) return;
      const r = await fetch('api/routes/' + id);
      if (!r.ok) { loadSaved(); return; }
      applyRoute(await r.json());
    }
  });

  function applyRoute(r) {
    const st = r.settings || {};
    const set = (id, v) => { if (v != null && v !== '') $(id).value = v; };
    state.mode = r.mode === 'area' ? 'area' : 'line';
    state.pts = (r.points || []).filter(p => Array.isArray(p) && p.length >= 2).map(p => [p[0], p[1]]);
    $('p-name').value = r.name || 'search';
    set('p-height', st.height_m); set('p-speed', st.speed_ms); set('p-lens', st.lens);
    if (st.front_overlap != null) $('p-front').value = Math.round(st.front_overlap * 100);
    if (st.side_overlap != null) $('p-side').value = Math.round(st.side_overlap * 100);
    if (st.interval_m != null) { $('p-imode').value = 'm'; $('p-ival').value = st.interval_m; }
    else if (st.interval_s != null) { $('p-imode').value = 's'; $('p-ival').value = st.interval_s; }
    else { $('p-imode').value = 'auto'; $('p-ival').value = ''; }
    if (st.angle_deg != null) { $('p-amode').value = 'set'; $('p-angle').value = st.angle_deg; } else $('p-amode').value = 'auto';
    if (st.photo_lenses) { const l = st.photo_lenses.split(','); ['wide', 'zoom', 'ir'].forEach(k => { $('l-' + k).checked = l.includes(k); }); }
    state.currentId = r.id; state.dirty = false;
    setMode(state.mode, true);
    redraw(true);
    fit();
    loadSaved();
    $('save-note').textContent = `Opened "${r.name}".`;
  }

  async function saveRoute() {
    if ($('save-btn').classList.contains('disabled')) return;
    const name = $('p-name').value.trim() || 'search';
    const post = overwrite => fetch('api/routes', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...request(), overwrite }) });
    let r = await post(state.currentId === slug(name));     // saving the route you opened updates it
    if (r.status === 409) {
      if (!confirm(`A route called "${name}" is already saved in this mission. Replace it?`)) return;
      r = await post(true);
    }
    const b = await r.json().catch(() => ({}));
    if (!r.ok) { $('save-note').textContent = ''; $('result').innerHTML = `<div class="err">${typeof b.detail === 'string' ? b.detail : 'Could not save the route.'}</div>`; return; }
    state.currentId = b.id; state.dirty = false; save();
    await loadSaved();
    $('save-note').textContent = `Saved "${b.name}" in this mission.`;
  }

  // ---- wiring ---------------------------------------------------------------
  $('mode-line').onclick = () => setMode('line');
  $('mode-area').onclick = () => setMode('area');
  $('undo-btn').onclick = () => { state.pts.pop(); redraw(); };
  $('clear-btn').onclick = () => {
    if (state.dirty && state.pts.length && !confirm('Start a new route and discard the unsaved changes to the current drawing?')) return;
    state.pts = []; state.currentId = null; $('p-name').value = 'search';
    redraw(true); state.dirty = false; save(); loadSaved();
  };
  $('save-btn').onclick = saveRoute;
  $('dl-btn').onclick = download;
  $('fit-btn').onclick = fit;
  FIELDS.concat(CHECKS).forEach(id => $(id).addEventListener(id === 'p-name' ? 'change' : 'input', () => { touched(); syncInputs(); save(); schedule(); }));
  $('p-imode').addEventListener('change', () => {
    if ($('p-imode').value !== 'auto' && !$('p-ival').value && state.plan) {
      $('p-ival').value = $('p-imode').value === 'm' ? Math.round(state.plan.interval_m) : Math.max(1, Math.round(state.plan.interval_s));
      save(); schedule(0);
    }
  });
  function goto() {
    const m = $('goto').value.trim().split(/[\s,]+/).map(Number);
    if (m.length === 2 && m.every(Number.isFinite) && Math.abs(m[0]) <= 90 && Math.abs(m[1]) <= 180) map.setView(m, 16);
    else $('goto').select();
  }
  $('goto-btn').onclick = goto;
  $('goto').addEventListener('keydown', e => { if (e.key === 'Enter') goto(); });
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !/INPUT|SELECT/.test(e.target.tagName)) { state.pts.pop(); redraw(); }
  });

  init();
})();
