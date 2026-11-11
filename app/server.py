"""HTTP front of the Microsoft app: owner page + API, Microsoft's return address, Composio's return page,
MCP for agents and a box-only HTTP mirror of the tools. One process, stdlib only."""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph  # noqa: E402
from service import Problem, Service  # noqa: E402
from agent_access import AppGrants, SUGGESTION_TOOLS, SUGGESTION_MCP_TOOLS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
APP_ID = 'wizard-microsoft'
VERSION = '0.3.1'
ASSETS = {'index.html', 'app.js', 'styles.css', 'boot.js', 'wizard-tokens.css', 'wizard-ui.css', 'wizard-sheet.js', 'standalone.css', 'close.js', 'signin.js'}
def out_origin(trusted, host, own):
    """The same box on another of its trusted ports. Wizard keeps links to its own origin inside Wizard, so the
    Quick connect window opens here instead: a real browser window where Google, Microsoft and Composio allow sign-in."""
    name = (host or '').split(':')[0].lower()
    others = sorted(o for o in trusted if o.startswith('https://') and o != own and urllib.parse.urlsplit(o).hostname == name and urllib.parse.urlsplit(o).port)
    return others[0] if others else own

FORWARDING = ('X-Forwarded-For', 'X-Forwarded-Host', 'Forwarded', 'X-Real-IP')
SESSION_TTL = 8 * 3600
MAX_BODY = 128_000
MAX_MAIL_BODY = 26_000_000  # mail_draft with attachments (base64 + JSON)


def csv(value):
    return {x.strip().lower() for x in (value or '').split(',') if x.strip()}


class Config:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        get = lambda k, d='': env.get(k, d)
        self.port = int(get('PORT', '18814'))
        self.data_dir = get('DATA_DIR', str(ROOT / '.data'))
        self.shell_auth = get('WIZARD_SHELL_AUTH', 'tailscale-serve')
        self.owner_logins = csv(get('WIZARD_OWNER_LOGINS'))
        self.shell_path = '/' + get('WIZARD_SHELL_PATH', '/wizard-microsoft/').strip('/') + '/'
        self.owner_token = get('WIZARD_OWNER_TOKEN')
        self.admin_token = get('WIZARD_ADMIN_TOKEN')
        self.dev_mode = get('MICROSOFT_DEV_MODE') == 'loopback-single-owner'
        app_port = get('APP_PORT') or str(self.port)
        self.local_hosts = {f'127.0.0.1:{self.port}', f'localhost:{self.port}', f'{APP_ID}:{self.port}',
                            f'127.0.0.1:{app_port}', f'localhost:{app_port}'}
        listed = csv(get('WIZARD_TRUSTED_HOSTS'))
        self.trusted_hosts = self.local_hosts | listed
        # A bare host name is trusted on every port: the shell reaches apps on :8444 and :8796.
        self.trusted_names = {h for h in listed if ':' not in h}
        self.trusted_origins = {f'http://{h}' for h in self.local_hosts} | {x.rstrip('/') for x in csv(get('WIZARD_TRUSTED_ORIGINS'))}
        self.frame_ancestors = get('WIZARD_FRAME_ANCESTORS', "'self'")
        self.grants_file = get('WIZARD_GRANTS_FILE', '/wizard/grants.json')

    def host_ok(self, host):
        return host in self.trusted_hosts or host.rsplit(':', 1)[0] in self.trusted_names

    def origin_for(self, host):
        for scheme in ('https', 'http'):
            if f'{scheme}://{host}' in self.trusted_origins:
                return f'{scheme}://{host}'
        name = host.rsplit(':', 1)[0]
        if name in self.trusted_names and host not in self.local_hosts:
            return f'https://{host}'
        return None


class Sessions:
    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def create(self):
        cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            stamp = time.time()
            self.items = {k: v for k, v in self.items.items() if v['expires'] > stamp}
            self.items[sha(cookie)] = {'csrf': sha(csrf), 'expires': stamp + SESSION_TTL}
        return cookie, csrf

    def check(self, cookie, csrf=None):
        entry = self.items.get(sha(cookie or ''))
        if not cookie or not entry or entry['expires'] <= time.time():
            raise Problem(401, 'owner_session_required', 'Reload the Microsoft app.')
        if csrf is not None and not hmac.compare_digest(entry['csrf'], sha(csrf)):
            raise Problem(403, 'csrf_rejected', 'Reload the Microsoft app.')
        return sha(cookie)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def read_cookie(header, name):
    for part in (header or '').split(';'):
        key, _, value = part.strip().partition('=')
        if key == name:
            return value
    return None


