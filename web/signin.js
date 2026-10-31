// The Quick connect window's Composio step. Composio's sign-in page never sends the window back, so it opens in a
// small window of its own; once the key has landed here, that window closes and this one moves on to the next sign-in.
(() => {
  const box = document.getElementById('signin');
  if (!box) return;
  const target = box.dataset.target, flow = box.dataset.flow;
  const go = document.getElementById('signin-go'), note = document.getElementById('signin-note');
  let win = null, busy = false;
  go.addEventListener('click', e => {
    e.preventDefault();
    win = window.open(target, 'composio-signin', 'popup,width=520,height=760');
    if (!win) { location.href = target; return; } // no second window allowed: use this one (Wizard still finishes)
    go.textContent = 'Show the Composio window';
    note.textContent = 'Sign in or create a free account there. This page moves on by itself.';
  });
  async function check() {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch('../../api/quick/flow?flow=' + encodeURIComponent(flow), { credentials: 'same-origin', cache: 'no-store' });
      const v = await r.json().catch(() => ({}));
      if (r.ok && (v.state !== 'running' || v.where !== 'composio')) {
        try { if (win && !win.closed) win.close(); } catch (_) { /* already gone */ }
        location.replace(location.href);
        return;
      }
      if (!r.ok && r.status !== 429) { note.textContent = (v.error && v.error.message) || 'This sign-in ended. Start again from Wizard.'; return; }
    } catch (_) { /* offline for a moment */ } finally { busy = false; }
    setTimeout(check, 2000);
  }
  setTimeout(check, 2000);
})();
