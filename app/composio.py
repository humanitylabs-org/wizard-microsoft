"""Composio Quick connect: a small, reusable client for Wizard connector apps.

The same file ships in wizard-google and wizard-microsoft; other apps can copy it unchanged.
It only knows Composio's REST API v3.1 (https://docs.composio.dev/reference):

    x-api-key header               project API key the owner pastes in the app
    GET  /connected_accounts       cheap key check on Save (not a tool call)
    POST /tool_router/session      a session for one user id + one toolkit
    POST /tool_router/session/{id}/link
                                   Composio Connect Link -> redirect_url + connected_account_id
    GET  /connected_accounts/{id}  status: INITIALIZING / INITIATED / ACTIVE / FAILED / EXPIRED / REVOKED ...
    POST /connected_accounts/{id}/revoke, DELETE /connected_accounts/{id}
    POST /tools/execute/proxy      provider API request signed by Composio -> {status, data, headers}

Sign in to Composio from the app (no key to copy): the same flow as Composio's own `composio login` /
`composio dev init` (github.com/ComposioHQ/composio, ts/packages/cli/src/effects/browser-login.ts):

    POST /api/v3.1/cli/create-session {scope: 'project'}      no credential -> {id, code, expiresAt, status: pending}
    owner opens https://dashboard.composio.dev/?cliKey=<id>   signs in or signs up on Composio's own page
    GET  /api/v3.1/cli/get-session?id=<id>                    pending -> linked {api_key: uak_..., account}; 404 = expired
    GET  /api/v3.1/auth/session/info   (x-user-api-key)        project {nano_id, org.id} and, when present, api_key.key
    POST /api/v3/org/project/<project>/api_keys/create          (x-user-api-key, x-org-id, x-project-id) when info had no key

Only the resulting project key is stored; the user-level key is used once in memory and dropped.

Composio holds and refreshes the provider tokens (custody); this app never sees them. Keys are kept by
the app (encrypted store or 0600 file): this module works on a plain dict the app persists.
"""
import json
import sys
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://backend.composio.dev/api/v3.1'
API_HOST = 'backend.composio.dev'
# One place for the sign-up link, so a referral link can be dropped in later.
SIGNUP_URL = os.environ.get('COMPOSIO_SIGNUP_URL') or 'https://dashboard.composio.dev/login'
KEY_HELP = 'In Composio, open Settings, then Project settings, and copy the key under API keys.'
MAX_KEYS = 10
MANAGED_FREE_CALLS = 20_000  # Hobby plan: calls through Composio-managed sign-in apps per month (composio.dev/pricing)
RECHECK_SECONDS = 60
LINK_TTL = 30 * 60
KEY_SHAPE = re.compile(r'^[A-Za-z0-9_\-.]{12,200}$')
DONE = {'ACTIVE'}
ENDED = {'FAILED', 'EXPIRED', 'REVOKED', 'DELETED', 'DISABLED'}


class ComposioError(Exception):
    """code: key_invalid | key_forbidden | quota | unavailable | rejected | not_found | policy."""

    def __init__(self, code, message, status=502, upstream=None):
        super().__init__(code)
        self.code, self.message, self.status, self.upstream = code, message, status, upstream

    @property
    def next_key_may_help(self):
        return self.code in ('key_invalid', 'key_forbidden', 'quota')


def _error(status, payload):
    err = payload.get('error') if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {}
    slug = str(err.get('slug') or '')
    text = str(err.get('message') or '')[:240]
    if status == 401:
        return ComposioError('key_invalid', 'Composio does not accept this key. Copy it again from Composio.', 401, slug)
    if status == 403:
        return ComposioError('key_forbidden', 'This Composio key may not do this. Use a full-access project key.', 403, slug)
    if status in (402, 429):
        return ComposioError('quota', 'Composio says this account is out of free calls or busy. Try again later, or add another key.', 429, slug)
    if status == 404:
        return ComposioError('not_found', text or 'Composio does not know this connection.', 404, slug)
    if 400 <= status < 500:
        return ComposioError('rejected', text or 'Composio refused the request.', 400, slug)
    return ComposioError('unavailable', 'Composio is not answering right now. Try again in a minute.', 502, slug)