STATUS_TOOL = {'name': 'microsoft_status', 'annotations': {'readOnlyHint': True}, 'inputSchema': {'type': 'object', 'properties': {}},
               'description': 'Start here. The owner\'s connected Microsoft accounts (Outlook mail and calendar), how each is connected, '
                              'and whether Mail and Calendar are on. Never returns credentials.'}


def tools():
    return [STATUS_TOOL] + graph.mcp_tools() + SUGGESTION_MCP_TOOLS


def rpc(app, message, caller):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    if 'id' not in message:
        return None
    rid, method, params = message['id'], message['method'], message.get('params') or {}
    if method == 'initialize':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                                                        'serverInfo': {'name': APP_ID, 'version': VERSION}}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {}}
    if method == 'tools/list':
        listed = app.service.access.visible_tools(caller, [t['name'] for t in tools()])
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': [t for t in tools() if t['name'] in listed]}}
    if method != 'tools/call' or not isinstance(params, dict):
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Method not found'}}
    name, args = params.get('name'), params.get('arguments') or {}
    try:
        if not isinstance(args, dict):
            raise Problem(400, 'invalid_arguments', 'Arguments must be an object.')
        if name == 'microsoft_status':
            value = app.service.status()
        elif name in graph.TOOLS or name in SUGGESTION_TOOLS:
            value = app.service.call_tool(caller, name, args)
        else:
            return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Unknown tool'}}
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}}
    except Problem as problem:
        error = problem.as_dict()
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': json.dumps({'error': error})}],
                                                        'structuredContent': {'error': error}, 'isError': True}}


class App:
    def __init__(self, config, service=None, http=None, composio_transport=None):
        self.config = config
        self.service = service or Service(config.data_dir, http=http, composio_transport=composio_transport)
        self.sessions = Sessions()
        self.grants = AppGrants(config.grants_file)

    def shell_path_for(self, host):
        return '/' if host in self.config.local_hosts else self.config.shell_path


