/* Microsoft for Wizard: connect Outlook mail and calendar, then switch on what your agent may use.
 * Connector pattern (APP-DESIGN-RULES rule 5): status card, accounts, one primary action, details collapsed.
 * Two ways to connect: Quick connect (Composio) or your own Microsoft Entra app (guided steps).
 * Plain DOM, no framework. Back: an open sheet closes first (wizard-sheet.js), then a nested page; the shell owns the root. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const view = $('view'), sheet = $('sheet'), status = $('status');
  let csrf = null, home = null, stack = [], polling = null, seq = 0, busy = false;
  const CHECK = '<i><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg></i>';

  // ---------- small DOM helpers ----------
  function el(tag, text, cls, attrs) {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  function button(text, onclick, cls = 'wz-button', attrs) { const b = el('button', text, cls, { type: 'button', ...attrs }); b.onclick = onclick; return b; }
  function link(text, href, cls = 'wz-button secondary') { return el('a', text, cls, { href, target: '_blank', rel: 'noopener noreferrer' }); }
  function row(title, sub, onclick, tile, trailing) {
    const r = el(onclick ? 'button' : 'div', null, 'wz-row', onclick ? { type: 'button' } : {});
    if (onclick) r.onclick = onclick;
    if (tile != null) r.append(typeof tile === 'string' ? el('span', tile, 'wz-tile', { 'aria-hidden': 'true' }) : tile);
    const main = el('span', null, 'wz-main'); main.append(el('b', title)); if (sub) main.append(el('small', sub)); r.append(main);
    if (trailing) r.append(trailing); else if (onclick) r.append(el('span', '›', 'chev', { 'aria-hidden': 'true' }));
    return r;
  }
  function pill(kind, text) { return el('span', text, `wz-pill ${kind}`); }
  function say(text, note) { status.textContent = text || ''; status.classList.toggle('note', !!note); if (note && text) setTimeout(() => { if (status.textContent === text) say(''); }, 3500); }
  function toggle(on, title, sub, onclick) {
    const t = el('button', null, 'wz-row toggle', { type: 'button', 'aria-pressed': String(on) });
    const check = el('span', null, 'wz-check', { 'aria-hidden': 'true' }); check.innerHTML = CHECK;
    const main = el('span', null, 'wz-main'); main.append(el('b', title)); if (sub) main.append(el('small', sub));
    t.append(check, main); t.onclick = onclick;
    return t;
  }
  function copyRow(label, value) {
    const wrap = el('div', null, 'copy');
    wrap.append(el('small', label), el('code', value || '—'));
    wrap.append(button('Copy', async () => {
      try { await navigator.clipboard.writeText(value); say('Copied.', true); }
      catch { const r = document.createRange(); r.selectNodeContents(wrap.querySelector('code')); getSelection().removeAllRanges(); getSelection().addRange(r); say('Selected. Copy it with your keyboard.', true); }
    }, 'wz-button secondary small'));
    return wrap;
  }
  const initial = text => (text || '?').trim().charAt(0).toUpperCase();
  function actionsOf(...buttons) { const a = el('div', null, 'actions'); a.append(...buttons); return a; }
  function title(text) { return el('h3', text, 'sec-title'); }
  function note(text) { return el('p', text, 'sec-note'); }

  // ---------- API ----------
  async function api(path, body) {
    const init = body === undefined ? { credentials: 'same-origin' }
      : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) };
    const r = await fetch(path, init);
    const value = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(value.error?.message || 'Something went wrong.'); Object.assign(e, value.error || {}); throw e; }
    return value;
  }
  async function refresh() { home = await api('api/home'); render(); }

  // ---------- navigation and Back (domain only; the shell owns root/exit) ----------
  function go(next) { stack.push(next); sheetClose(); say(''); render(); view.scrollTop = 0; }
  function back() { if (!stack.length) return false; stack.pop(); render(); return true; }
  if (window.WizardApp?.version === 1) {
    WizardApp.ready({ name: 'wizard-microsoft', version: '0.2.6' });
    WizardApp.back.register({ canGoBack: () => stack.length > 0, back });
  }

  function render() {
    if (!home) return;
    const top = stack[stack.length - 1];
    view.replaceChildren();
    if (!top) return renderHome();
    if (top.kind === 'quick') return renderQuick();
    if (top.kind === 'own') return renderOwn();
  }
  function crumb(text) {
    const c = el('div', null, 'crumb');
    c.append(button('‹', back, 'back', { 'aria-label': 'Back' }), el('h2', text));
    return c;
  }

  // ---------- home ----------
  function overall() {
    const active = home.accounts.filter(a => a.status === 'active');
    if (!home.accounts.length) return ['off', 'Not connected'];
    if (active.length < home.accounts.length) return ['warn', 'Needs attention'];
    return ['ok', 'Connected'];
  }
  function renderHome() {
    const [kind, word] = overall();
    const card = el('div', null, 'wz-conn');
    const tile = el('span', null, 'wz-tile mark', { 'aria-hidden': 'true' });
    tile.innerHTML = '<svg viewBox="0 0 24 24"><rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M4.5 7.5l7.5 5.5 7.5-5.5"/></svg>';
    const main = el('span', null, 'wz-main');
    const head = el('span', null, 'conn-head'); head.append(el('b', 'Microsoft'), pill(kind, word));
    const on = new Set(home.accounts.flatMap(a => a.services.filter(s => s.on).map(s => s.label)));
    main.append(head, el('small', home.accounts.length
      ? (on.size ? `Your agent can use ${[...on].join(' and ')} on ${home.accounts.length === 1 ? 'your account' : `${home.accounts.length} accounts`}.` : 'Switch on what your agent may use.')
      : 'Connect Outlook once. Then choose what your agent may use: Mail and Calendar.'));
    card.append(tile, main);
    const top = el('section', null, 'wz-sec'); top.append(card); view.append(top);

    if (home.accounts.length) {
      const grid = el('div', null, 'wz-grid accounts');
      for (const a of home.accounts) grid.append(accountCard(a));
      const sec = el('section', null, 'wz-sec'); sec.append(title('Accounts'), grid); view.append(sec);
    }
    const acts = el('div', null, 'primary');
    acts.append(button(home.accounts.length ? 'Add another Microsoft account' : 'Connect Microsoft', () => waySheet()));
    view.append(acts);

    const more = el('details', null, 'wz-more');
    more.append(el('summary', 'More'));
    const list = el('div', null, 'wz-list');
    list.append(row('Quick connect', home.quick.keys.length ? `Through Composio · ${home.quick.keys.length === 1 ? '1 key' : `${home.quick.keys.length} keys`}` : 'Through Composio · not set up', () => go({ kind: 'quick' }), '✦'));
    list.append(row('Your own Microsoft app', home.own.client ? `Set up · ${home.own.client.client_id.slice(0, 8)}…` : 'Not set up', () => go({ kind: 'own' }), '⚙'));
    more.append(list);
    if (home.audit.length) {
      const log = el('div', null, 'wz-list');
      for (const item of home.audit.slice(0, 12)) log.append(row(item.operation, `${new Date(item.at * 1000).toLocaleString()} · ${item.outcome}`));
      more.append(el('h3', 'Recent use', 'sec-title'), log);
    }
    more.append(el('p', 'Your agent and apps get mail and calendar tools from this app. They never see a password, key or token.', 'muted pad'));
    const sec = el('section', null, 'wz-sec'); sec.append(more); view.append(sec);
  }
  function accountCard(a) {
    const box = el('div', null, 'wz-list account');
    const how = a.via === 'composio' ? 'Quick connect' : 'Your own app';
    box.append(row(a.label || a.email || 'Microsoft account', `${a.label ? a.email + ' · ' : ''}${how}`, () => accountSheet(a), el('span', initial(a.label || a.email), 'wz-tile', { 'aria-hidden': 'true' }),
      a.status === 'active' ? pill('ok', 'Connected') : pill('warn', 'Sign in again')));
    if (a.status !== 'active') {
      box.append(row('This sign-in ended', 'Sign in again to keep things working.', () => reconnect(a), '!'));
      return box;
    }
    for (const s of a.services) {
      box.append(toggle(s.on, s.label, s.on ? s.what : 'Off', async e => {
        const t = e.currentTarget; if (busy) return; busy = true; t.disabled = true;
        try { await api(`api/accounts/${a.id}/service`, { service: s.id, on: !s.on }); await refresh(); say(`${s.label} ${s.on ? 'off' : 'on'} for ${a.label || a.email}.`, true); }
        catch (err) { say(err.message); t.disabled = false; } finally { busy = false; }
      }));
    }
    return box;
  }
  function reconnect(a) { if (a.via === 'composio') startQuick(a); else startOwn(a); }
  function accountSheet(a) {
    const input = el('input', null, 'wz-field', { value: a.label || '', placeholder: 'Work, Personal…', 'aria-label': 'Name', maxlength: '40', enterkeyhint: 'done' });
    const form = el('form', null, 'label-form'); form.append(input);
    const save = async () => { try { await api(`api/accounts/${a.id}/label`, { label: input.value }); sheetClose(); await refresh(); say('Saved.', true); } catch (e) { say(e.message); } };
    form.onsubmit = e => { e.preventDefault(); save(); };
    const chips = el('div', null, 'chips');
    for (const name of ['Work', 'Personal']) chips.append(button(name, () => { input.value = name; save(); }, 'wz-chip'));
    openSheet(a.email || 'Microsoft account', el('p', `${a.via === 'composio' ? 'Connected through Composio (Quick connect).' : 'Connected with your own Microsoft app.'} Name it so you and your agent can tell accounts apart.`, 'muted'),
      form, chips,
      actionsOf(button('Save name', save, 'wz-button secondary'), button('Sign in again', () => reconnect(a), 'wz-button secondary'),
        button('Disconnect', () => confirmDisconnect(a), 'wz-button secondary danger')));
  }
  function confirmDisconnect(a) {
    openSheet(`Disconnect ${a.email || 'this account'}?`,
      el('p', a.via === 'composio' ? 'Wizard forgets this account and asks Composio to remove the sign-in.' : 'Wizard forgets this sign-in. Microsoft keeps your permission until you remove it on its page.', 'muted'),
      actionsOf(button('Disconnect', async () => {
        try {
          const out = await api(`api/accounts/${a.id}/disconnect`, {}); sheetClose(); await refresh();
          if (out.manual_url) openSheet('One more step', el('p', 'To end access completely, remove Wizard (or Composio) on Microsoft\'s page.', 'muted'),
            actionsOf(link('Open Microsoft ↗', out.manual_url, 'wz-button'), button('Done', sheetClose, 'wz-button secondary')));
          else say('Disconnected.', true);
        } catch (e) { say(e.message); }
      }, 'wz-button danger'), button('Cancel', sheetClose, 'wz-button secondary')));
  }
  function waySheet() {
    const list = el('div', null, 'wz-list');
    list.append(row('Quick connect', 'About a minute. Mail and Calendar, through Composio.', () => { sheetClose(); go({ kind: 'quick' }); }, '✦', home.quick.ready ? pill('ok', 'Ready') : null));
    list.append(row('Your own Microsoft app', home.own.client ? 'Ready. Sign in with your app.' : 'About ten minutes in Microsoft Entra. Nobody else is involved.',
      () => { sheetClose(); if (home.own.client) startOwn(null); else go({ kind: 'own' }); }, '⚙', home.own.client ? pill('ok', 'Ready') : null));
    openSheet(home.accounts.length ? 'Add a Microsoft account' : 'Connect Microsoft', list, el('p', 'You can use both, and switch any time.', 'muted'));
  }

  // ---------- Quick connect (Composio) ----------
  function renderQuick() {
    const q = home.quick;
    view.append(crumb('Quick connect'));
    view.append(el('p', q.note, 'quick-note'));
    const keys = el('section', null, 'wz-sec');
    keys.append(title(q.keys.length ? 'Your Composio key' + (q.keys.length > 1 ? 's' : '') : 'Composio account'));
    if (!q.keys.length) {
      keys.append(note('Quick connect asks you to sign in to Composio (or create a free account) in the same window. The key is saved by itself.'));
      const acts = el('div', null, 'actions pad');
      acts.append(button('Paste a key instead', () => openSheet('Paste a Composio key', el('p', q.key_help, 'muted'), keyForm(true)), 'wz-button secondary'));
      keys.append(acts);
    } else {
      const list = el('div', null, 'wz-list');
      for (const k of q.keys) list.append(keyRow(k));
      const acts = el('div', null, 'actions pad');
      acts.append(button('Add another key', () => openSheet('Add a Composio key', el('p', `Several keys are fine, for example a second free account. ${q.key_help}`, 'muted'), keyForm(true)), 'wz-button secondary'));
      keys.append(list, acts);
    }
    view.append(keys);
    const connect = el('section', null, 'wz-sec');
    connect.append(title('Connect'), note('One window: sign in to Microsoft (and Composio the first time). One sign-in covers Mail and Calendar.'));
    const acts = el('div', null, 'actions pad'); acts.append(button('Quick connect', () => startQuick(null), 'wz-button'));
    connect.append(acts);
    const mine = home.accounts.filter(a => a.via === 'composio');
    if (mine.length) connect.append(note(`Connected: ${mine.map(a => a.email || 'Microsoft account').join(', ')}.`));
    view.append(connect);
  }
  function keyRow(k) {
    const r = el('div', null, 'key-row');
    r.append(el('span', initial(k.label), 'wz-tile', { 'aria-hidden': 'true' }));
    const main = el('span', null, 'wz-main');
    main.append(el('b', `${k.label} · ${k.hint}`));
    const used = `${k.calls_this_month.toLocaleString()} calls this month (about ${k.free_calls.toLocaleString()} free)`;
    main.append(el('small', k.status === 'ok' ? used : `${k.error || 'Not checked yet'} · ${used}`));
    r.append(main, pill(k.status === 'ok' ? 'ok' : (k.status === 'unchecked' ? 'off' : 'warn'), k.status === 'ok' ? 'Working' : (k.status === 'unchecked' ? 'Not checked' : 'Not working')));
    const rm = button('Remove', async () => {
      if (rm.dataset.armed !== '1') { rm.dataset.armed = '1'; rm.textContent = 'Remove?'; return; }
      try { await api(`api/quick/keys/${k.id}/remove`, {}); await refresh(); say('Key removed.', true); } catch (e) { say(e.message); rm.dataset.armed = ''; rm.textContent = 'Remove'; }
    }, 'wz-button secondary small rm');
    r.append(rm);
    return r;
  }
  function keyForm(inSheet) {
    const form = el('form', null, 'key-form', { autocomplete: 'off' });
    const id = inSheet ? 'quick-key-2' : 'quick-key';
    const input = el('input', null, 'wz-field', { id, type: 'password', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', 'aria-describedby': id + '-msg' });
    const msg = el('p', 'Saved on this box only. Wizard checks it with Composio first.', 'hint', { id: id + '-msg' });
    const save = el('button', 'Save key', 'wz-button', { type: 'submit' });
    form.onsubmit = async e => {
      e.preventDefault(); save.disabled = true; input.removeAttribute('aria-invalid'); msg.classList.remove('bad'); msg.textContent = 'Checking with Composio…';
      try { await api('api/quick/keys', { key: input.value.trim() }); input.value = ''; if (inSheet) sheetClose(); await refresh(); say('Composio accepted the key.', true); }
      catch (err) { input.setAttribute('aria-invalid', 'true'); msg.classList.add('bad'); msg.textContent = err.message; }
      finally { save.disabled = false; }
    };
    form.append(el('label', 'Composio API key', null, { for: id }), input, msg, actionsOf(save));
    return form;
  }
  // One popup does it all: Composio sign-in when no key works yet, then the Microsoft sign-in (contract doc, "Connecting from another app").
  async function startQuick(account) {
    stopPolling();
    // Inside Wizard, a window for this address would open as a Wizard sheet, where Microsoft refuses to sign in. The window
    // goes to the box's other address instead (out.go_out), which Wizard hands to the browser. On its own, a blank tab
    // opened inside the tap keeps the popup allowed while the sign-in is prepared.
    const framed = window.top !== window;
    const tab = framed ? null : window.open('', '_blank');
    try {
      const out = await api('api/quick/flow', { account: account ? account.id : null });
      const go = new URL((framed && out.go_out) || out.go, location.href).href;
      if (tab) { tab.opener = null; tab.location.href = go; }
      flowSheet(out.flow, go, tab, null, out);
      pollFlow(out.flow, go, tab, out);
    } catch (e) { if (tab) tab.close(); sheetClose(); say(e.message); }
  }
  function flowSheet(flow, go, tab, v, out) {
    const first = !tab && !v;
    const label = v && v.label ? v.label : 'Microsoft';
    const where = v ? v.where : (out && out.steps && out.steps[0] && out.steps[0].id);
    const text = first ? 'Open the sign-in window to continue. It closes by itself when you are done.'
      : where === 'composio' ? 'In the new window, sign in to Composio or create a free account. Your key is saved by itself, then Microsoft asks for access.'
        : `In the new window, choose your Microsoft account and allow access for ${label}. This page updates by itself.`;
    const again = link(first ? 'Open the sign-in window ↗' : 'Open the window again ↗', go, first ? 'wz-button' : 'wz-button secondary');
    again.addEventListener('click', e => { if (!framed()) { e.preventDefault(); const t = window.open(go, '_blank'); if (t) { t.opener = null; pollFlow(flow, go, t, out); } } });
    openSheet(first ? 'Connect Microsoft' : 'Finish in the new window', el('p', text, 'muted'), '', first ? '' : el('div', null, 'spinner', { 'aria-hidden': 'true' }),
      actionsOf(again, button('Cancel', () => { stopPolling(); api('api/quick/flow/cancel', { flow }).catch(() => {}); if (tab && !tab.closed) tab.close(); sheetClose(); }, 'wz-button secondary')));
  }
  function framed() { return window.top !== window; }
  function pollFlow(flow, go, tab, out) {
    stopPolling();
    const mine = ++seq;
    let shown = '';
    polling = setInterval(async () => {
      try {
        const v = await api('api/quick/flow?flow=' + encodeURIComponent(flow));
        if (mine !== seq) return;
        if (v.state === 'done') { stopPolling(); if (tab && !tab.closed) tab.close(); sheetClose(); await refresh(); say(`Connected ${(v.result && v.result.email) || 'your account'}.`, true); return; }
        if (v.state !== 'running') { stopPolling(); if (tab && !tab.closed) tab.close(); sheetClose(); if (v.error) say(v.error.message); await refresh(); return; }
        // The window itself moves from Composio to the next sign-in (signin.js); here the sheet only follows along.
        const sig = v.step + v.where;
        if (sig !== shown) { shown = sig; flowSheet(flow, go, tab, v, out); }
      } catch { /* keep waiting */ }
    }, 1500);
  }

  // ---------- your own Microsoft app ----------
  function renderOwn() {
    const own = home.own;
    view.append(crumb('Your own Microsoft app'));
    view.append(el('p', 'About ten minutes in Microsoft Entra. Your sign-ins stay on this box and nobody else is involved. Steps marked Microsoft open in a new tab; come back here after each one.', 'intro'));
    const steps = el('ol', null, 'steps');
    const current = own.steps.find(s => !s.done && s.where === 'wizard');
    own.steps.forEach((s, i) => steps.append(stepCard(s, i + 1, s === current || (!own.client && i === 0))));
    view.append(steps);
    if (own.client) {
      const foot = el('div', null, 'foot');
      foot.append(button('Use a different app', async () => { try { await api('api/own/client/forget', {}); await refresh(); } catch (e) { say(e.message); } }, 'wz-chip linkish'));
      view.append(foot);
    }
  }
  function stepCard(s, n, open) {
    const li = el('li', null, `step${s.done ? ' done' : ''}${open ? ' current' : ''}`);
    const head = el('button', null, 'step-head', { type: 'button', 'aria-expanded': String(open) });
    head.append(el('span', s.done ? '✓' : String(n), 'num', { 'aria-hidden': 'true' }), el('span', s.title, 'step-title'),
      el('span', s.where === 'microsoft' ? 'Microsoft' : 'Wizard', 'wz-chip where'));
    const body = el('div', null, 'step-body'); body.hidden = !open;
    head.onclick = () => { body.hidden = !body.hidden; head.setAttribute('aria-expanded', String(!body.hidden)); };
    const list = el('ul', null, 'detail'); for (const d of s.detail) list.append(el('li', d)); body.append(list);
    for (const c of s.copy || []) body.append(copyRow(c.label, c.value));
    if (s.id === 'paste') body.append(clientForm());
    const acts = el('div', null, 'actions');
    if (s.url) acts.append(link('Open Microsoft Entra ↗', s.url, 'wz-button secondary'));
    for (const l of s.links || []) acts.append(link(l.label + ' ↗', l.url, 'wz-chip linkish'));
    if (s.id === 'connect') acts.append(button(s.done ? 'Connect another account' : 'Connect Microsoft account', () => startOwn(null), s.done ? 'wz-button secondary' : 'wz-button', home.own.client ? {} : { disabled: '' }));
    if (acts.childElementCount) body.append(acts);
    li.append(head, body);
    return li;
  }
  function clientForm() {
    const form = el('form', null, 'key-form', { autocomplete: 'off' });
    const c = home.own.client;
    const idIn = el('input', null, 'wz-field', { id: 'client-id', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', placeholder: c ? c.client_id : '1a2b3c4d-…', inputmode: 'text' });
    const secretIn = el('input', null, 'wz-field', { id: 'client-secret', type: 'password', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', 'aria-describedby': 'client-msg' });
    const msg = el('p', c ? `Saved: ${c.client_id.slice(0, 8)}… · secret ${c.secret_hint}. Paste both again to replace them.` : 'Saved on this box only. Wizard checks them with Microsoft first.', 'hint', { id: 'client-msg' });
    const save = el('button', c ? 'Replace' : 'Check and save', 'wz-button', { type: 'submit' });
    form.onsubmit = async e => {
      e.preventDefault(); save.disabled = true; msg.classList.remove('bad'); msg.textContent = 'Checking with Microsoft…';
      for (const i of [idIn, secretIn]) i.removeAttribute('aria-invalid');
      try {
        const out = await api('api/own/client', { client_id: idIn.value.trim(), client_secret: secretIn.value.trim() });
        idIn.value = ''; secretIn.value = ''; await refresh();
        say(out.check === 'ok' ? 'Microsoft accepted the app.' : 'Saved. Microsoft could not confirm it yet; the first sign-in will check it.', true);
      } catch (err) {
        (err.code === 'client_id_invalid' || err.code === 'client_unknown' ? idIn : secretIn).setAttribute('aria-invalid', 'true');
        msg.classList.add('bad'); msg.textContent = err.message;
      } finally { save.disabled = false; }
    };
    form.append(el('label', 'Application (client) ID', null, { for: 'client-id' }), idIn, el('label', 'Client secret value', null, { for: 'client-secret' }), secretIn, msg, actionsOf(save));
    return form;
  }
  async function startOwn(account) {
    const tab = framed() ? null : window.open('', '_blank'); // inside Wizard the link below hands Microsoft to the browser
    try {
      const out = await api('api/own/start', { account: account ? account.id : null });
      if (tab) { tab.opener = null; tab.location.href = out.authorization_url; }
      waitSheet('Finish in the Microsoft tab', 'Choose your account and accept. When Microsoft sends you back, close that tab. This page updates by itself.', out.authorization_url, !!tab);
      poll('api/own/status');
    } catch (e) { if (tab) tab.close(); sheetClose(); say(e.message); }
  }

  // ---------- sheets and waiting ----------
  function openSheet(text, ...children) {
    sheet.replaceChildren(el('h2', text, null, { id: 'sheet-title' }), ...children);
    if (window.WzSheet) WzSheet.open(sheet); else if (!sheet.open) sheet.showModal();
  }
  function sheetClose() { if (sheet.open) sheet.close(); stopPolling(); }
  sheet.addEventListener('close', stopPolling);
  function waitSheet(text, body, url, opened) {
    openSheet(text, el('p', body, 'muted'), el('div', null, 'spinner', { 'aria-hidden': 'true' }),
      actionsOf(link(opened ? 'Open the page again ↗' : 'Open the page ↗', url, opened ? 'wz-button secondary' : 'wz-button'), button('Cancel', sheetClose, 'wz-button secondary')));
  }
  function poll(path) {
    stopPolling();
    const mine = ++seq;
    polling = setInterval(async () => {
      try {
        const s = await api(path);
        if (mine !== seq) return;
        if (s.status === 'connected') { stopPolling(); sheetClose(); stack = []; await refresh(); say(`Connected ${s.result.email || 'your account'}.`, true); }
        else if (s.status === 'failed') { stopPolling(); sheetClose(); say(s.result.message); }
      } catch { /* keep waiting */ }
    }, 1500);
  }
  function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }

  // ---------- start ----------
  addEventListener('focus', () => { if (csrf && !sheet.open) refresh().catch(() => {}); });
  fetch('api/session', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Wizard-Request': 'bootstrap' }, body: '{}' })
    .then(async r => { const v = await r.json(); if (!r.ok) throw new Error(v.error?.message || 'Unavailable.'); csrf = v.csrf; await refresh();
      // Deep links from Mail and Calendar: ?start=own opens your-own-app setup, ?start=quick the Quick connect page.
      const start = new URLSearchParams(location.search).get('start');
      if (start === 'own') go({ kind: 'own' }); else if (start === 'quick') go({ kind: 'quick' }); })
    .catch(e => { const box = el('div', null, 'wz-empty'); box.append(el('strong', 'Microsoft is not available'), el('p', e.message)); view.replaceChildren(box); });
})();
