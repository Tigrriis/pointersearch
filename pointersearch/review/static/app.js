/* pointersearch review UI. Plain JS, no build step, fully offline. */
(function () {
  const COLORS = { rx: '#f06292', bright: '#ffb74d', thermal: '#ef5350' };
  const STATUS_LABEL = { followup: 'follow-up' };
  const state = { cands: [], markers: new Map(), overlays: new Map(), sel: null, lastId: 0, fitted: false,
    views: new Map(), lens: null, imageCount: 0, shot: new Map() };   // shot: object id -> sighting chosen by the reviewer
  try { state.lens = localStorage.getItem('ps-lens') || null; } catch (e) { /* storage unavailable */ }

  const map = L.map('map', { zoomControl: true, maxZoom: 24 });
  map.setView([0, 0], 2);
  L.control.scale({ imperial: false }).addTo(map);
  // draw order: base map < imagery < saved routes < footprints, candidates, follow-up route
  map.createPane('imagery').style.zIndex = 300;
  map.createPane('routes').style.zIndex = 390;
  const overlayGroup = L.layerGroup().addTo(map);
  const savedRouteGroup = L.layerGroup().addTo(map);
  const markerGroup = L.layerGroup().addTo(map);
  const footprintGroup = L.layerGroup().addTo(map);
  const routeGroup = L.layerGroup().addTo(map);

  fetch('api/summary').then(r => r.json()).then(s => {
    if (s.tiles) L.tileLayer('tiles/{z}/{x}/{y}.png', { maxNativeZoom: 19, maxZoom: 24 }).addTo(map);
  });

  const $ = id => document.getElementById(id);
  const filters = () => ({
    rx: $('f-rx').checked, bright: $('f-bright').checked, thermal: $('f-thermal').checked,
    rejected: $('f-rejected').checked, map: $('f-map').value, routes: $('f-routes').checked,
    minScore: +$('f-minscore').value || 0, minFrames: +$('f-minframes').value || 1,
  });
  const FILTER_IDS = ['f-rx', 'f-bright', 'f-thermal', 'f-rejected', 'f-map', 'f-routes', 'f-minscore', 'f-minframes'];
  // which lenses each "map" choice lays down, bottom first
  const MAP_LENSES = { colour: ['wide', 'unknown'], thermal: ['thermal'], zoom: ['wide', 'unknown', 'zoom'], none: [] };
  try {  // remember filter settings per browser
    const saved = JSON.parse(localStorage.getItem('ps-filters') || '{}');
    FILTER_IDS.forEach(id => { if (id in saved) { const el = $(id); el.type === 'checkbox' ? el.checked = saved[id] : el.value = saved[id]; } });
  } catch (e) { /* storage unavailable */ }
  FILTER_IDS.forEach(id => $(id).addEventListener(id.startsWith('f-min') ? 'input' : 'change', () => {
    try { localStorage.setItem('ps-filters', JSON.stringify(Object.fromEntries(FILTER_IDS.map(i => [i, $(i).type === 'checkbox' ? $(i).checked : $(i).value])))); } catch (e) {}
    render();
  }));

  async function refresh() {
    const [summary, overlays, images, cands, routes] = await Promise.all([
      fetch('api/summary').then(r => r.json()),
      fetch('api/overlays').then(r => r.json()),
      fetch('api/images').then(r => r.json()),
      fetch('api/groups').then(r => r.json()),
      fetch('api/routes?paths=1').then(r => r.json()).catch(() => []),
    ]);
    drawSavedRoutes(routes);
    $('mission').textContent = summary.mission || '';
    state.imageCount = summary.images_ok || 0;
    if (summary.version) $('ver').textContent = 'v' + summary.version;
    $('empty').hidden = summary.images > 0 || importState.busy;
    const st = summary.stale || { total: 0 };
    $('stale').hidden = !st.total || importState.busy;
    if (st.total) $('stale-text').textContent = `${st.total} photo${st.total > 1 ? 's were' : ' was'} processed by an older version. Processing them again lines the map up and updates the search.`;
    const c = summary.candidates || {};
    const g = summary.groups || {};
    $('stats').textContent = `${summary.images_ok} images · ${summary.n_groups || 0} objects (${g.pending || 0} pending · ${g.accepted || 0} accepted · ${g.followup || 0} follow-up · ${g.rejected || 0} rejected) from ${Object.values(c).reduce((a, b) => a + b, 0)} sightings`;

    // one patch per photo; replaced when the photo is lined up or processed again
    overlays.forEach(o => {
      const key = o.png + JSON.stringify(o.bounds);
      const had = state.overlays.get(o.id);
      if (had && had.key === key) return;
      if (had) overlayGroup.removeLayer(had.layer);
      state.overlays.set(o.id, { key, source: o.source, shown: false,
        layer: L.imageOverlay('files/' + o.png + '?v=' + encodeURIComponent(o.bounds.flat().map(v => v.toFixed(6)).join('_')), o.bounds, { opacity: 1, interactive: false, pane: 'imagery' }) });
      state.mapKey = null;
    });
    const al = summary.align;
    state.alignText = al && al.links ? ` · map lined up: ${al.aligned}/${al.photos} photos` : '';
    $('stats').title = al && al.links ? `Photos were matched to their neighbours at ${al.links} overlaps. Matched points were ${al.before_m} m apart as recorded and are ${al.after_m} m apart now.` : '';
    $('stats').textContent += state.alignText;
    state.images = images;
    state.cands = cands;
    // the first view covers what was flown and what is planned
    const pts = images.filter(i => i.lat).map(i => [i.lat, i.lon])
      .concat(routes.flatMap(rt => rt.path.length ? rt.path : rt.points));
    // fit once, and keep fitting while an import is still bringing images in
    if (pts.length && (!state.fitted || (importState.busy && pts.length !== state.fitN))) {
      map.fitBounds(L.latLngBounds(pts).pad(0.2)); state.fitted = true; state.fitN = pts.length;
    }
    render();
  }

  // Each detector scores on its own scale (colour: how unusual; thermal: how far above its
  // surroundings; bright: how vivid), so an object is ranked by the detectors that are ticked.
  // With only "thermal" ticked the list is the thermal hits, strongest first.
  function rankScore(c) {
    const f = filters();
    const on = Object.entries(c.scores || {}).filter(([d]) => f[d]).map(([, v]) => v);
    return on.length ? Math.max(...on) : c.score;
  }
  const fmtScore = (d, v) => d === 'bright' ? v.toFixed(2) : v.toFixed(v >= 100 ? 0 : 1);
  function scoreBadges(c) {
    const f = filters();
    return Object.entries(c.scores || {}).map(([d, v]) =>
      `<span class="badge ${d}${f[d] ? '' : ' dim'}" title="${d === 'rx' ? 'colour: how unusual the colour is for its photo' : d === 'thermal' ? 'thermal: how far it stands above its surroundings' : 'bright: how vivid the colour is, 0 to 1'}">${d} ${fmtScore(d, v)}</span>`).join(' ')
      + (c.red_hot ? ' <span class="badge hot" title="the aircraft marked this as its hottest band">red-hot</span>' : '');
  }

  function visible(c) {
    const f = filters();
    if (!c.detectors.some(d => f[d])) return false;
    if (c.status === 'rejected' && !f.rejected) return false;
    if (c.n_frames < f.minFrames) return false;
    // Score floor applies to the colour-anomaly scale only; thermal and bright use different units.
    if (f.minScore > 0 && c.detectors.includes('rx') && !c.detectors.includes('thermal')) {
      const rxBest = c.rx_score != null ? c.rx_score : (c.detector === 'rx' ? c.score : 0);
      if (rxBest < f.minScore) return false;
    }
    return true;
  }

  function render() {
    const f = filters();
    // lay down the lenses chosen in "map", lower lenses first so zoom sits on colour
    const lenses = MAP_LENSES[f.map] || MAP_LENSES.colour;
    // outlines of the photos of those lenses only (wide ones when the map is off)
    const outlined = lenses.length ? lenses : MAP_LENSES.colour;
    footprintGroup.clearLayers();
    (state.images || []).filter(i => i.footprint && outlined.includes(i.source || 'unknown')).forEach(i => {
      L.polygon(i.footprint, { color: '#5d7a96', weight: 1, fill: false, opacity: 0.6, interactive: false }).addTo(footprintGroup);
    });
    const mapKey = f.map + ':' + state.overlays.size;
    if (state.mapKey !== mapKey) {
      overlayGroup.clearLayers();
      lenses.forEach(lens => state.overlays.forEach(o => { if (o.source === lens) overlayGroup.addLayer(o.layer); }));
      state.mapKey = mapKey;
    }
    f.routes ? map.addLayer(savedRouteGroup) : map.removeLayer(savedRouteGroup);
    const list = $('list');
    list.innerHTML = '';
    markerGroup.clearLayers();
    state.markers.clear();
    const shown = state.cands.filter(visible).sort((a, b) =>
      (f.thermal && !f.rx && !f.bright ? (b.red_hot ? 1 : 0) - (a.red_hot ? 1 : 0) : 0) || rankScore(b) - rankScore(a));
    $('shown').textContent = `showing ${shown.length} of ${state.cands.length}`;
    shown.forEach(c => {
      const color = COLORS[c.detector] || '#fff';
      const m = L.circleMarker([c.lat, c.lon], {
        radius: c.status === 'accepted' ? 9 : c.status === 'followup' ? 8 : 6,
        color: c.status === 'accepted' ? '#3dc47e' : c.status === 'followup' ? '#4fc3f7' : color,
        weight: c.status === 'rejected' ? 1 : 2, fillColor: color, fillOpacity: c.status === 'rejected' ? 0.15 : 0.6,
      }).on('click', () => select(c.id, false));
      m.bindTooltip(`${Object.entries(c.scores || {}).map(([d, v]) => d + ' ' + fmtScore(d, v)).join(' · ')}${c.red_hot ? ' · red-hot' : ''} · ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''} · ${STATUS_LABEL[c.status] || c.status}`);
      markerGroup.addLayer(m);
      state.markers.set(c.id, m);

      const li = document.createElement('li');
      li.dataset.id = c.id;
      if (c.id === state.sel) li.classList.add('sel');
      li.innerHTML = `<img loading="lazy" src="files/${c.best.thumb}">
        <div><div class="title">${scoreBadges(c)}</div>
        <div class="sub">${c.area_m2 ?? '?'} m² · seen in ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''}${c.min_range_m != null ? ' · ' + Math.round(c.min_range_m) + ' m from aircraft' : ''}</div></div>
        <div class="st ${c.status}">${STATUS_LABEL[c.status] || c.status}</div>`;
      li.addEventListener('click', () => select(c.id, true));
      list.appendChild(li);
    });
    if (state.sel && !shown.some(c => c.id === state.sel)) state.sel = null;
    renderDetail();
  }

  // ---- saved routes ---------------------------------------------------------
  const esc = t => String(t).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  let savedRoutesKey = '';
  function drawSavedRoutes(routes) {
    const key = JSON.stringify(routes.map(rt => [rt.id, rt.updated, rt.path.length]));
    if (key === savedRoutesKey) return;              // unchanged: leave any open popup alone
    savedRoutesKey = key;
    savedRouteGroup.clearLayers();
    routes.forEach(rt => {
      const line = rt.path.length ? rt.path : rt.points;
      if (line.length < 2) return;
      const sm = rt.summary || {};
      const facts = [rt.mode === 'area' ? 'search area' : 'line route',
        sm.height_m != null ? `${sm.height_m} m` : null,
        sm.duration_min != null ? `about ${sm.duration_min} min` : null,
        sm.n_photos != null ? `${sm.n_photos} photos` : null].filter(Boolean).join(' · ');
      const popup = `<b>${esc(rt.name)}</b><div class="sub">${facts}</div>`
        + (rt.error ? `<div class="sub">Cannot be flown as saved: ${esc(rt.error)}</div>` : '')
        + `<a class="btn" href="plan#route=${rt.id}">Open in planner</a>`
        + (rt.error ? '' : `<a class="btn" href="api/routes/${rt.id}/kmz" download>KMZ</a>`);
      if (rt.mode === 'area' && rt.points.length >= 3) {
        L.polygon(rt.points, { pane: 'routes', color: '#b39ddb', weight: 1, dashArray: '4 6', fillOpacity: 0.04, interactive: false }).addTo(savedRouteGroup);
      }
      // a wide invisible line under the visible one makes it easy to click
      L.polyline(line, { pane: 'routes', color: '#b39ddb', weight: 14, opacity: 0 }).bindPopup(popup).addTo(savedRouteGroup);
      L.polyline(line, { pane: 'routes', color: '#b39ddb', weight: 2, opacity: 0.9, dashArray: rt.error ? '2 6' : null, interactive: false }).addTo(savedRouteGroup);
      L.marker(line[0], { pane: 'routes', icon: L.divIcon({ className: '', iconSize: [0, 0], iconAnchor: [-6, 22],
        html: `<div class="route-label">${esc(rt.name)}</div>` }) }).bindPopup(popup).addTo(savedRouteGroup);
    });
  }

  function baseName(p) { return (p || '').split(/[\\/]/).pop(); }

  function select(id, pan) {
    state.sel = id;
    document.querySelectorAll('#list li').forEach(li => li.classList.toggle('sel', +li.dataset.id === id));
    const li = document.querySelector(`#list li[data-id="${id}"]`);
    if (li) li.scrollIntoView({ block: 'nearest' });
    const c = state.cands.find(x => x.id === id);
    if (c && pan) map.setView([c.lat, c.lon], Math.max(map.getZoom(), 19));
    renderDetail();
  }

  function renderDetail() {
    const c = state.cands.find(x => x.id === state.sel);
    const el = $('detail');
    if (!c) { el.innerHTML = '<div class="hint">Select a candidate. Keys: <b>A</b> accept, <b>W</b> follow-up waypoint, <b>R</b> reject, <b>U</b> undo, <b>N</b>/<b>P</b> next/prev, <b>V</b> switch lens, <b>F</b> full image.</div>'; return; }
    const b = c.best;
    const view = pickView(c);                    // the lens being shown, once the lenses are known
    const temp = b.extra && b.extra.peak_temp_c != null ? ` · ${b.extra.peak_temp_c.toFixed(1)} °C` : '';
    const shown = shownSighting(c);
    const strip = c.thumbs.length > 1 ? `<div class="strip" title="other photos of this object; tap one to look at that shot">${c.thumbs.map((t, i) =>
      `<img src="files/${t}" data-cand="${c.members[i]}" class="${c.members[i] === shown ? 'on' : ''}">`).join('')}</div>` : '';
    el.innerHTML = `<div class="lens-tabs" id="lens-tabs">${lensTabs(c, view)}</div><img id="main-view" src="${view ? view.url : 'files/' + b.thumb}">${strip}
      <div class="meta"><span>${scoreBadges(c)}${temp}</span>
      <span class="st ${c.status}">${STATUS_LABEL[c.status] || c.status}</span></div>
      <div class="meta"><span>${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}</span><span>${c.area_m2 ?? '?'} m² · ${c.n_sightings} sighting${c.n_sightings > 1 ? 's' : ''} in ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''}</span></div>
      <div class="meta"><span id="view-name">${view ? view.image_name : baseName(b.image_path)}</span><a class="btn" id="full-link" href="image/${view ? view.image_id : b.image_id}" target="_blank">full image</a></div>
      <div class="actions"><button class="accept">Accept (A)</button><button class="followup" title="tag for a second look on another flight">Follow-up (W)</button><button class="reject">Reject (R)</button><button class="undo">Undo (U)</button></div>`;
    el.querySelectorAll('#lens-tabs button[data-lens]').forEach(btn => { btn.onclick = () => setLens(btn.dataset.lens); });
    el.querySelectorAll('.strip img[data-cand]').forEach(img => { img.onclick = () => { state.shot.set(c.id, +img.dataset.cand); renderDetail(); }; });
    loadViews(c);
    el.querySelector('.accept').onclick = () => decide('accepted');
    el.querySelector('.followup').onclick = () => decide('followup');
    el.querySelector('.reject').onclick = () => decide('rejected');
    el.querySelector('.undo').onclick = () => decide('pending');
  }

  // ---- wide / zoom / thermal views of the same moment ---------------------------
  // An object is usually seen in several shots. One shot is shown at a time; within
  // it the reviewer switches lens.
  const viewStamp = c => `${c.best.id}:${state.imageCount}:${state.shot.get(c.id) || 'auto'}`;
  function cachedViews(c) {
    const hit = state.views.get(c.id);
    return hit && hit.stamp === viewStamp(c) ? hit : null;
  }
  function shownSighting(c) {
    const hit = cachedViews(c);
    return hit ? hit.candidate_id : (state.shot.get(c.id) || c.best.id);
  }
  function pickView(c) {
    const hit = cachedViews(c);
    const usable = hit ? hit.views.filter(v => v.available) : [];
    return usable.find(v => v.lens === state.lens) || usable.find(v => v.primary) || usable[0] || null;
  }
  function lensTabs(c, view) {
    const hit = cachedViews(c);
    if (!hit) return '';
    // say so when there is nothing to switch to, so its absence is not a puzzle
    if (hit.views.length < 2) return '<div class="lens-note">Only this lens was saved for this shot.</div>';
    if (!view) return '';
    return hit.views.map(v => `<button data-lens="${v.lens}" class="${v === view ? 'on' : ''}" ${v.available ? '' : 'disabled'}
      title="${v.available ? (v.primary ? 'the lens this object was found in' : v.whole ? 'the split-screen picture saved with this shot: thermal and visible side by side (whole picture)' : 'the same moment through the ' + v.label.toLowerCase() + ' lens') : v.reason}">${v.label}${v.primary ? ' <span class="dot">●</span>' : ''}</button>`).join('');
  }
  async function loadViews(c) {
    if (cachedViews(c)) return;
    const stamp = viewStamp(c);
    const chosen = state.shot.get(c.id);
    const res = await fetch(`api/groups/${c.id}/views` + (chosen ? `?candidate=${chosen}` : '')).catch(() => null);
    if (res && res.status === 404 && chosen) { state.shot.delete(c.id); if (state.sel === c.id) renderDetail(); return; }
    const body = res && res.ok ? await res.json() : { candidate_id: c.best.id, views: [] };
    state.views.set(c.id, { stamp, candidate_id: body.candidate_id, views: body.views });
    if (state.sel === c.id) renderDetail();
  }
  function setLens(lens) {
    state.lens = lens;
    try { localStorage.setItem('ps-lens', lens); } catch (e) { /* storage unavailable */ }
    renderDetail();
  }
  function cycleLens() {
    const c = state.cands.find(x => x.id === state.sel);
    const hit = c ? cachedViews(c) : null;
    const usable = hit ? hit.views.filter(v => v.available) : [];
    if (usable.length < 2) return;
    setLens(usable[(usable.indexOf(pickView(c)) + 1) % usable.length].lens);
  }

  async function decide(status) {
    if (!state.sel) return;
    const id = state.sel;
    const updated = await fetch(`api/groups/${id}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }),
    }).then(r => r.json());
    const i = state.cands.findIndex(x => x.id === id);
    if (i >= 0) state.cands[i] = updated;
    if (status !== 'pending') step(1, true);
    render();
    drawRoute();
  }

  function step(dir, keepIfNone) {
    const ids = [...document.querySelectorAll('#list li')].map(li => +li.dataset.id);     // the order on screen
    const shown = ids.map(id => state.cands.find(c => c.id === id)).filter(Boolean);
    if (!shown.length) return;
    let i = shown.findIndex(c => c.id === state.sel);
    const pendingOnly = shown.filter(c => c.status === 'pending');
    if (dir > 0 && pendingOnly.length) {
      const next = pendingOnly.find(c => shown.indexOf(c) > i) || pendingOnly[0];
      select(next.id, true); return;
    }
    i = (i + dir + shown.length) % shown.length;
    select(shown[i].id, true);
  }

  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || $('mission-dlg').open || $('import-dlg').open || $('export-dlg').open) return;
    const k = e.key.toLowerCase();
    if (k === 'a') decide('accepted');
    else if (k === 'w') decide('followup');
    else if (k === 'r') decide('rejected');
    else if (k === 'u') decide('pending');
    else if (k === 'n' || k === 'j') step(1);
    else if (k === 'p' || k === 'k') step(-1);
    else if (k === 'v') cycleLens();
    else if (k === 'f' && state.sel) { const c = state.cands.find(x => x.id === state.sel); const v = pickView(c); window.open(`image/${v ? v.image_id : c.best.image_id}`); }
  });

  // ---- follow-up mission -------------------------------------------------
  const missionQuery = () => {
    const q = new URLSearchParams();
    if ($('m-transit').value) q.set('transit', $('m-transit').value);
    if ($('m-inspect').value) q.set('inspect', $('m-inspect').value);
    return q.toString();
  };

  let routeSeq = 0;
  async function drawRoute() {
    // Dashed line through the follow-up objects in planned order.
    const seq = ++routeSeq;
    if (!state.cands.some(c => c.status === 'followup')) { routeGroup.clearLayers(); return; }
    const r = await fetch('api/mission?' + missionQuery());
    if (seq !== routeSeq) return;            // a newer request is in flight
    routeGroup.clearLayers();
    if (!r.ok) return;
    const plan = await r.json();
    if (seq !== routeSeq) return;
    const pts = (plan.home ? [plan.home] : []).concat(plan.order.map(o => [o.lat, o.lon]));
    L.polyline(pts, { color: '#4fc3f7', weight: 2, dashArray: '6 6', interactive: false }).addTo(routeGroup);
    plan.order.forEach(o => L.marker([o.lat, o.lon], { interactive: false, icon: L.divIcon({
      className: '', iconSize: [18, 18], iconAnchor: [-6, 20],
      html: `<div style="background:#4fc3f7;color:#06222e;border-radius:9px;width:18px;height:18px;line-height:18px;text-align:center;font-size:11px;font-weight:700">${o.seq}</div>` }) }).addTo(routeGroup));
  }

  async function updateMission() {
    const el = $('m-summary'), dl = $('m-download');
    const r = await fetch('api/mission?' + missionQuery());
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      el.innerHTML = `<div class="err">${e.detail || 'Cannot build mission.'}</div>`;
      dl.classList.add('disabled');
      return;
    }
    const p = await r.json();
    dl.classList.remove('disabled');
    dl.href = 'api/mission.kmz?' + missionQuery();
    el.innerHTML = `<div><b>${p.objects}</b> object${p.objects > 1 ? 's' : ''} · ${p.waypoints} waypoints · about ${p.distance_m} m · about <b>${p.duration_min} min</b></div>`
      + p.warnings.map(w => `<div class="warn">⚠ ${w}</div>`).join('')
      + `<ol>${p.order.map(o => `<li>G${o.object_id} · ${o.lat.toFixed(6)}, ${o.lon.toFixed(6)} · ${o.label}</li>`).join('')}</ol>`;
    drawRoute();
  }

  $('mission-btn').addEventListener('click', async () => {
    if (!$('m-transit').value) {
      const s = await fetch('api/summary').then(r => r.json());
      $('m-transit').value = s.followup.transit_height_m;
      $('m-inspect').value = s.followup.inspect_height_m;
      $('m-transit').max = $('m-inspect').max = s.followup.max_height_m;
    }
    $('mission-dlg').showModal();
    updateMission();
  });
  ['m-transit', 'm-inspect'].forEach(id => $(id).addEventListener('input', updateMission));

  // ---- export: objects, and the map as a GeoTIFF ------------------------------
  const exportState = { options: null, timer: null };
  function fillGsd() {
    const o = exportState.options; if (!o) return;
    const lens = o.lenses.find(l => l.lens === $('g-lens').value); if (!lens) return;
    const steps = [lens.default_gsd_cm, 1, 2, 5, 10, 25, 50].filter((v, i, a) => (i === 0 || v > lens.native_gsd_cm * 0.5) && a.indexOf(v) === i).sort((a, b) => a - b);
    $('g-gsd').innerHTML = steps.map(v => `<option value="${v}" ${v === lens.default_gsd_cm ? 'selected' : ''}>${v} cm per pixel${v === lens.default_gsd_cm ? ' (default)' : v < lens.native_gsd_cm ? ' (finer than the photos)' : ''}</option>`).join('');
  }
  function showExport(st) {
    const el = $('g-status');
    const busy = st.state === 'running';
    $('g-start').disabled = busy;
    ['g-lens', 'g-crs', 'g-gsd'].forEach(id => { $(id).disabled = busy; });
    if (busy) {
      el.innerHTML = `<progress max="${Math.max(1, st.total)}" value="${st.done}"></progress><div>${st.total ? `Laying down photo ${Math.min(st.done + 1, st.total)} of ${st.total}` : 'Starting'}${st.current ? ' · ' + st.current : ''}</div>`;
    } else if (st.state === 'error') {
      el.innerHTML = `<div class="err">${st.message ? st.message.charAt(0).toUpperCase() + st.message.slice(1) + '.' : 'The export failed.'}</div>`;
    } else if (st.state === 'done' && st.result) {
      const r = st.result;
      el.innerHTML = `<div class="ok">Ready: ${r.width} × ${r.height} pixels at ${r.gsd_cm} cm, ${r.size_mb} MB, ${r.crs}.</div>`
        + (r.coarsened ? `<div class="warn">⚠ The area is large, so the pixel size was raised from ${r.requested_gsd_cm} cm to fit.</div>` : '')
        + (r.missing_files ? `<div class="warn">⚠ ${r.missing_files} photo file${r.missing_files > 1 ? 's were' : ' was'} not found, so ${r.low_detail_photos} photo${r.low_detail_photos === 1 ? ' is' : 's are'} at the map's lower detail. Connect the card or folder and export again for full detail.</div>` : '')
        + `<div style="margin-top:8px"><a class="btn primary" href="${r.url}" download>Download ${r.name}</a></div>`;
    } else if (!exportState.options || !exportState.options.lenses.length) {
      el.innerHTML = '<div class="hint">There are no photos on the map yet.</div>';
    } else {
      el.innerHTML = '';
    }
    clearTimeout(exportState.timer);
    if (busy) exportState.timer = setTimeout(pollExport, 800);
  }
  async function pollExport() {
    try { showExport(await fetch('api/geotiff').then(r => r.json())); } catch (e) { /* server stopped */ }
  }
  $('export-btn').addEventListener('click', async () => {
    $('export-dlg').showModal();
    const o = exportState.options = await fetch('api/geotiff/options').then(r => r.json());
    const keep = { lens: $('g-lens').value, crs: $('g-crs').value };
    $('g-lens').innerHTML = o.lenses.map(l => `<option value="${l.lens}">${l.label} (${l.photos} photos)</option>`).join('');
    $('g-crs').innerHTML = o.crs.map(c => `<option value="${c.code}">${c.label}</option>`).join('');
    if (o.lenses.some(l => l.lens === keep.lens)) $('g-lens').value = keep.lens;
    else {                                                    // start from what the map is showing
      const fromMap = { thermal: 'thermal', zoom: 'colour+zoom' }[filters().map];
      if (fromMap && o.lenses.some(l => l.lens === fromMap)) $('g-lens').value = fromMap;
    }
    if (o.crs.some(c => c.code === keep.crs)) $('g-crs').value = keep.crs;
    $('g-form').hidden = $('g-start').hidden = !o.lenses.length;
    fillGsd();
    pollExport();
  });
  $('g-lens').addEventListener('change', fillGsd);
  $('g-start').addEventListener('click', async () => {
    const r = await fetch('api/geotiff', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lens: $('g-lens').value, crs: $('g-crs').value, gsd_cm: +$('g-gsd').value }) });
    const b = await r.json().catch(() => ({}));
    showExport(r.ok ? b : { state: 'error', message: typeof b.detail === 'string' ? b.detail : 'could not start the export' });
  });

  // ---- add images ---------------------------------------------------------
  const importState = { busy: false, timer: null, last: null };
  if (location.pathname.startsWith('/m/')) $('home-link').hidden = false;

  function showImport(st) {
    const busy = st.state === 'running' || st.state === 'aligning' || st.state === 'watching';
    const wasBusy = importState.busy;
    importState.busy = busy;
    $('i-start').hidden = busy; $('i-stop').hidden = !busy;
    $('i-folder').disabled = $('i-watch').disabled = $('i-force').disabled = $('i-browse').disabled = busy;
    const chip = $('import-chip');
    chip.hidden = !busy;
    chip.textContent = st.state === 'watching' ? 'watching for images' : st.state === 'aligning' ? 'lining up the map' : `importing ${st.done}/${st.total}`;
    let html = '';
    if (st.state === 'running') {
      html = `<progress max="${Math.max(1, st.total)}" value="${st.done}"></progress>
        <div>Processing ${st.done} of ${st.total}${st.current ? ' · ' + st.current : ''}</div>`;
    } else if (st.state === 'aligning') {
      html = `<progress></progress><div>Lining the photos up on the map…</div>`;
    } else if (st.state === 'error') {
      html = `<div class="err">${st.message || 'The import failed.'}</div>`;
    } else if (st.message) {
      html = `<div>${st.message}</div>`;
    }
    if (st.state !== 'idle' && st.folder && !busy) html += `<div class="hint">${st.folder}</div>`;
    $('i-status').innerHTML = html;
    if (busy && st.folder && !$('i-folder').value) $('i-folder').value = st.folder;
    if (wasBusy && !busy && st.state !== 'watching') state.fitted = false;   // final fit to everything imported
    if (busy || wasBusy) refresh();                 // pull new overlays and objects onto the map
    clearTimeout(importState.timer);
    if (busy || $('import-dlg').open) importState.timer = setTimeout(pollImport, busy ? 1200 : 4000);
  }
  async function pollImport() {
    try { showImport(await fetch('api/ingest').then(r => r.json())); } catch (e) { /* server stopped */ }
  }
  function openImport() {
    try { if (!$('i-folder').value) $('i-folder').value = localStorage.getItem('ps-folder') || ''; } catch (e) {}
    $('import-dlg').showModal();
    pollImport();
  }
  $('import-btn').addEventListener('click', openImport);
  $('empty-import').addEventListener('click', openImport);
  $('i-browse').addEventListener('click', async () => {
    $('i-browse').disabled = true;
    const r = await fetch('api/browse', { method: 'POST' });
    $('i-browse').disabled = false;
    if (r.ok) { const b = await r.json(); if (b.folder) $('i-folder').value = b.folder; }
    else { const b = await r.json().catch(() => ({})); $('i-status').innerHTML = `<div class="hint">${b.detail || 'Paste the folder path instead.'}</div>`; $('i-folder').focus(); }
  });
  $('i-start').addEventListener('click', async () => {
    const folder = $('i-folder').value.trim();
    const r = await fetch('api/ingest', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, watch: $('i-watch').checked, force: $('i-force').checked }) });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) { $('i-status').innerHTML = `<div class="err">${typeof b.detail === 'string' ? b.detail : 'Could not start the import.'}</div>`; return; }
    try { localStorage.setItem('ps-folder', folder); } catch (e) {}
    $('i-force').checked = false;
    state.views.clear();                          // close-ups depend on what was found
    showImport(b);
  });
  $('stale-btn').addEventListener('click', async () => {
    const res = await fetch('api/ingest/refresh', { method: 'POST' });
    const b = await res.json().catch(() => ({}));
    state.views.clear();
    $('import-dlg').showModal();
    if (!res.ok) { $('i-status').innerHTML = `<div class="err">${typeof b.detail === 'string' ? b.detail : 'Could not start.'}</div>`; return; }
    showImport(b);
  });
  $('i-stop').addEventListener('click', async () => { showImport(await fetch('api/ingest/stop', { method: 'POST' }).then(r => r.json())); });

  refresh().then(drawRoute);
  setInterval(refresh, 5000);
  pollImport();
  if (location.hash === '#import') openImport();
})();