def handler_for(app):
    config = app.config

    class Handler(BaseHTTPRequestHandler):
        server_version = APP_ID

        def headers_common(self, content_type):
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
                             f"form-action 'self'; base-uri 'none'; frame-ancestors {config.frame_ancestors}")

        def send_json(self, status, value, extra=None):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.headers_common('application/json')
            for key, val in (extra or {}).items():
                self.send_header(key, val)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def send_page(self, status, title, message):
            esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            raw = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                   f'<meta name="color-scheme" content="dark light"><title>{esc(title)}</title><link rel="stylesheet" href="../wizard-tokens.css">'
                   f'<link rel="stylesheet" href="../wizard-ui.css"><link rel="stylesheet" href="../standalone.css">'
                   f'<body><main class="wz-empty"><strong>{esc(title)}</strong><p>{esc(message)}</p></main></body></html>').encode()
            self.send_response(status)
            self.headers_common('text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def send_redirect(self, url):
            self.send_response(302)
            self.headers_common('text/plain; charset=utf-8')
            self.send_header('Location', url)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def send_signin(self, target, flow):
            # The Composio step: Composio's page never returns, so signin.js opens it in a small window and moves on.
            esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            raw = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                   f'<title>Sign in to Composio</title><link rel="stylesheet" href="../../wizard-tokens.css"><link rel="stylesheet" href="../../wizard-ui.css"><link rel="stylesheet" href="../../standalone.css">'
                   f'<body><main class="wz-empty" id="signin" data-target="{esc(target)}" data-flow="{esc(flow)}"><strong>Sign in to Composio</strong>'
                   f'<p id="signin-note">Quick connect uses a free Composio account. Sign in or create one; Wizard saves the key by itself.</p>'
                   f'<p><a class="wz-button" id="signin-go" href="{esc(target)}">Sign in to Composio</a></p></main>'
                   f'<script src="../../signin.js"></script></body></html>').encode()
            self.send_response(200)
            self.headers_common('text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def send_done(self, title, message, close):
            # The popup's last page. close.js closes it when the app opened it; the opener also closes it.
            esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            script = '<script src="../../close.js"></script>' if close else ''
            raw = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                   f'<meta name="color-scheme" content="dark light"><title>{esc(title)}</title><link rel="stylesheet" href="../../wizard-tokens.css">'
                   f'<link rel="stylesheet" href="../../wizard-ui.css"><link rel="stylesheet" href="../../standalone.css">'
                   f'<body><main class="wz-empty"><strong>{esc(title)}</strong><p>{esc(message)}</p></main>{script}</body></html>').encode()
            self.send_response(200)
            self.headers_common('text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def flow_links(self, host, flow):
            shell = app.shell_path_for(host)
            out = out_origin(config.trusted_origins, host, config.origin_for(host))
            return {'go': shell + 'quick/go/' + flow, 'go_out': out + shell + 'quick/go/' + flow if out else None}

        def flow_base(self, host):
            origin = config.origin_for(host) or f'http://{host}'
            return origin.rstrip('/') + app.shell_path_for(host) + 'quick/go'

        def fail(self, problem):
            self.send_json(problem.status, {'error': problem.as_dict()})

        def log_message(self, *args):
            pass  # no request logs: return addresses carry sign-in codes

        # ----- guards -----
        def host(self):
            host = (self.headers.get('Host') or '').lower()
            if not config.host_ok(host):
                raise Problem(403, 'unknown_host', 'Unknown host.')
            return host

        def owner(self):
            host = self.host()
            if self.headers.get('Authorization'):
                raise Problem(403, 'owner_bearer_refused', 'Bearer credentials cannot open the owner page.')
            if config.dev_mode and host in config.local_hosts:
                return host
            if config.owner_token:
                supplied = self.headers.get('X-Wizard-Owner-Token', '')
                if supplied and hmac.compare_digest(supplied, config.owner_token):
                    return host
            if config.shell_auth == 'tailscale-serve' and host not in config.local_hosts:
                # Only the box owner's own tailnet login. With no owner login configured, no tailnet login is enough:
                # any member of the tailnet (a shared user, a guest device) would otherwise read the owner's mail.
                # The App Manager writes WIZARD_OWNER_LOGINS at install; the owner token still works without it.
                logins = [x.strip().lower() for x in self.headers.get_all('Tailscale-User-Login') or []]
                if config.owner_logins and len(logins) == 1 and logins[0] in config.owner_logins:
                    return host
                if not config.owner_logins:
                    raise Problem(401, 'owner_login_not_configured', 'This app does not know its owner yet. Update it from your Wizard.')
                raise Problem(401, 'owner_login_required', 'Open this app from your Wizard as its owner.')
            raise Problem(401, 'owner_required', 'Open this app from your Wizard.')

        def same_origin_write(self, host):
            origin = (self.headers.get('Origin') or '').lower().rstrip('/')
            if not origin or origin != config.origin_for(host) or (self.headers.get('Sec-Fetch-Site') or 'same-origin').lower() != 'same-origin':
                raise Problem(403, 'origin_rejected', 'Open this app from your Wizard.')
            return origin

        def session(self, csrf=False):
            return app.sessions.check(read_cookie(self.headers.get('Cookie'), 'wm_session'), self.headers.get('X-CSRF-Token', '') if csrf else None)

        def box_caller(self):
            """MCP and the /v1 mirror: agents and apps on this box only. Browsers and forwarded requests are refused."""
            if self.headers.get('Origin') or self.headers.get('Cookie') or self.headers.get('Sec-Fetch-Site'):
                raise Problem(403, 'browser_refused', 'Browsers cannot use this API.')
            if any(self.headers.get(x) for x in FORWARDING) or any(k.lower().startswith(('tailscale-', 'cf-')) for k in self.headers.keys()):
                raise Problem(403, 'forwarding_refused', 'Forwarded requests are refused.')
            host = (self.headers.get('Host') or '').lower()
            supplied = self.headers.get('Authorization', '')
            if config.admin_token and supplied and hmac.compare_digest(supplied, 'Bearer ' + config.admin_token):
                pass
            elif not (host in config.local_hosts and not supplied):
                raise Problem(401, 'not_box_local', 'This API is for agents and apps on this box.')
            # A Wizard app acting for the owner (Mail's Send button) proves itself with its App Manager grant. Apps send it
            # only for requests from the owner's signed-in screen; anything else here is an agent at the owner's level.
            via_grant = app.grants.caller(self.headers.get('X-Wizard-Grant', ''))
            if via_grant:
                return 'app:' + via_grant
            agent = (self.headers.get('X-Wizard-Agent') or 'main').strip().lower()
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', agent):
                raise Problem(400, 'invalid_agent', 'Invalid agent name.')
            return 'agent:' + agent

        def body(self, limit=MAX_BODY):
            if (self.headers.get('Content-Type') or '').split(';')[0].strip().lower() != 'application/json':
                raise Problem(415, 'content_type', 'Use application/json.')
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                raise Problem(400, 'invalid_length', 'Invalid Content-Length.') from None
            if length > limit and limit == MAX_MAIL_BODY:
                raise Problem(413, 'body_size', 'Too big: attachments can be at most 18 MB together.')
            if not 1 <= length <= limit:
                raise Problem(413, 'body_size', 'Request body is empty or too large.')
            try:
                value = json.loads(self.rfile.read(length))
            except ValueError:
                raise Problem(400, 'invalid_json', 'Invalid JSON.') from None
            if not isinstance(value, dict):
                raise Problem(400, 'invalid_body', 'Expected a JSON object.')
            return value

        def redirect_uri(self, host):
            origin = config.origin_for(host) or f'http://{host}'
            return origin.rstrip('/') + app.shell_path_for(host) + 'oauth/callback'

        # ----- routes -----
        def do_GET(self):
            try:
                url = urllib.parse.urlsplit(self.path)
                path, query = url.path, {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
                if path == '/healthz':
                    self.host()
                    return self.send_json(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
                if path in ('/api/connection', '/v1/status'):
                    self.box_caller() if path == '/v1/status' else self.host()
                    if any(self.headers.get(x) for x in FORWARDING) or any(k.lower().startswith(('tailscale-', 'cf-')) for k in self.headers.keys()):
                        raise Problem(403, 'box_only', 'Box-only route.')
                    return self.send_json(200, app.service.connection() if path == '/api/connection' else app.service.status())
                if path == '/oauth/callback':
                    self.owner()
                    try:
                        result = app.service.finish_own(query.get('state'), self.session(), query.get('code'), query.get('error'),
                                                        query.get('error_description', ''))
                    except Problem as problem:
                        return self.send_page(400, 'Not connected', problem.message + ' Return to Wizard.')
                    return self.send_page(200, 'Connected', f"{result['email'] or 'Your account'} is connected. Close this tab and return to Wizard.")
                if path == '/quick/callback':
                    self.owner()
                    attempt = query.get('attempt') or ''
                    try:
                        session = self.session()
                    except Problem:
                        session = None
                    found = app.service.links.get(attempt) if re.fullmatch(r'cl-[A-Za-z0-9_-]{8,40}', attempt) else None
                    if found and session and found.get('session') == session:
                        result = app.service.quick_status(session, attempt)
                        if result['status'] == 'connected':
                            return self.send_page(200, 'Connected', f"{result['result']['email'] or 'Your account'} is connected. Close this tab and return to Wizard.")
                        if result['status'] == 'failed':
                            return self.send_page(400, 'Not connected', result['result']['message'])
                    return self.send_page(200, 'Almost done', 'Return to Wizard: the Microsoft app finishes the connection there.')
                go = re.fullmatch(r'/quick/go/(qf-[A-Za-z0-9_-]{8,40})', path)
                if go:
                    # The Quick connect popup loops through here: Composio sign-in, then the Microsoft sign-in, then done.
                    host = self.owner()
                    try:
                        view, target = app.service.flow_step(self.session(), go.group(1), self.flow_base(host))
                    except Problem as problem:
                        return self.send_done('Not connected', problem.message, False)
                    if target and view.get('where') == 'composio':
                        return self.send_signin(target, go.group(1))
                    if target:
                        return self.send_redirect(target)
                    if view['state'] == 'done':
                        who = (view.get('result') or {}).get('email') or 'Your account'
                        return self.send_done('Connected', f'{who} is connected. This window closes by itself.', True)
                    return self.send_done('Not connected', (view.get('error') or {}).get('message') or 'Start again from Wizard.', True)
                if path.startswith('/api/'):
                    host = self.owner()
                    session = self.session()
                    if path == '/api/quick/flow':
                        flow = query.get('flow') or ''
                        if not re.fullmatch(r'qf-[A-Za-z0-9_-]{8,40}', flow):
                            raise Problem(400, 'invalid_flow', 'Unknown sign-in.')
                        view, _ = app.service.flow_step(session, flow, self.flow_base(host))
                        return self.send_json(200, {**view, **self.flow_links(host, flow)})
                    if path == '/api/home':
                        return self.send_json(200, app.service.home(self.redirect_uri(host), session))
                    if path == '/api/own/status':
                        return self.send_json(200, app.service.own_status(session))
                    if path == '/api/quick/status':
                        return self.send_json(200, app.service.quick_status(session))
                    if path == '/api/mail-settings':
                        return self.send_json(200, app.service.mail_settings())
                    if path == '/api/calendar-settings':
                        return self.send_json(200, app.service.calendar_settings())
                    raise Problem(404, 'not_found', 'Not found.')
                self.host()
                asset = 'index.html' if path in ('', '/') else path.lstrip('/')
                if asset not in ASSETS:
                    raise Problem(404, 'not_found', 'Not found.')
                raw = (WEB / asset).read_bytes()
                kind = mimetypes.guess_type(asset)[0] or 'application/octet-stream'
                self.send_response(200)
                self.headers_common(kind + ('; charset=utf-8' if kind.startswith('text/') or kind.endswith('javascript') else ''))
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Problem as problem:
                self.fail(problem)
            except OSError:
                self.fail(Problem(404, 'not_found', 'Not found.'))

        def do_POST(self):
            try:
                path = urllib.parse.urlsplit(self.path).path
                if path == '/mcp':
                    caller = self.box_caller()
                    self.host()
                    output = rpc(app, self.body(MAX_MAIL_BODY), caller)
                    return self.send_json(202 if output is None else 200, output or {})
                local = re.fullmatch(r'/v1/([a-z_]{3,40})', path)
                if local:
                    caller = self.box_caller()
                    self.host()
                    if local.group(1) not in graph.TOOLS and local.group(1) not in SUGGESTION_TOOLS:
                        raise Problem(404, 'not_found', 'Not found.')
                    limit = MAX_MAIL_BODY if local.group(1) == 'mail_draft' else MAX_BODY
                    return self.send_json(200, app.service.call_tool(caller, local.group(1), self.body(limit)))
                host = self.owner()
                origin = self.same_origin_write(host)
                if path == '/api/session':
                    if self.headers.get('X-Wizard-Request') != 'bootstrap':
                        raise Problem(403, 'bootstrap_refused', 'Session bootstrap refused.')
                    cookie, csrf = app.sessions.create()
                    secure = '; Secure' if origin.startswith('https://') else ''
                    return self.send_json(201, {'csrf': csrf}, {'Set-Cookie': f'wm_session={cookie}; Path={app.shell_path_for(host)}; HttpOnly; SameSite=Lax{secure}'})
                session = self.session(csrf=True)
                body = self.body()
                s = app.service
                shell = origin.rstrip('/') + app.shell_path_for(host)
                routes = {
                    '/api/own/client': lambda: s.save_client(body.get('client_id'), body.get('client_secret'), body.get('tenant') or 'common'),
                    '/api/own/client/forget': s.forget_client,
                    '/api/own/start': lambda: s.start_own(session, self.redirect_uri(host), body.get('account')),
                    '/api/quick/keys': lambda: s.add_key(body.get('key'), body.get('label')),
                    '/api/quick/start': lambda: s.start_quick(session, body.get('account'), shell + 'quick/callback'),
                    '/api/quick/flow': lambda: (lambda f: {**f, **self.flow_links(host, f['flow'])})(
                        s.flow_begin(session, body.get('account'))),
                    '/api/quick/flow/cancel': lambda: s.flow_cancel(session, body.get('flow')),
                    '/api/agent-access': lambda: s.set_agent_access(body),
                }
                if path in routes:
                    return self.send_json(200, routes[path]())
                match = re.fullmatch(r'/api/(accounts|quick/keys)/([A-Za-z0-9_-]{4,64})/(service|label|disconnect|remove)', path)
                if match:
                    kind, item, action = match.groups()
                    actions = {('accounts', 'service'): lambda: s.set_service(item, body.get('service'), body.get('on')),
                               ('accounts', 'label'): lambda: s.set_label(item, body.get('label')),
                               ('accounts', 'disconnect'): lambda: s.disconnect(item),
                               ('quick/keys', 'remove'): lambda: s.remove_key(item)}
                    if (kind, action) in actions:
                        return self.send_json(200, actions[(kind, action)]())
                raise Problem(404, 'not_found', 'Not found.')
            except Problem as problem:
                self.fail(problem)

    return wizard_switches_install(Handler)


def wait_for_storage(path, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        try:
            Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
            probe = Path(path) / '.write-check'
            probe.write_text('ok')
            probe.unlink()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def serve(config, app=None):
    app = app or App(config)
    server = ThreadingHTTPServer((os.getenv('BIND', '0.0.0.0'), config.port), handler_for(app))
    server.daemon_threads = True
    return server, app

# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app (template: wizard-app-manager templates/hello-wizard). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only: the
# box's Tailscale Serve owner login with X-Wizard-Switch: 1; never a bearer, agent grant or box-local host.
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or _sw_os.environ.get('DATA_DIR') or '/data') / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------


if __name__ == '__main__':
    config = Config()
    wait_for_storage(config.data_dir)
    server, _ = serve(config)
    server.serve_forever()
