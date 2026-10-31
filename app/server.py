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

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
APP_ID = 'wizard-microsoft'
VERSION = '0.1.0'
ASSETS = {'index.html', 'app.js', 'styles.css', 'boot.js', 'wizard-tokens.css', 'wizard-ui.css', 'wizard-sheet.js', 'standalone.css'}
FORWARDING = ('X-Forwarded-For', 'X-Forwarded-Host', 'Forwarded', 'X-Real-IP')
SESSION_TTL = 8 * 3600
MAX_BODY = 128_000


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
    return [STATUS_TOOL] + graph.mcp_tools()


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
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': tools()}}
    if method != 'tools/call' or not isinstance(params, dict):
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Method not found'}}
    name, args = params.get('name'), params.get('arguments') or {}
    try:
        if not isinstance(args, dict):
            raise Problem(400, 'invalid_arguments', 'Arguments must be an object.')
        if name == 'microsoft_status':
            value = app.service.status()
        elif name in graph.TOOLS:
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
                logins = [x.strip().lower() for x in self.headers.get_all('Tailscale-User-Login') or []]
                if len(logins) == 1 and (logins[0] in config.owner_logins or (not config.owner_logins and '@' in logins[0])):
                    return host
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
            agent = (self.headers.get('X-Wizard-Agent') or 'main').strip().lower()
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', agent):
                raise Problem(400, 'invalid_agent', 'Invalid agent name.')
            return 'agent:' + agent

        def body(self):
            if (self.headers.get('Content-Type') or '').split(';')[0].strip().lower() != 'application/json':
                raise Problem(415, 'content_type', 'Use application/json.')
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                raise Problem(400, 'invalid_length', 'Invalid Content-Length.') from None
            if not 1 <= length <= MAX_BODY:
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
                if path.startswith('/api/'):
                    host = self.owner()
                    session = self.session()
                    if path == '/api/home':
                        return self.send_json(200, app.service.home(self.redirect_uri(host), session))
                    if path == '/api/own/status':
                        return self.send_json(200, app.service.own_status(session))
                    if path == '/api/quick/status':
                        return self.send_json(200, app.service.quick_status(session))
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
                    output = rpc(app, self.body(), caller)
                    return self.send_json(202 if output is None else 200, output or {})
                local = re.fullmatch(r'/v1/([a-z_]{3,40})', path)
                if local:
                    caller = self.box_caller()
                    self.host()
                    if local.group(1) not in graph.TOOLS:
                        raise Problem(404, 'not_found', 'Not found.')
                    return self.send_json(200, app.service.call_tool(caller, local.group(1), self.body()))
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

    return Handler


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


if __name__ == '__main__':
    config = Config()
    wait_for_storage(config.data_dir)
    server, _ = serve(config)
    server.serve_forever()
