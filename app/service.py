"""Microsoft for Wizard: the service layer under the page, the box API and MCP.

Two ways to connect Outlook mail and calendar:
  * Quick connect: Composio's already-approved Microsoft sign-in (composio.py). Composio keeps the tokens.
  * Your own Microsoft Entra app: the owner registers an app (guided steps on the page), pastes its ID and
    secret; this app signs in with OAuth (PKCE) and keeps the tokens itself.
Either way, agents and apps get the same mail/calendar tools (graph.py, contract wizard.mail-calendar/1).

Everything lives in DATA_DIR/state.json (0600, directory 0700): keys, client secret, tokens. Nothing secret
is ever returned by a view.
"""
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import composio
import graph

LOGIN = 'https://login.microsoftonline.com'
SCOPES = ['offline_access', 'openid', 'email', 'profile', 'User.Read', 'Mail.ReadWrite', 'Mail.Send', 'Calendars.ReadWrite']
GRAPH_PERMISSIONS = ['offline_access', 'User.Read', 'Mail.ReadWrite', 'Mail.Send', 'Calendars.ReadWrite']
SERVICES = {'mail': {'label': 'Mail', 'what': 'Search, read, draft and send mail'},
            'calendar': {'label': 'Calendar', 'what': 'See, add, change and answer events'}}
TOOLKIT = 'outlook'
APP_NAME = 'Wizard'
ENTRA_NEW = 'https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/CreateApplicationBlade/quickStartType~/null/isMSAApp~/false'
ENTRA_APPS = 'https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade'
AZURE_FREE = 'https://azure.microsoft.com/free/'
CONSENTS = 'https://account.live.com/consent/Manage'
WORK_APPS = 'https://myapplications.microsoft.com/'
FLOW_TTL = 600
AUDIT_KEEP = 200
GUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
NOTE = ("Quick connect goes through Composio, a third party. They hold the sign-in keys, so technically they can access this "
        "account. Free for normal personal use; heavy use can cost money. We think that's an acceptable start: Microsoft "
        "already reads your data. The most private setup is your own email on your Wizard, using a provider like "
        "Cloudflare only to send. You can switch any time.")