class HttpTransport:
    """urllib to backend.composio.dev only; no redirects, no proxies from the environment."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def __init__(self, timeout=30):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), self._NoRedirect())

    def __call__(self, method, url, key, body=None, headers=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.hostname != API_HOST:
            raise ComposioError('policy', 'Endpoint refused.', 500)
        extra = headers or {}
        headers = {'Accept': 'application/json', 'User-Agent': 'wizard-composio/1', **extra}
        if key:
            headers['x-api-key'] = key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read(8_000_001)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(65536)
            error.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ComposioError('unavailable', 'Could not reach Composio.', 502) from None
        if len(raw) > 8_000_000:
            raise ComposioError('unavailable', 'Composio returned too much data.', 502)
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        return status, payload


class Client:
    def __init__(self, key, transport=None):
        self.key = key
        self.transport = transport or HttpTransport()

    def _call(self, method, path, body=None, query=None):
        url = API + path + ('?' + urllib.parse.urlencode(query) if query else '')
        status, payload = self.transport(method, url, self.key, body)
        if status >= 400:
            raise _error(status, payload)
        return payload if isinstance(payload, dict) else {}

    def check(self):
        """Cheapest real call that proves the key: list at most one connection."""
        self._call('GET', '/connected_accounts', query={'limit': 1})
        return True

    def start_link(self, user_id, toolkit, callback_url=None):
        session = self._call('POST', '/tool_router/session', {'user_id': user_id, 'toolkits': {'enable': [toolkit]}})
        sid = str(session.get('session_id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_\-]{4,128}', sid):
            raise ComposioError('unavailable', 'Composio returned an unexpected answer.', 502)
        body = {'toolkit': toolkit}
        if callback_url:
            body['callback_url'] = callback_url
        link = self._call('POST', f'/tool_router/session/{sid}/link', body)
        url, account = link.get('redirect_url'), link.get('connected_account_id')
        if not (isinstance(url, str) and url.startswith('https://') and isinstance(account, str)
                and re.fullmatch(r'[A-Za-z0-9_\-]{4,128}', account)):
            raise ComposioError('unavailable', 'Composio returned an unexpected answer.', 502)
        return {'redirect_url': url, 'connected_account_id': account}

    def account(self, account_id):
        value = self._call('GET', f'/connected_accounts/{urllib.parse.quote(account_id, safe="")}')
        toolkit = value.get('toolkit') if isinstance(value.get('toolkit'), dict) else {}
        return {'id': value.get('id', account_id), 'status': str(value.get('status') or '').upper(),
                'toolkit': str(toolkit.get('slug') or '').lower(), 'reason': value.get('status_reason'),
                'scopes': [s for s in value.get('requested_scopes') or [] if isinstance(s, str)][:40]}

    def remove(self, account_id):
        """Revoke at the provider (best effort), then delete the connection at Composio."""
        quoted = urllib.parse.quote(account_id, safe='')
        revoked = True
        try:
            self._call('POST', f'/connected_accounts/{quoted}/revoke', {})
        except ComposioError:
            revoked = False
        try:
            self._call('DELETE', f'/connected_accounts/{quoted}')
        except ComposioError as error:
            if error.code != 'not_found':
                raise
        return {'revoked': revoked}

    def proxy(self, user_id, account_id, method, url, query=None, body=None, headers=None):
        """One provider API request through Composio. Returns (status, data); never a token."""
        payload = {'user_id': user_id, 'connected_account_id': account_id, 'endpoint': url, 'method': method}
        params = [{'name': k, 'value': str(v), 'type': 'header'} for k, v in (headers or {}).items()]
        params += [{'name': k, 'value': str(v), 'type': 'query'} for k, values in (query or {}).items() if values is not None
                   for v in (values if isinstance(values, (list, tuple)) else [values])]
        if params:
            payload['parameters'] = params
        if body is not None:
            payload['body'] = body
        value = self._call('POST', '/tools/execute/proxy', payload)
        status = value.get('status')
        if not isinstance(status, int):
            raise ComposioError('unavailable', 'Composio returned an unexpected answer.', 502)
        return status, value.get('data')


def month(stamp=None):
    return time.strftime('%Y-%m', time.gmtime(stamp if stamp is not None else time.time()))


class Keys:
    """Several Composio keys (owners stack free accounts). Works on `section`, a dict the app persists:

        {'keys': [{'id', 'key', 'label', 'added', 'status', 'error', 'checked', 'calls': {'YYYY-MM': n}}]}

    `save()` is called after every change. Secrets never leave through `public()`."""

    def __init__(self, section, save, transport=None, clock=time.time):
        self.section, self.save, self.transport, self.clock = section, save, transport, clock
        self.section.setdefault('keys', [])
        self.lock = threading.RLock()

    def client(self, key_id):
        item = self.get(key_id)
        return Client(item['key'], self.transport)

    def get(self, key_id):
        for item in self.section['keys']:
            if item['id'] == key_id:
                return item
        raise ComposioError('not_found', 'That Composio key was removed. Add it again, then reconnect.', 404)

    def add(self, key, label=''):
        key = (key or '').strip()
        if not KEY_SHAPE.match(key):
            raise ComposioError('key_invalid', 'That does not look like a Composio API key.', 400)
        with self.lock:
            if any(secrets.compare_digest(k['key'], key) for k in self.section['keys']):
                raise ComposioError('rejected', 'This key is already saved.', 409)
            if len(self.section['keys']) >= MAX_KEYS:
                raise ComposioError('rejected', f'At most {MAX_KEYS} keys.', 400)
        status, error = 'ok', None
        try:
            Client(key, self.transport).check()
        except ComposioError as failure:
            if failure.code in ('key_invalid', 'key_forbidden'):
                raise  # never save a key Composio refuses
            status, error = 'unchecked', failure.message  # Composio unreachable: keep it, check again later
        label = (label or '').strip()[:40] or f'Composio {len(self.section["keys"]) + 1}'
        item = {'id': 'ck-' + secrets.token_hex(6), 'key': key, 'label': label, 'added': int(self.clock()),
                'status': status, 'error': error, 'checked': int(self.clock()), 'calls': {}}
        with self.lock:
            self.section['keys'].append(item)
            self.save()
        return self.public_one(item)

    def remove(self, key_id):
        with self.lock:
            before = len(self.section['keys'])
            self.section['keys'] = [k for k in self.section['keys'] if k['id'] != key_id]
            if len(self.section['keys']) == before:
                raise ComposioError('not_found', 'Key not found.', 404)
            self.save()
        return {'removed': True}

    def mark(self, key_id, status, error=None):
        with self.lock:
            for item in self.section['keys']:
                if item['id'] == key_id:
                    item.update(status=status, error=error, checked=int(self.clock()))
                    self.save()

    def count(self, key_id, n=1):
        with self.lock:
            for item in self.section['keys']:
                if item['id'] == key_id:
                    calls = item.setdefault('calls', {})
                    this = month(self.clock())
                    calls[this] = calls.get(this, 0) + n
                    for old in sorted(calls)[:-3]:  # keep three months
                        calls.pop(old, None)
                    self.save()

    def recheck(self, force=False):
        """Keys that failed or were unchecked get one real call again (at most once a minute each):
        a key that was out of calls or saved while Composio was down recovers by itself."""
        for item in list(self.section['keys']):
            if item['status'] == 'ok' and not force:
                continue
            if not force and self.clock() - item.get('checked', 0) < RECHECK_SECONDS:
                continue
            try:
                Client(item['key'], self.transport).check()
                self.mark(item['id'], 'ok')
            except ComposioError as failure:
                self.mark(item['id'], 'unchecked' if failure.code == 'unavailable' else failure.code, failure.message)

    def pick(self, prefer=None):
        """The key for a new connection: the preferred one if it works, else the first that works."""
        self.recheck()
        keys = self.section['keys']
        if not keys:
            raise ComposioError('no_key', 'Add your Composio API key first.', 409)
        ordered = sorted(keys, key=lambda k: (k['id'] != prefer, k['status'] != 'ok'))
        for item in ordered:
            if item['status'] in ('ok', 'unchecked'):
                return item
        raise ComposioError(ordered[0]['status'] if ordered[0]['status'] in ('key_invalid', 'quota') else 'key_invalid',
                            ordered[0].get('error') or 'No Composio key works right now.', 409)

    def public_one(self, item):
        this = month(self.clock())
        return {'id': item['id'], 'label': item['label'], 'hint': '••••' + item['key'][-4:], 'status': item['status'],
                'error': item.get('error'), 'added': item.get('added'), 'checked': item.get('checked'),
                'calls_this_month': item.get('calls', {}).get(this, 0), 'free_calls': MANAGED_FREE_CALLS}

    def public(self):
        return [self.public_one(k) for k in self.section['keys']]


class Links:
    """Pending Connect Links, in memory (like an OAuth state): which key and user id made each one."""

    def __init__(self, clock=time.time):
        self.items, self.lock, self.clock, self.seq = {}, threading.Lock(), clock, 0

    def add(self, **attempt):
        attempt_id = 'cl-' + secrets.token_urlsafe(12)
        with self.lock:
            stamp = self.clock()
            self.items = {k: v for k, v in self.items.items() if v['expires'] > stamp}
            if len(self.items) >= 32:
                raise ComposioError('rejected', 'Too many sign-ins in progress.', 429)
            self.seq += 1
            self.items[attempt_id] = {**attempt, 'id': attempt_id, 'expires': stamp + LINK_TTL, 'state': 'pending', 'result': None, 'seq': self.seq}
        return attempt_id

    def get(self, attempt_id):
        item = self.items.get(attempt_id or '')
        if not item or item['expires'] <= self.clock():
            return None
        return item

    def latest(self, session):
        mine = [v for v in self.items.values() if v.get('session') == session and v['expires'] > self.clock()]
        return max(mine, key=lambda v: v['seq']) if mine else None


# ---------- Sign in to Composio from the app ----------

BACKEND = 'https://backend.composio.dev'
DASHBOARD_URL = os.environ.get('COMPOSIO_DASHBOARD_URL') or 'https://dashboard.composio.dev/'
SESSION_ID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
NANO_ID = re.compile(r'^[A-Za-z0-9_\-]{2,80}$')
KEY_NAMES = ('api_key', 'apiKey', 'key', 'token')


def _find_key(payload, exclude):
    """First project-key-looking string under a key-like name (the create-key answer is not documented)."""
    queue = [payload]
    while queue:
        current = queue.pop(0)
        if isinstance(current, list):
            queue.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for name, value in current.items():
            if isinstance(value, str) and name in KEY_NAMES and value != exclude and KEY_SHAPE.match(value) \
                    and not value.startswith('uak_'):
                return value
            if isinstance(value, (dict, list)):
                queue.append(value)
    return None


class SignIn:
    """Composio's CLI login, run by the app: the owner signs in on Composio's page and a project key arrives."""

    def __init__(self, transport=None, sleep=time.sleep):
        self.transport = transport or HttpTransport()
        self.sleep = sleep

    def _call(self, method, path, body=None, headers=None):
        status, payload = self.transport(method, BACKEND + path, None, body, headers=headers or {})
        return status, payload if isinstance(payload, dict) else {}

    def start(self):
        status, value = self._call('POST', '/api/v3.1/cli/create-session', {'scope': 'project'})
        sid = str(value.get('id') or '')
        if status >= 400 or not SESSION_ID.match(sid):
            raise _error(status if status >= 400 else 502, value)
        return {'id': sid, 'code': str(value.get('code') or '')[:12], 'url': DASHBOARD_URL + '?cliKey=' + sid,
                'expires': str(value.get('expiresAt') or '')}

    def poll(self, sid):
        """('pending', None) | ('linked', user_key) | ('expired', None). Composio down counts as pending."""
        if not SESSION_ID.match(sid or ''):
            return 'expired', None
        try:
            status, value = self._call('GET', '/api/v3.1/cli/get-session?' + urllib.parse.urlencode({'id': sid}))
        except ComposioError:
            return 'pending', None
        if status == 404:
            return 'expired', None
        if status >= 400:
            return ('pending', None) if status >= 500 or status == 429 else ('expired', None)
        if value.get('status') == 'linked' and isinstance(value.get('api_key'), str) and KEY_SHAPE.match(value['api_key']):
            return 'linked', value['api_key']
        return 'pending', None

    CLI_HEADERS = {'x-source': 'CLI', 'x-framework': 'cli'}

    def _log(self, step, status, payload=None):
        err = payload.get('error') if isinstance(payload, dict) else None
        slug = str((err or {}).get('slug') or '') if isinstance(err, dict) else ''
        print(f'composio sign-in: {step} status={status} {slug}'.rstrip(), file=sys.stderr, flush=True)

    def _works(self, key, tries=4):
        """A fresh project key can take a few seconds to be accepted: check it a few times."""
        for attempt in range(tries):
            status, payload = self.transport('GET', API + '/connected_accounts?limit=1', key, None)
            self._log('check_key', status, payload)
            if status < 400:
                return True
            if status not in (401, 403, 429) and status < 500:
                return False
            if attempt < tries - 1:
                self.sleep(2 + attempt * 2)
        return False

    def project_key(self, user_key):
        """The project API key for the signed-in Composio account (read or minted once). Never returns the user key.

        Mirrors the Composio CLI: session/info by user key, then again scoped to its org and project, then mint."""
        auth = {**self.CLI_HEADERS, 'x-user-api-key': user_key}
        status, info = self._call('GET', '/api/v3.1/auth/session/info', headers=auth)
        self._log('session_info', status, info)
        if status >= 400:
            raise _error(status, info)
        project = info.get('project') if isinstance(info.get('project'), dict) else {}
        org = project.get('org') if isinstance(project.get('org'), dict) else {}
        pid, oid = str(project.get('nano_id') or project.get('id') or ''), str(org.get('id') or '')
        scoped = {**auth, 'x-org-id': oid, 'x-project-id': pid} if NANO_ID.match(pid) and NANO_ID.match(oid) else None
        candidates = []
        if scoped:
            status2, info2 = self._call('GET', '/api/v3.1/auth/session/info', headers=scoped)
            self._log('session_info_scoped', status2, info2)
            if status2 < 400:
                candidates.append(info2)
        candidates.append(info)
        seen = set()
        for source in candidates:
            found = source.get('api_key') if isinstance(source.get('api_key'), dict) else {}
            key = found.get('key')
            if isinstance(key, str) and key != user_key and key not in seen and KEY_SHAPE.match(key) and not key.startswith('uak_'):
                seen.add(key)
                if self._works(key, tries=2):
                    return key
        if not scoped:
            raise ComposioError('unavailable', 'Composio did not say which project to use. Paste a key instead.', 502)
        status, made = self._call('POST', f'/api/v3/org/project/{urllib.parse.quote(pid, safe="")}/api_keys/create',
                                  {'name': 'wizard-' + time.strftime('%Y-%m-%d')}, scoped)
        self._log('create_key', status, made)
        key = _find_key(made, user_key) if status < 400 else None
        if not key:
            raise _error(status, made) if status >= 400 else ComposioError(
                'unavailable', 'Composio did not hand over a key. Paste a key instead.', 502)
        if not self._works(key):
            raise ComposioError('key_invalid', 'Composio made a key but does not accept it yet. Try again in a minute.', 401)
        return key


# ---------- One guided popup: Composio sign-in (if needed), then each provider sign-in ----------

GUIDE_TTL = 30 * 60


class Guide:
    """Quick connect as one popup the owner only signs in through. The app passes an adapter with:

        ready()                               -> bool: a saved Composio key works
        add_key(key)                          -> saves a project key (validated by Keys.add)
        start(session, service, account, callback_base) -> {'attempt', 'redirect_url'}
        status(session, attempt)              -> {'status': pending|connected|failed, 'result': {...}}
        label(service)                        -> 'Gmail', 'Outlook', ...

    `advance()` moves the flow as far as it can and says where the popup belongs: on Composio's sign-in page,
    on a provider sign-in page, or done. The popup itself loops through the app's `/quick/go/<flow>` page."""

    def __init__(self, adapter, transport=None, clock=time.time, signin=None):
        self.adapter, self.clock = adapter, clock
        self.signin = signin or SignIn(transport)
        self.flows, self.lock = {}, threading.RLock()

    def begin(self, session, services, account=None):
        if not services:
            raise ComposioError('rejected', 'Choose what to connect.', 400)
        steps = ([] if self.adapter.ready() else ['composio']) + list(services)
        flow_id = 'qf-' + secrets.token_urlsafe(12)
        with self.lock:
            stamp = self.clock()
            self.flows = {k: v for k, v in self.flows.items() if v['expires'] > stamp}
            if len(self.flows) >= 16:
                raise ComposioError('rejected', 'Too many sign-ins in progress.', 429)
            self.flows[flow_id] = {'id': flow_id, 'session': session, 'steps': steps, 'i': 0, 'account': account,
                                   'signin': None, 'attempt': None, 'redirect': None, 'state': 'running',
                                   'where': None, 'result': None, 'error': None, 'connected': [],
                                   'expires': stamp + GUIDE_TTL}
        return flow_id

    def get(self, flow_id, session):
        flow = self.flows.get(flow_id or '')
        if not flow or flow['expires'] <= self.clock() or flow['session'] != session:
            return None
        return flow

    def cancel(self, flow_id, session):
        with self.lock:
            flow = self.get(flow_id, session)
            if flow and flow['state'] == 'running':
                flow.update(state='cancelled', where='done')
        return {'cancelled': True}

    def _fail(self, flow, code, message):
        flow.update(state='failed', where='done', error={'code': code, 'message': message})

    def advance(self, flow, callback_base):
        with self.lock:
            while flow['state'] == 'running' and flow['i'] < len(flow['steps']):
                step = flow['steps'][flow['i']]
                if step == 'composio':
                    if not self._composio(flow):
                        break
                    continue
                if not self._service(flow, step, callback_base):
                    break
            if flow['state'] == 'running' and flow['i'] >= len(flow['steps']):
                flow.update(state='done', where='done')
            return self.view(flow)

    def _composio(self, flow):
        if self.adapter.ready():
            flow['i'] += 1
            return True
        try:
            if not flow['signin']:
                flow['signin'] = self.signin.start()
                flow['where'] = 'composio'
                return False
            state, user_key = self.signin.poll(flow['signin']['id'])
            if state == 'pending':
                flow['where'] = 'composio'
                return False
            if state == 'expired':
                self._fail(flow, 'signin_expired', 'The Composio sign-in timed out. Start again.')
                return False
            key = self.signin.project_key(user_key)
            del user_key
            try:
                self.adapter.add_key(key)
            except ComposioError as error:
                if error.status != 409:  # 409: this key is already saved
                    raise
        except ComposioError as error:
            self._fail(flow, 'composio_' + error.code, error.message)
            return False
        flow['i'] += 1
        return True

    def _service(self, flow, service, callback_base):
        try:
            if not flow['attempt']:
                out = self.adapter.start(flow['session'], service, flow['account'], callback_base + '/' + flow['id'])
                flow.update(attempt=out['attempt'], redirect=out['redirect_url'], where='provider')
                return False
            found = self.adapter.status(flow['session'], flow['attempt'])
        except ComposioError as error:
            self._fail(flow, 'composio_' + error.code, error.message)
            return False
        if found.get('status') == 'connected':
            result = found.get('result') or {}
            flow['account'] = result.get('account') or flow['account']
            flow['result'] = result
            flow['connected'].append(service)
            flow.update(attempt=None, redirect=None)
            flow['i'] += 1
            return True
        if found.get('status') in ('failed', 'none'):
            result = found.get('result') or {}
            self._fail(flow, result.get('code') or 'connect_failed', result.get('message') or 'The sign-in did not finish. Try again.')
            return False
        flow['where'] = 'provider'
        return False

    def target(self, flow):
        """Where the popup should go now: a Composio or provider URL, or None when done."""
        if flow['state'] != 'running':
            return None
        if flow['where'] == 'composio' and flow['signin']:
            return flow['signin']['url']
        if flow['where'] == 'provider':
            return flow['redirect']
        return None

    def view(self, flow):
        step = flow['steps'][flow['i']] if flow['i'] < len(flow['steps']) else None
        return {'flow': flow['id'], 'state': flow['state'], 'where': flow['where'], 'step': step,
                'label': 'Composio' if step == 'composio' else (self.adapter.label(step) if step else None),
                'steps': [{'id': s, 'label': 'Composio' if s == 'composio' else self.adapter.label(s),
                           'done': n < flow['i'] or flow['state'] == 'done'} for n, s in enumerate(flow['steps'])],
                'connected': list(flow['connected']), 'result': flow['result'], 'error': flow['error']}
