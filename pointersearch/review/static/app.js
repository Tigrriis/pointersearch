/* pointersearch review UI. Plain JS, no build step, fully offline. */
(function () {
  const COLORS = { rx: '#f06292', bright: '#ffb74d', thermal: '#ef5350' };
  const state = { cands: [], markers: new Map(), overlays: new Map(), sel: null, lastId: 0, fitted: false };

  const map = L.map('map', { zoomControl: true, maxZoom: 24 });
  map.setView([0, 0], 2);
  L.control.scale({ imperial: false }).addTo(map);
  const overlayGroup = L.layerGroup().addTo(map);
  const markerGroup = L.layerGroup().addTo(map);
  const footprintGroup = L.layerGroup().addTo(map);

  fetch('/api/summary').then(r => r.json()).then(s => {
    if (s.tiles) L.tileLayer('/tiles/{z}/{x}/{y}.png', { maxNativeZoom: 19, maxZoom: 24 }).addTo(map);
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
      fetch('/api/summary').then(r => r.json()),
      fetch('/api/overlays').then(r => r.json()),
      fetch('/api/images').then(r => r.json()),
      fetch('/api/groups').then(r => r.json()),
    ]);
    $('mission').textContent = summary.mission || '';
    const c = summary.candidates || {};
    const g = summary.groups || {};
    $('stats').textContent = `${summary.images_ok} images · ${summary.n_groups || 0} objects (${g.pending || 0} pending · ${g.accepted || 0} accepted · ${g.rejected || 0} rejected) from ${Object.values(c).reduce((a, b) => a + b, 0)} sightings`;

    overlays.forEach(o => {
      if (!state.overlays.has(o.id)) {
        const layer = L.imageOverlay('/files/' + o.png, o.bounds, { opacity: 0.95, interactive: false });
        state.overlays.set(o.id, layer);
        overlayGroup.addLayer(layer);
      }
    });
    footprintGroup.clearLayers();
    images.filter(i => i.footprint).forEach(i => {
      L.polygon(i.footprint, { color: '#5d7a96', weight: 1, fill: false, opacity: 0.6, interactive: false }).addTo(footprintGroup);
    });
    state.cands = cands;
    if (!state.fitted) {
      const pts = images.filter(i => i.lat).map(i => [i.lat, i.lon]);
      if (pts.length) { map.fitBounds(L.latLngBounds(pts).pad(0.2)); state.fitted = true; }
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
        radius: c.status === 'accepted' ? 9 : 6, color: c.status === 'accepted' ? '#3dc47e' : color,
        weight: c.status === 'rejected' ? 1 : 2, fillColor: color, fillOpacity: c.status === 'rejected' ? 0.15 : 0.6,
      }).on('click', () => select(c.id, false));
      m.bindTooltip(`${c.detectors.join('+')} ${c.score.toFixed(1)} · ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''} · ${c.status}`);
      markerGroup.addLayer(m);
      state.markers.set(c.id, m);

      const li = document.createElement('li');
      li.dataset.id = c.id;
      if (c.id === state.sel) li.classList.add('sel');
      li.innerHTML = `<img loading="lazy" src="/files/${c.best.thumb}">
        <div><div class="title">${c.detectors.map(d => `<span class="badge ${d}">${d}</span>`).join(' ')} ${c.score.toFixed(1)}</div>
        <div class="sub">${c.area_m2 ?? '?'} m² · seen in ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''}${c.min_range_m != null ? ' · ' + Math.round(c.min_range_m) + ' m from aircraft' : ''}</div></div>
        <div class="st ${c.status}">${c.status}</div>`;
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
    if (!c) { el.innerHTML = '<div class="hint">Select a candidate. Keys: <b>A</b> accept, <b>R</b> reject, <b>U</b> undo, <b>N</b>/<b>P</b> next/prev, <b>F</b> full image.</div>'; return; }
    const b = c.best;
    const temp = b.extra && b.extra.peak_temp_c != null ? ` · ${b.extra.peak_temp_c.toFixed(1)} °C` : '';
    const strip = c.thumbs.length > 1 ? `<div class="strip">${c.thumbs.slice(1).map(t => `<img src="/files/${t}">`).join('')}</div>` : '';
    el.innerHTML = `<img src="/files/${b.thumb}">${strip}
      <div class="meta"><span>${c.detectors.map(d => `<span class="badge ${d}">${d}</span>`).join(' ')} score ${c.score.toFixed(2)}${temp}</span>
      <span class="st ${c.status}">${c.status}</span></div>
      <div class="meta"><span>${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}</span><span>${c.area_m2 ?? '?'} m² · ${c.n_sightings} sighting${c.n_sightings > 1 ? 's' : ''} in ${c.n_frames} frame${c.n_frames > 1 ? 's' : ''}</span></div>
      <div class="meta"><span>${baseName(b.image_path)}</span><a class="btn" href="/image/${b.image_id}" target="_blank">full image</a></div>
      <div class="actions"><button class="accept">Accept (A)</button><button class="reject">Reject (R)</button><button class="undo">Undo (U)</button></div>`;
    el.querySelector('.accept').onclick = () => decide('accepted');
    el.querySelector('.reject').onclick = () => decide('rejected');
    el.querySelector('.undo').onclick = () => decide('pending');
  }

  async function decide(status) {
    if (!state.sel) return;
    const id = state.sel;
    const updated = await fetch(`/api/groups/${id}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }),
    }).then(r => r.json());
    const i = state.cands.findIndex(x => x.id === id);
    if (i >= 0) state.cands[i] = updated;
    if (status !== 'pending') step(1, true);
    render();
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
    if (e.target.tagName === 'INPUT') return;
    const k = e.key.toLowerCase();
    if (k === 'a') decide('accepted');
    else if (k === 'r') decide('rejected');
    else if (k === 'u') decide('pending');
    else if (k === 'n' || k === 'j') step(1);
    else if (k === 'p' || k === 'k') step(-1);
    else if (k === 'f' && state.sel) { const c = state.cands.find(x => x.id === state.sel); window.open(`/image/${c.best.image_id}`); }
  });

  refresh();
  setInterval(refresh, 5000);
})();
