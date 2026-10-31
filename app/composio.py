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

Composio holds and refreshes the provider tokens (custody); this app never sees them. Keys are kept by
the app (encrypted store or 0600 file): this module works on a plain dict the app persists.
"""
import json
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

    def __call__(self, method, url, key, body=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.hostname != API_HOST:
            raise ComposioError('policy', 'Endpoint refused.', 500)
        headers = {'x-api-key': key, 'Accept': 'application/json', 'User-Agent': 'wizard-composio/1'}
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
