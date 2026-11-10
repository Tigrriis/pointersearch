/* Shared by pages that also run in the hosted planner: if the session has ended,
   go to the sign-in page and come back afterwards. Does nothing in the laptop app. */
(function () {
  const raw = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const r = await raw(...args);
    if (r.status === 401 && !location.pathname.startsWith('/login')) {
      location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
      return new Promise(() => {});            // navigation is under way
    }
    if (r.status === 403) {
      const b = await r.clone().json().catch(() => ({}));
      if (b.code === 'password_change_required') { location.href = '/account?first=1'; return new Promise(() => {}); }
    }
    return r;
  };
  window.psEsc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // fills <span id="user-menu"> on hosted pages
  window.psUserMenu = user => {
    const el = document.getElementById('user-menu');
    if (!el || !user) return;
    el.innerHTML = `<span class="who">${psEsc(user.name)}</span>`
      + (user.role === 'admin' ? '<a class="btn" href="/users">People</a>' : '')
      + '<a class="btn" href="/account">Account</a><button class="btn" id="signout">Sign out</button>';
    document.getElementById('signout').onclick = async () => { await raw('/api/logout', { method: 'POST' }); location.href = '/login'; };
  };
})();
