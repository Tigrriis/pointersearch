/* pointersearch route planner. Plain JS, no build step. */
(function () {
  const $ = id => document.getElementById(id);
  const state = { mode: 'line', pts: [], markers: [], plan: null, seq: 0, timer: null };

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
    const s = await fetch('/api/summary').then(r => r.json());
    const bases = {};
    if (s.tiles) bases['Offline tiles'] = L.tileLayer('/tiles/{z}/{x}/{y}.png', { maxNativeZoom: 19, maxZoom: 22 });
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
      fetch('/api/overlays').then(r => r.json()), fetch('/api/images').then(r => r.json())]);
    overlays.forEach(o => L.imageOverlay('/files/' + o.png, o.bounds, { opacity: 0.9, interactive: false }).addTo(imagery));
    if (overlays.length) imagery.addTo(map);
    if (state.pts.length) fit();
    else {
      const pts = images.filter(i => i.lat).map(i => [i.lat, i.lon]);
      if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.3));
    }
    setMode(state.mode, true);
    redraw();
  }

  // ---- persistence ---------------------------------------------------------
  const FIELDS = ['p-name', 'p-height', 'p-speed', 'p-lens', 'p-front', 'p-side', 'p-imode', 'p-ival', 'p-amode', 'p-angle'];
  const CHECKS = ['l-wide', 'l-zoom', 'l-ir'];
  function save() {
    try {
      localStorage.setItem('ps-plan', JSON.stringify({
        mode: state.mode, pts: state.pts,
        fields: Object.fromEntries(FIELDS.map(id => [id, $(id).value])),
        checks: Object.fromEntries(CHECKS.map(id => [id, $(id).checked])),
      }));
    } catch (e) { /* storage unavailable */ }
  }
  function restore() {
    try {
      const d = JSON.parse(localStorage.getItem('ps-plan') || 'null');
      if (!d) return;
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

  function redraw() {
    vertexLayer.clearLayers();
    shapeLayer.clearLayers();
    state.markers = state.pts.map((p, i) => {
      const m = L.marker(p, {
        draggable: true, bubblingMouseEvents: false,
        icon: L.divIcon({ className: '', iconSize: [20, 20], iconAnchor: [10, 10], html: `<div class="vtx">${i + 1}</div>` }),
      });
      m.on('drag', e => { state.pts[i] = [e.latlng.lat, e.latlng.lng]; drawShape(); schedule(); });
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

  map.on('click', e => { state.pts.push([e.latlng.lat, e.latlng.lng]); redraw(); });

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
      $('dl-btn').classList.add('disabled');
      $('result').innerHTML = `<div class="hint">${state.mode === 'line'
        ? 'Click the map to draw the line the aircraft should follow. At least two points.'
        : 'Click the map to mark the corners of the search area. At least three corners.'}</div>`;
      return;
    }
    const r = await fetch('/api/route/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request()) });
    if (seq !== state.seq) return;
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      state.plan = null; routeLayer.clearLayers(); drawShape();
      $('dl-btn').classList.add('disabled');
      $('result').innerHTML = `<div class="err">${typeof e.detail === 'string' ? e.detail : 'Check the settings: a value is missing or out of range.'}</div>`;
      return;
    }
    const p = await r.json();
    if (seq !== state.seq) return;
    state.plan = p;
    drawRoute(p);
    showResult(p);
    $('dl-btn').classList.remove('disabled');
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
    const r = await fetch('/api/route/kmz', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request()) });
    if (!r.ok) { replan(); return; }
    const m = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob());
    a.download = m ? m[1] : 'search_route.kmz';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  // ---- wiring ---------------------------------------------------------------
  $('mode-line').onclick = () => setMode('line');
  $('mode-area').onclick = () => setMode('area');
  $('undo-btn').onclick = () => { state.pts.pop(); redraw(); };
  $('clear-btn').onclick = () => { if (!state.pts.length || confirm('Remove all points?')) { state.pts = []; redraw(); } };
  $('dl-btn').onclick = download;
  $('fit-btn').onclick = fit;
  FIELDS.concat(CHECKS).forEach(id => $(id).addEventListener(id === 'p-name' ? 'change' : 'input', () => { syncInputs(); save(); schedule(); }));
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
