/* pointersearch start screen. */
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

  // suggest today's date as the start of the name
  const d = new Date();
  const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  $('new-name').placeholder = `e.g. ${today} Oaks Road`;

  let hosted = false;
  function hostedCard(m) {
    const routes = (m.route_list || []).map(r => {
      const sm = r.summary || {};
      const tip = [r.mode === 'area' ? 'search area' : 'line route', sm.duration_min != null ? `about ${sm.duration_min} min` : '', sm.height_m != null ? `${sm.height_m} m` : ''].filter(Boolean).join(', ');
      return `<a class="btn" href="${m.url}api/routes/${r.id}/kmz" download title="Download KMZ for DJI Pilot 2 (${tip})">⭳ ${esc(r.name)}</a>`;
    }).join('');
    const when = [m.modified ? `last changed ${m.modified.replace('T', ' ')}` : '', m.created_by ? `created by ${esc(m.created_by)}` : ''].filter(Boolean).join(' · ');
    return `<div class="card mission">
      <div class="m-main">
        <div class="m-name">${esc(m.name)}</div>
        <div class="m-sub">${plural(m.routes || 0, 'saved route')}</div>
        ${routes ? `<div class="m-routes">${routes}</div>` : ''}
        <div class="m-when">${when}</div>
      </div>
      <div class="m-actions"><a class="btn primary" href="${m.url}plan">Open planner</a></div>
    </div>`;
  }

  function card(m) {
    if (hosted) return hostedCard(m);
    const o = m.objects || {};
    const bits = [plural(m.routes || 0, 'saved route'), plural(m.images, 'image')];
    if (m.n_objects) {
      const parts = [`${o.pending || 0} to review`];
      if (o.accepted) parts.push(`${o.accepted} accepted`);
      if (o.followup) parts.push(`${o.followup} follow-up`);
      bits.push(`${plural(m.n_objects, 'object')} (${parts.join(', ')})`);
    }
    const busy = m.importing ? `<span class="chip">${m.importing === 'watching' ? 'watching for images' : 'importing images'}</span>` : '';
    const when = m.modified ? `last changed ${m.modified.replace('T', ' ')}` : '';
    return `<div class="card mission">
      <div class="m-main">
        <div class="m-name">${esc(m.name)} ${busy}</div>
        <div class="m-sub">${bits.join(' · ')}</div>
        <div class="m-when">${when}</div>
      </div>
      <div class="m-actions">
        <a class="btn" href="${m.url}plan">Plan route</a>
        <a class="btn" href="${m.url}#import">Add images</a>
        <a class="btn ${m.images ? 'primary' : ''}" href="${m.url}">Review map</a>
      </div>
    </div>`;
  }

  async function refresh() {
    try {
      const r = await fetch('/api/missions').then(r => r.json());
      $('root').textContent = r.root ? `Missions folder: ${r.root}` : '';
      $('list').innerHTML = r.missions.length ? r.missions.map(card).join('')
        : '<div class="hint">No missions yet. Create one above to start planning a route.</div>';
    } catch (e) {
      $('list').innerHTML = '<div class="err">Cannot reach pointersearch. Is it still running?</div>';
    }
  }

  $('new-form').addEventListener('submit', async e => {
    e.preventDefault();
    $('new-err').textContent = '';
    const name = $('new-name').value.trim();
    if (!name) { $('new-err').textContent = 'Give the mission a name.'; $('new-name').focus(); return; }
    const r = await fetch('/api/missions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { $('new-err').textContent = typeof body.detail === 'string' ? body.detail : 'Could not create the mission.'; return; }
    location.href = body.url + 'plan';       // a new mission starts in the route planner
  });

  fetch('/api/app').then(r => r.json()).then(a => {
    hosted = !!a.hosted;
    if (hosted) {
      psUserMenu(a.user);
      $('new-hint').textContent = 'A mission holds the planned routes for one search. Plan here, download the KMZ for the controller, and process the images on the laptop app.';
    }
  }).catch(() => {}).finally(() => { refresh(); setInterval(refresh, hosted ? 15000 : 5000); });
  $('new-name').focus();
})();