class Problem(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def as_dict(self):
        return {'code': self.code, 'message': self.message, **self.extra}


def now():
    return int(time.time())


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


# ---------- storage ----------

def empty_state():
    return {'schema': 1, 'client': None, 'accounts': {}, 'quick': {'keys': [], 'box': secrets.token_hex(8)}, 'audit': []}


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.path = self.root / 'state.json'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path, mode in ((self.root, 0o700), (self.path, 0o600)):
            try:
                os.chmod(path, mode)  # Runtipi loosens app-data on lifecycle actions
            except (FileNotFoundError, PermissionError):
                pass

    def load(self):
        if not self.path.exists():
            return empty_state()
        value = json.loads(self.path.read_text())
        base = empty_state()
        base.update(value)
        base['quick'].setdefault('keys', [])
        base['quick'].setdefault('box', secrets.token_hex(8))
        return base

    def save(self, state):
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


# ---------- HTTP to Microsoft ----------

class Http:
    """urllib to Microsoft's login and Graph hosts only; no redirects, no environment proxies."""
    HOSTS = {'login.microsoftonline.com', 'graph.microsoft.com'}

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), self._NoRedirect())

    def __call__(self, method, url, *, token=None, form=None, body=None, headers=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in self.HOSTS:
            raise graph.GraphError('transport_policy', 500, 'Endpoint refused.')
        h = {'Accept': 'application/json', 'User-Agent': 'wizard-microsoft', **(headers or {})}
        data = None
        if token:
            h['Authorization'] = 'Bearer ' + token
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h['Content-Type'] = 'application/x-www-form-urlencoded'
        elif body is not None:
            data = json.dumps(body).encode()
            h['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(request, timeout=25) as response:
                status, raw = response.status, response.read(8_000_001)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(65536)
            error.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            raise graph.GraphError('provider_unavailable', 502, 'Could not reach Microsoft.') from None
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        return status, payload


def oauth_error(payload):
    codes = payload.get('error_codes') if isinstance(payload, dict) else None
    codes = codes if isinstance(codes, list) else []
    return str(payload.get('error') or '') if isinstance(payload, dict) else '', codes


# ---------- the service ----------

class Service:
    def __init__(self, data_dir, *, http=None, composio_transport=None, clock=time.time):
        self.store = Store(data_dir)
        self.state = self.store.load()
        self.http = http or Http()
        self.composio_transport = composio_transport
        self.clock = clock
        self.lock = threading.RLock()
        self.flows = {}               # sha(state) -> own-app sign-in in progress (memory only)
        self.links = composio.Links(clock)

    def _commit(self):
        self.store.save(self.state)

    def _audit(self, caller, account, tool, outcome):
        with self.lock:
            self.state['audit'] = (self.state['audit'] + [{'at': now(), 'client': caller, 'account': account, 'operation': tool,
                                                           'outcome': outcome}])[-AUDIT_KEEP:]
            self._commit()

    # ===== Your own Microsoft Entra app =====

    def setup_steps(self, redirect_uri):
        client = self.state['client']
        return [
            {'id': 'register', 'title': 'Register an app', 'where': 'microsoft', 'url': ENTRA_NEW,
             'detail': ['Sign in to Microsoft Entra with your Microsoft account and choose New registration.',
                        'Name: Wizard. Supported account types: Accounts in any organizational directory and personal Microsoft accounts.',
                        'Redirect URI: choose Web and paste the address below. Then press Register.',
                        'If Microsoft says you need a directory first, create a free Azure account with the same Microsoft account, then come back. '
                        'Microsoft asks for a card to check who you are; app registrations are free.'],
             'copy': [{'label': 'Name', 'value': APP_NAME}, {'label': 'Redirect URI (Web)', 'value': redirect_uri}],
             'links': [{'label': 'Free Azure account', 'url': AZURE_FREE}]},
            {'id': 'secret', 'title': 'Make a client secret', 'where': 'microsoft', 'url': ENTRA_APPS,
             'detail': ['In your new app, open Certificates & secrets, then New client secret. Choose the longest expiry.',
                        'Copy the Value right away (not the Secret ID): Microsoft shows it once.']},
            {'id': 'permissions', 'title': 'Allow mail and calendar', 'where': 'microsoft', 'url': ENTRA_APPS,
             'detail': ['Open API permissions, Add a permission, Microsoft Graph, Delegated permissions.',
                        'Tick the permissions below, then Add permissions. A work or school account may need its admin to approve them.'],
             'copy': [{'label': 'Delegated permissions', 'value': ', '.join(GRAPH_PERMISSIONS)}]},
            {'id': 'paste', 'title': 'Paste the ID and secret here', 'where': 'wizard', 'done': bool(client),
             'detail': ['From Overview copy the Application (client) ID. Paste it with the secret value below.']},
            {'id': 'connect', 'title': 'Connect your account', 'where': 'wizard',
             'done': any(a['via'] == 'own' and a['status'] == 'active' for a in self.state['accounts'].values()),
             'detail': ['Sign in with Microsoft and accept. Each further account works the same way.']},
        ]

    def client_view(self):
        client = self.state['client']
        if not client:
            return None
        return {'client_id': client['client_id'], 'secret_hint': '••••' + client['client_secret'][-4:], 'tenant': client['tenant'],
                'checked': client.get('checked'), 'added': client.get('added')}

    def probe_client(self, client_id, client_secret, tenant):
        """Ask Microsoft whether the ID and secret belong together, with no one signing in.

        A client-credentials request authenticates the app before anything else: a wrong secret answers
        invalid_client (AADSTS7000215/7000222), an unknown app AADSTS700016. Anything else means the pair passed
        that check; the first sign-in proves the rest. The token Microsoft may return is dropped unread."""
        try:
            status, payload = self.http('POST', f'{LOGIN}/{tenant}/oauth2/v2.0/token', form={
                'grant_type': 'client_credentials', 'client_id': client_id, 'client_secret': client_secret,
                'scope': 'https://graph.microsoft.com/.default'})
        except graph.GraphError:
            return 'unknown'
        if status < 400:
            return 'ok'
        error, codes = oauth_error(payload)
        if 700016 in codes:
            raise Problem(400, 'client_unknown', 'Microsoft does not know this Application (client) ID. Copy it again from the app\'s Overview.')
        if error == 'invalid_client' or 7000215 in codes or 7000222 in codes:
            message = 'This client secret has expired. Make a new one and paste its Value.' if 7000222 in codes else \
                'Microsoft does not accept this secret. Paste the secret\'s Value (not its Secret ID).'
            raise Problem(400, 'secret_invalid', message)
        return 'ok' if error in ('unauthorized_client', 'invalid_scope') else 'unknown'

    def save_client(self, client_id, client_secret, tenant='common'):
        client_id = (client_id or '').strip()
        client_secret = (client_secret or '').strip()
        tenant = (tenant or 'common').strip() or 'common'
        if not GUID.match(client_id):
            raise Problem(400, 'client_id_invalid', 'The Application (client) ID looks like 1a2b3c4d-…, five groups of letters and digits.')
        if not 8 <= len(client_secret) <= 200 or any(c.isspace() for c in client_secret):
            raise Problem(400, 'secret_invalid', 'Paste the secret\'s Value from Certificates & secrets.')
        if tenant not in ('common', 'consumers', 'organizations') and not GUID.match(tenant):
            raise Problem(400, 'tenant_invalid', 'Leave this as common, or paste your Directory (tenant) ID.')
        with self.lock:
            current = self.state['client']
            if current and current['client_id'] != client_id and any(a['via'] == 'own' for a in self.state['accounts'].values()):
                raise Problem(409, 'client_in_use', 'Disconnect the accounts signed in with your own app before changing it.')
        checked = self.probe_client(client_id, client_secret, 'common' if tenant in ('consumers',) else tenant)
        with self.lock:
            self.state['client'] = {'client_id': client_id, 'client_secret': client_secret, 'tenant': tenant, 'added': now(), 'checked': checked}
            self._commit()
        return {'client': self.client_view(), 'check': checked}

    def forget_client(self):
        with self.lock:
            if any(a['via'] == 'own' for a in self.state['accounts'].values()):
                raise Problem(409, 'client_in_use', 'Disconnect the accounts signed in with your own app first.')
            self.state['client'] = None
            self._commit()
        return {'forgotten': True}

    def start_own(self, session, redirect_uri, account=None):
        client = self.state['client']
        if not client:
            raise Problem(409, 'client_required', 'Paste your app\'s ID and secret first.')
        if not redirect_uri.startswith('https://'):
            raise Problem(409, 'box_address_unusable', 'Open Wizard through its https address to sign in to Microsoft.')
        login_hint = None
        if account:
            current = self.state['accounts'].get(account)
            if not current or current['via'] != 'own':
                raise Problem(404, 'account_not_found', 'Account not found.')
            login_hint = current['email'] or None
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        with self.lock:
            stamp = self.clock()
            self.flows = {k: v for k, v in self.flows.items() if v['expires'] > stamp and v['session'] != session}
            self.flows[sha(state)] = {'session': session, 'verifier': verifier, 'redirect_uri': redirect_uri, 'account': account,
                                      'expires': stamp + FLOW_TTL, 'status': 'pending', 'result': None, 'started': stamp}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': client['client_id'], 'response_type': 'code', 'redirect_uri': redirect_uri, 'response_mode': 'query',
                  'scope': ' '.join(SCOPES), 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
                  'prompt': 'select_account'}
        if login_hint:
            params['login_hint'] = login_hint
        return {'authorization_url': f"{LOGIN}/{client['tenant']}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params),
                'redirect_uri': redirect_uri, 'expires_in': FLOW_TTL}

    def own_status(self, session):
        with self.lock:
            mine = [f for f in self.flows.values() if f['session'] == session]
        if not mine:
            return {'status': 'none'}
        latest = max(mine, key=lambda f: f['started'])
        return {'status': latest['status'], 'result': latest['result']}

    def finish_own(self, state, session, code=None, error=None, description=''):
        if not isinstance(state, str) or len(state) > 512:
            raise Problem(400, 'invalid_state', 'This sign-in link is not valid. Start again from Wizard.')
        with self.lock:
            flow = self.flows.get(sha(state))
            if not flow or flow['status'] != 'pending' or flow['expires'] <= self.clock() or not hmac.compare_digest(flow['session'], session or ''):
                raise Problem(400, 'invalid_state', 'This sign-in expired or was started in another browser. Start again from Wizard.')
            flow['status'] = 'exchanging'
        try:
            result = self._finish_own(flow, code, error, description)
            flow.update(status='connected', result=result)
            return result
        except Problem as problem:
            flow.update(status='failed', result=problem.as_dict())
            raise

    def _token(self, form):
        client = self.state['client']
        status, payload = self.http('POST', f"{LOGIN}/{client['tenant']}/oauth2/v2.0/token",
                                    form={**form, 'client_id': client['client_id'], 'client_secret': client['client_secret']})
        if status >= 400:
            error, codes = oauth_error(payload)
            if error == 'invalid_grant' or 70008 in codes or 700082 in codes:
                raise Problem(409, 'reauth_required', 'Microsoft ended this sign-in. Connect the account again.')
            if error == 'invalid_client' or 7000215 in codes or 7000222 in codes:
                raise Problem(409, 'secret_invalid', 'Your app\'s client secret is wrong or expired. Make a new one and paste it in Your own app.')
            if 50011 in codes or 'redirect' in str(payload.get('error_description', '')).lower():
                raise Problem(400, 'redirect_uri_mismatch', 'The Redirect URI in your app does not match. Copy it again from step 1.')
            raise Problem(502, 'provider_rejected', 'Microsoft refused the sign-in: ' + str(payload.get('error_description', error))[:200])
        if not isinstance(payload.get('access_token'), str) or not isinstance(payload.get('expires_in'), (int, float)):
            raise Problem(502, 'invalid_provider_grant', 'Microsoft returned an unexpected answer.')
        return payload

    def _finish_own(self, flow, code, error, description):
        if error:
            if 'AADSTS65004' in (description or '') or error == 'access_denied':
                raise Problem(400, 'consent_declined', 'Microsoft sign-in was cancelled.')
            raise Problem(400, 'signin_failed', f'Microsoft said: {(description or error)[:200]}')
        if not isinstance(code, str) or not 1 <= len(code) <= 4096:
            raise Problem(400, 'invalid_code', 'Microsoft did not return a sign-in code.')
        tokens = self._token({'grant_type': 'authorization_code', 'code': code, 'redirect_uri': flow['redirect_uri'],
                              'code_verifier': flow['verifier'], 'scope': ' '.join(SCOPES)})
        token = tokens['access_token']

        def direct(method, url, query=None, body=None, headers=None):
            return self._graph(method, url, token, query, body, headers)
        me = graph.profile(direct)
        if not me['id']:
            raise Problem(502, 'identity_invalid', 'Microsoft returned no account identity.')
        account = 'm-' + sha(me['id'])[:16]
        with self.lock:
            if flow['account'] and flow['account'] != account:
                raise Problem(409, 'wrong_account', 'You signed in with a different Microsoft account. Choose the same account to reconnect it.')
            previous = self.state['accounts'].get(account, {})
            refresh = tokens.get('refresh_token') or (previous.get('own') or {}).get('refresh_token')
            if not refresh:
                raise Problem(502, 'no_refresh_token', 'Microsoft did not allow offline access. Add offline_access in API permissions and connect again.')
            self.state['accounts'][account] = {
                'id': account, 'via': 'own', 'email': me['email'], 'name': me['name'], 'label': previous.get('label', ''),
                'status': 'active', 'error': None, 'services': previous.get('services', ['mail', 'calendar']),
                'added': previous.get('added', now()), 'signed_in': now(),
                'own': {'access_token': token, 'refresh_token': refresh, 'expires': self.clock() + tokens['expires_in'],
                        'scopes': str(tokens.get('scope') or '').split()}}
            self._commit()
        return {'connected': True, 'account': account, 'email': me['email']}

    def _own_token(self, account):
        with self.lock:
            a = self.state['accounts'][account]
            own = a['own']
            if own['expires'] > self.clock() + 60:
                return own['access_token']
        try:
            tokens = self._token({'grant_type': 'refresh_token', 'refresh_token': own['refresh_token'], 'scope': ' '.join(SCOPES)})
        except Problem as problem:
            if problem.code in ('reauth_required', 'secret_invalid'):
                self._mark(account, 'reauth_required')
            raise
        with self.lock:
            own.update(access_token=tokens['access_token'], expires=self.clock() + tokens['expires_in'])
            if tokens.get('refresh_token'):
                own['refresh_token'] = tokens['refresh_token']  # Microsoft rotates refresh tokens
            self._commit()
        return tokens['access_token']

    def _graph(self, method, url, token, query=None, body=None, headers=None):
        if query:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(query, doseq=True, quote_via=urllib.parse.quote)
        status, payload = self.http(method, url, token=token, body=body, headers=headers)
        if status >= 400:
            raise graph.classify(status, payload)
        return payload if isinstance(payload, dict) else {}

    def _own_call(self, account):
        def call(method, url, query=None, body=None, headers=None):
            try:
                return self._graph(method, url, self._own_token(account), query, body, headers)
            except graph.GraphError as failure:
                if failure.code != 'token_rejected':
                    raise
                with self.lock:
                    self.state['accounts'][account]['own']['expires'] = 0
                return self._graph(method, url, self._own_token(account), query, body, headers)
        return call

    # ===== Quick connect (Composio) =====

    def keys(self):
        return composio.Keys(self.state['quick'], self._commit, self.composio_transport, self.clock)

    def add_key(self, key, label=''):
        try:
            item = self.keys().add(key, label)
        except composio.ComposioError as error:
            raise Problem(error.status if 400 <= error.status < 600 else 502, 'composio_' + error.code, error.message) from None
        return {'key': item, 'quick': self.quick_view()}

    def remove_key(self, key_id):
        users = [a['email'] or a['id'] for a in self.state['accounts'].values() if a['via'] == 'composio' and a['composio']['key_id'] == key_id]
        if users:
            raise Problem(409, 'key_in_use', f'Disconnect {", ".join(users)} first: they were connected with this key.')
        try:
            self.keys().remove(key_id)
        except composio.ComposioError as error:
            raise Problem(error.status, 'composio_' + error.code, error.message) from None
        return {'quick': self.quick_view()}

    def start_quick(self, session, account=None, callback_base=None):
        existing = self.state['accounts'].get(account) if account else None
        if account and (not existing or existing['via'] != 'composio'):
            raise Problem(404, 'account_not_found', 'Account not found.')
        keys, item = self.keys(), None
        try:
            item = keys.get(existing['composio']['key_id']) if existing else keys.pick()
            user_id = existing['composio']['user_id'] if existing else f"wizard-{self.state['quick']['box']}-{secrets.token_hex(4)}"
            attempt = self.links.add(session=session, key_id=item['id'], user_id=user_id, account=account)
            link = composio.Client(item['key'], self.composio_transport).start_link(
                user_id, TOOLKIT, f'{callback_base}?attempt={attempt}' if callback_base else None)
        except composio.ComposioError as error:
            if error.next_key_may_help and item:
                keys.mark(item['id'], error.code, error.message)
            raise Problem(error.status if 400 <= error.status < 600 else 502, 'composio_' + error.code, error.message) from None
        self.links.items[attempt]['connected_account_id'] = link['connected_account_id']
        return {'attempt': attempt, 'redirect_url': link['redirect_url']}

    def quick_status(self, session=None, attempt_id=None):
        attempt = self.links.get(attempt_id) if attempt_id else self.links.latest(session)
        if not attempt or (session is not None and attempt.get('session') != session):
            return {'status': 'none'}
        if attempt['state'] != 'pending':
            return {'status': attempt['state'], 'result': attempt['result']}
        try:
            found = self.keys().client(attempt['key_id']).account(attempt['connected_account_id'])
        except composio.ComposioError as error:
            if error.code == 'unavailable':
                return {'status': 'pending'}
            attempt.update(state='failed', result={'code': 'composio_' + error.code, 'message': error.message})
            return {'status': 'failed', 'result': attempt['result']}
        if found['status'] in composio.ENDED:
            attempt.update(state='failed', result={'code': 'connect_failed', 'message': 'The sign-in did not finish. Try Quick connect again.'})
            return {'status': 'failed', 'result': attempt['result']}
        if found['status'] not in composio.DONE:
            return {'status': 'pending'}
        with self.lock:
            if attempt['state'] != 'pending':
                return {'status': attempt['state'], 'result': attempt['result']}
            attempt['state'] = 'saving'
        link = {'key_id': attempt['key_id'], 'user_id': attempt['user_id'], 'ca': attempt['connected_account_id']}
        try:
            me = graph.profile(self._quick_call_for(link))
        except graph.GraphError:
            me = {'id': '', 'name': '', 'email': ''}
        with self.lock:
            target = self.state['accounts'].get(attempt['account']) if attempt['account'] else None
            if target and target['email'] and me['email'] and target['email'] != me['email']:
                result = {'code': 'wrong_account', 'message': f'You signed in as {me["email"]}, not {target["email"]}.'}
                attempt.update(state='failed', result=result)
                return {'status': 'failed', 'result': result}
            if not target:
                account = 'q-' + sha(f"{attempt['user_id']}:{me['email'] or attempt['connected_account_id']}")[:16]
                target = self.state['accounts'].setdefault(account, {'id': account, 'via': 'composio', 'label': '', 'services': ['mail', 'calendar'],
                                                                     'added': now()})
            target.update(email=target.get('email') or me['email'], name=target.get('name') or me['name'], status='active', error=None,
                          composio=link, signed_in=now())
            self._commit()
        result = {'connected': True, 'account': target['id'], 'email': target['email']}
        attempt.update(state='connected', result=result)
        return {'status': 'connected', 'result': result}

    def _quick_call_for(self, link, account=None):
        keys = self.keys()
        try:
            client = keys.client(link['key_id'])
        except composio.ComposioError:
            raise graph.GraphError('reauth_required', 409, 'The Composio key for this account was removed. Add it again.') from None

        def call(method, url, query=None, body=None, headers=None):
            try:
                status, data = client.proxy(link['user_id'], link['ca'], method, url, query, body, headers)
            except composio.ComposioError as error:
                if error.code in ('key_invalid', 'key_forbidden', 'quota'):
                    keys.mark(link['key_id'], error.code, error.message)
                if error.code in ('not_found', 'rejected') and account and self._gone(client, link['ca']):
                    self._mark(account, 'reauth_required')
                    raise graph.GraphError('reauth_required', 409, 'Composio no longer has this sign-in. Quick connect again.') from None
                raise graph.GraphError('provider_unavailable' if error.code == 'unavailable' else 'composio_' + error.code,
                                       error.status, error.message) from None
            finally:
                keys.count(link['key_id'])
            if status >= 400:
                failure = graph.classify(status, data if isinstance(data, dict) else {})
                if failure.code == 'token_rejected' and account:
                    self._mark(account, 'reauth_required')
                    failure = graph.GraphError('reauth_required', 409, 'Microsoft ended this sign-in. Quick connect again.')
                raise failure
            return data if isinstance(data, dict) else {}
        return call

    def _gone(self, client, ca):
        try:
            return client.account(ca)['status'] in composio.ENDED
        except composio.ComposioError as error:
            return error.code == 'not_found'

    def _mark(self, account, status):
        with self.lock:
            if account in self.state['accounts']:
                self.state['accounts'][account]['status'] = status
                self._commit()

    # ===== accounts =====

    def account(self, account_id):
        a = self.state['accounts'].get(account_id)
        if not a:
            raise Problem(404, 'account_not_found', 'Account not found.')
        return a

    def set_service(self, account_id, service, on):
        a = self.account(account_id)
        if service not in SERVICES or not isinstance(on, bool):
            raise Problem(400, 'unknown_service', 'Unknown service.')
        with self.lock:
            services = set(a['services'])
            services.add(service) if on else services.discard(service)
            a['services'] = sorted(services)
            self._commit()
        return {'account': self.account_view(a)}

    def set_label(self, account_id, label):
        a = self.account(account_id)
        if not isinstance(label, str) or len(label.strip()) > 40:
            raise Problem(400, 'invalid_label', 'Use a short name, like Work or Personal.')
        with self.lock:
            a['label'] = label.strip()
            self._commit()
        return {'account': self.account_view(a)}

    def disconnect(self, account_id):
        a = self.account(account_id)
        removed = True
        if a['via'] == 'composio':
            try:
                self.keys().client(a['composio']['key_id']).remove(a['composio']['ca'])
            except composio.ComposioError:
                removed = False
        with self.lock:
            self.state['accounts'].pop(account_id, None)
            self._commit()
        # Microsoft has no call for an app to revoke its own grant; the owner removes it on Microsoft's page.
        return {'disconnected': True, 'removed_at_composio': removed if a['via'] == 'composio' else None,
                'manual_url': CONSENTS if a['via'] == 'own' or not removed else None}

    # ===== tools =====

    def _pick(self, value, service):
        everyone = list(self.state['accounts'].values())
        candidates = [a for a in everyone if a['status'] == 'active' and service in a['services']]
        if value not in (None, ''):
            if not isinstance(value, str):
                raise Problem(400, 'invalid_account', 'Name the account by its email or label.')
            wanted = value.strip().lower()
            named = [a for a in everyone if wanted in (a['id'], (a.get('email') or '').lower(), (a.get('label') or '').lower())]
            if not named:
                raise Problem(404, 'account_not_found', 'No connected Microsoft account matches that.', accounts=[a['email'] for a in everyone])
            match = next((a for a in candidates if a in named), None)
            if not match:
                raise Problem(403, 'service_off', f'{SERVICES[service]["label"]} is off or needs a new sign-in for that account. The owner fixes it in the Microsoft app.',
                              app_link='/wizard-os-home/?open=wizard-microsoft')
            return match
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            if not everyone:
                raise Problem(409, 'not_connected', 'No Microsoft account is connected yet. The owner connects one in the Microsoft app.',
                              app_link='/wizard-os-home/?open=wizard-microsoft')
            raise Problem(403, 'service_off', f'No connected Microsoft account has {SERVICES[service]["label"]} on.',
                          app_link='/wizard-os-home/?open=wizard-microsoft')
        raise Problem(400, 'account_required', 'Several accounts can do this; name one with "account".', accounts=[a['email'] for a in candidates])

    def call_tool(self, caller, tool, args):
        """One mail/calendar tool on one account. Mail and event text in the result is untrusted data."""
        if tool not in graph.TOOLS:
            raise Problem(404, 'unknown_tool', 'Unknown tool.')
        service, _, handler = graph.TOOLS[tool]
        args = args if isinstance(args, dict) else {}
        a = self._pick(args.get('account'), service)
        call = self._quick_call_for(a['composio'], a['id']) if a['via'] == 'composio' else self._own_call(a['id'])
        try:
            result = handler(call, args)
        except graph.BadArgs as bad:
            raise Problem(400, 'invalid_arguments', str(bad)) from None
        except graph.GraphError as failure:
            self._audit(caller, a['id'], tool, failure.code)
            raise Problem(failure.status, failure.code, failure.message) from None
        self._audit(caller, a['id'], tool, 'ok')
        return {'contract': graph.CONTRACT, 'source': 'microsoft', 'via': 'composio' if a['via'] == 'composio' else 'own-app',
                'account': a['email'] or a['id'], **result}

    # ===== views =====

    def account_view(self, a):
        return {'id': a['id'], 'email': a['email'], 'name': a.get('name', ''), 'label': a.get('label', ''), 'status': a['status'],
                'via': a['via'], 'services': [{'id': sv, 'label': spec['label'], 'what': spec['what'], 'on': sv in a['services']}
                                              for sv, spec in SERVICES.items()]}

    def quick_view(self):
        keys = self.keys()
        if self.state['quick']['keys']:
            keys.recheck()
        return {'signup_url': composio.SIGNUP_URL, 'key_help': composio.KEY_HELP, 'note': NOTE, 'free_calls': composio.MANAGED_FREE_CALLS,
                'keys': keys.public(), 'ready': any(k['status'] in ('ok', 'unchecked') for k in self.state['quick']['keys'])}

    def home(self, redirect_uri, session=None):
        return {'accounts': [self.account_view(a) for a in self.state['accounts'].values()], 'quick': self.quick_view(),
                'own': {'client': self.client_view(), 'redirect_uri': redirect_uri, 'steps': self.setup_steps(redirect_uri),
                        'permissions': GRAPH_PERMISSIONS, 'consents_url': CONSENTS},
                'services': [{'id': sv, **spec} for sv, spec in SERVICES.items()],
                'audit': self.state['audit'][-30:][::-1]}

    def status(self):
        """For agents and scripts. Never a key or token."""
        accounts = list(self.state['accounts'].values())
        return {'connected': any(a['status'] == 'active' for a in accounts),
                'accounts': [{'email': a['email'], 'label': a.get('label', ''), 'status': a['status'],
                              'via': 'composio' if a['via'] == 'composio' else 'own-app', 'services': a['services']} for a in accounts],
                'contract': graph.CONTRACT, 'tools': sorted(graph.TOOLS), 'app_link': '/wizard-os-home/?open=wizard-microsoft'}

    def connection(self):
        accounts = list(self.state['accounts'].values())
        if not accounts:
            status, error = 'not_connected', {'code': 'signed_out', 'message': 'No Microsoft account connected.'}
        elif all(a['status'] != 'active' for a in accounts):
            status, error = 'expired', {'code': 'expired', 'message': 'Every Microsoft account needs to be connected again.'}
        else:
            status, error = 'connected', None
        return {'contract': 'wizard.microsoft-connection/1', 'provider': 'microsoft', 'name': 'Microsoft', 'status': status,
                'account': ', '.join(a['email'] for a in accounts if a['email']) or None,
                'accounts': [{'id': a['id'], 'email': a['email'], 'label': a.get('label', ''), 'status': a['status'],
                              'via': 'composio' if a['via'] == 'composio' else 'own-app', 'services': a['services']} for a in accounts],
                'error': error}
