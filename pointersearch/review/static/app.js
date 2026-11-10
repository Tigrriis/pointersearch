/* pointersearch review UI. Plain JS, no build step, fully offline. */
(function () {
  const COLORS = { rx: '#f06292', bright: '#ffb74d', thermal: '#ef5350' };
  const STATUS_LABEL = { followup: 'follow-up' };
  const state = { cands: [], markers: new Map(), overlays: new Map(), sel: null, lastId: 0, fitted: false };

  const map = L.map('map', { zoomControl: true, maxZoom: 24 });
  map.setView([0, 0], 2);
  L.control.scale({ imperial: false }).addTo(map);
  const overlayGroup = L.layerGroup().addTo(map);
  const markerGroup = L.layerGroup().addTo(map);
  const footprintGroup = L.layerGroup().addTo(map);
  const routeGroup = L.layerGroup().addTo(map);

  fetch('api/summary').then(r => r.json()).then(s => {
    if (s.tiles) L.tileLayer('tiles/{z}/{x}/{y}.png', { maxNativeZoom: 19, maxZoom: 24 }).addTo(map);
  });

  const $ = id => document.getElementById(id);
  const filters = () => ({
    rx: $('f-rx').checked, bright: $('f-bright').checked, thermal: $('f-thermal').checked,
    rejected: $('f-rejected').checked, overlays: $('f-overlays').checked,
    minScore: +$('f-minscore').value || 0, minFrames: +$('f-minframes').value || 1,
  });
  const FILTER_IDS = ['f-rx', 'f-bright', 'f-thermal', 'f-rejected', 'f-overlays', 'f-minscore', 'f-minframes'];
  try {  // remember filter settings per browser
    const saved = JSON.parse(localStorage.getItem('ps-filters') || '{}');
    FILTER_IDS.forEach(id => { if (id in saved) { const el = $(id); el.type === 'checkbox' ? el.checked = saved[id] : el.value = saved[id]; } });
  } catch (e) { /* storage unavailable */ }
  FILTER_IDS.forEach(id => $(id).addEventListener(id.startsWith('f-min') ? 'input' : 'change', () => {
    try { localStorage.setItem('ps-filters', JSON.stringify(Object.fromEntries(FILTER_IDS.map(i => [i, $(i).type === 'checkbox' ? $(i).checked : $(i).value])))); } catch (e) {}
    render();
  }));

  async function refresh() {
    const [summary, overlays, images, cands] = await Promise.all([
      fetch('api/summary').then(r => r.json()),
      fetch('api/overlays').then(r => r.json()),
      fetch('api/images').then(r => r.json()),
      fetch('api/groups').then(r => r.json()),
    ]);
    $('mission').textContent = summary.mission || '';
    $('empty').hidden = summary.images > 0 || importState.busy;
    const c = summary.candidates || {};
    const g = summary.groups || {};
    $('stats').textContent = `${summary.images_ok} images · ${summary.n_groups || 0} objects (${g.pending || 0} pending · ${g.accepted || 0} accepted · ${g.followup || 0} follow-up · ${g.rejected || 0} rejected) from ${Object.values(c).reduce((a, b) => a + b, 0)} sightings`;

    overlays.forEach(o => {
      if (!state.overlays.has(o.id)) {
        const layer = L.imageOverlay('files/' + o.png, o.bounds, { opacity: 0.95, interactive: false });
        state.overlays.set(o.id, layer);
        overlayGroup.addLayer(layer);
      }
    });
    footprintGroup.clearLayers();
    images.filter(i => i.footprint).forEach(i => {
      L.polygon(i.footprint, { color: '#5d7a96', weight: 1, fill: false, opacity: 0.6, interactive: false }).addTo(footprintGroup);
    });
    state.cands = cands;
    const pts = images.filter(i => i.lat).map(i => [i.lat, i.lon]);
    // fit once, and keep fitting while an import is still bringing images in
    if (pts.length && (!state.fitted || (importState.busy && pts.length !== state.fitN))) {
      map.fitBounds(L.latLngBounds(pts).pad(0.2)); state.fitted = true; state.fitN = pts.length;
    }
    render();
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
    f.overlays ? map.addLayer(overlayGroup) : map.removeLayer(overlayGroup);
    const list = $('list');
    list.innerHTML = '';
    markerGroup.clearLayers();
    state.markers.clear();
    const shown = state.cands.filter(visible);
    $('shown').textContent = `showing ${shown.length} of ${state.cands.length}`;
    shown.forEach(c => {
      const color = COLORS[c.detector] || '#fff';
      const m = L.circleMarker([c.lat, c.lon], {
        radius: c.status === 'accepted' ? 9 : c.status === 'followup' ? 8 : 6,
        color: c.status === 'accepted' ? '#3dc47e' : c.status === 'followup' ? '#4fc3f7' : color,
        weight: c.status === 'rejected' ? 1 : 2, fillColor: color, fillOpacity: c.status === 'rejected' ? 0.15 : 0.6,
      }).on('click', () => select(c.id, false));
      m.bindTooltip(`${c.detectors.join('+')} ${c.score.toFixed(1)} · ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''} · ${STATUS_LABEL[c.status] || c.status}`);
      markerGroup.addLayer(m);
      state.markers.set(c.id, m);

      const li = document.createElement('li');
      li.dataset.id = c.id;
      if (c.id === state.sel) li.classList.add('sel');
      li.innerHTML = `<img loading="lazy" src="files/${c.best.thumb}">
        <div><div class="title">${c.detectors.map(d => `<span class="badge ${d}">${d}</span>`).join(' ')} ${c.score.toFixed(1)}</div>
        <div class="sub">${c.area_m2 ?? '?'} m² · seen in ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''}${c.min_range_m != null ? ' · ' + Math.round(c.min_range_m) + ' m from aircraft' : ''}</div></div>
        <div class="st ${c.status}">${STATUS_LABEL[c.status] || c.status}</div>`;
      li.addEventListener('click', () => select(c.id, true));
      list.appendChild(li);
    });
    if (state.sel && !shown.some(c => c.id === state.sel)) state.sel = null;
    renderDetail();
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
    if (!c) { el.innerHTML = '<div class="hint">Select a candidate. Keys: <b>A</b> accept, <b>W</b> follow-up waypoint, <b>R</b> reject, <b>U</b> undo, <b>N</b>/<b>P</b> next/prev, <b>F</b> full image.</div>'; return; }
    const b = c.best;
    const temp = b.extra && b.extra.peak_temp_c != null ? ` · ${b.extra.peak_temp_c.toFixed(1)} °C` : '';
    const strip = c.thumbs.length > 1 ? `<div class="strip">${c.thumbs.slice(1).map(t => `<img src="files/${t}">`).join('')}</div>` : '';
    el.innerHTML = `<img src="files/${b.thumb}">${strip}
      <div class="meta"><span>${c.detectors.map(d => `<span class="badge ${d}">${d}</span>`).join(' ')} score ${c.score.toFixed(2)}${temp}</span>
      <span class="st ${c.status}">${STATUS_LABEL[c.status] || c.status}</span></div>
      <div class="meta"><span>${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}</span><span>${c.area_m2 ?? '?'} m² · ${c.n_sightings} sighting${c.n_sightings > 1 ? 's' : ''} in ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''}</span></div>
      <div class="meta"><span>${baseName(b.image_path)}</span><a class="btn" href="image/${b.image_id}" target="_blank">full image</a></div>
      <div class="actions"><button class="accept">Accept (A)</button><button class="followup" title="tag for a second look on another flight">Follow-up (W)</button><button class="reject">Reject (R)</button><button class="undo">Undo (U)</button></div>`;
    el.querySelector('.accept').onclick = () => decide('accepted');
    el.querySelector('.followup').onclick = () => decide('followup');
    el.querySelector('.reject').onclick = () => decide('rejected');
    el.querySelector('.undo').onclick = () => decide('pending');
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
    const shown = state.cands.filter(visible);
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
    if (e.target.tagName === 'INPUT' || $('mission-dlg').open || $('import-dlg').open) return;
    const k = e.key.toLowerCase();
    if (k === 'a') decide('accepted');
    else if (k === 'w') decide('followup');
    else if (k === 'r') decide('rejected');
    else if (k === 'u') decide('pending');
    else if (k === 'n' || k === 'j') step(1);
    else if (k === 'p' || k === 'k') step(-1);
    else if (k === 'f' && state.sel) { const c = state.cands.find(x => x.id === state.sel); window.open(`image/${c.best.image_id}`); }
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

  // ---- add images ---------------------------------------------------------
  const importState = { busy: false, timer: null, last: null };
  if (location.pathname.startsWith('/m/')) $('home-link').hidden = false;

  function showImport(st) {
    const busy = st.state === 'running' || st.state === 'watching';
    const wasBusy = importState.busy;
    importState.busy = busy;
    $('i-start').hidden = busy; $('i-stop').hidden = !busy;
    $('i-folder').disabled = $('i-watch').disabled = $('i-browse').disabled = busy;
    const chip = $('import-chip');
    chip.hidden = !busy;
    chip.textContent = st.state === 'watching' ? 'watching for images' : `importing ${st.done}/${st.total}`;
    let html = '';
    if (st.state === 'running') {
      html = `<progress max="${Math.max(1, st.total)}" value="${st.done}"></progress>
        <div>Processing ${st.done} of ${st.total}${st.current ? ' · ' + st.current : ''}</div>`;
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
      body: JSON.stringify({ folder, watch: $('i-watch').checked }) });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) { $('i-status').innerHTML = `<div class="err">${typeof b.detail === 'string' ? b.detail : 'Could not start the import.'}</div>`; return; }
    try { localStorage.setItem('ps-folder', folder); } catch (e) {}
    showImport(b);
  });
  $('i-stop').addEventListener('click', async () => { showImport(await fetch('api/ingest/stop', { method: 'POST' }).then(r => r.json())); });

  refresh().then(drawRoute);
  setInterval(refresh, 5000);
  pollImport();
  if (location.hash === '#import') openImport();
})();
