"""Who an agent call speaks for, and per-agent mail levels (Off / Read-only / Draft / Send, Messenger's levels).

The bypass these close: an agent with a terminal on the box called /mcp directly and named itself with
X-Wizard-Agent, so it got whatever level it claimed. Now only the gateway's grant (`host:agent-service`) or a front
end's grant makes a name count. Real HTTP server; only Microsoft is fake.
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import threading

from test_app import FakeComposio, FakeGraph, FakeMicrosoft  # first: it puts app/ on the path

import agent_access
import server
from service import Service


def start(env):
    config = server.Config(env)
    app = server.App(config, http=FakeMicrosoft(), composio_transport=FakeComposio(FakeGraph()))
    srv, app = server.serve(config, app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, app


class Client:
    def __init__(self, port):
        self.port = port

    def send(self, method, path, body=None, headers=None, browser=False):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        raw = json.dumps(body).encode() if body is not None else None
        h = {'Host': f'127.0.0.1:{self.port}', **({'Content-Type': 'application/json'} if raw else {}), **(headers or {})}
        conn.request(method, path, raw, h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            return r.status, json.loads(data), None
        except ValueError:
            return r.status, data, None

GATEWAY_KEY = 'gateway-grant-key-0123456789abcdef'
MAIL_KEY = 'mail-grant-key-0123456789abcdef00'


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def rpc(method, params=None):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}


class AgentCallers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.grants = Path(self.dir) / 'grants.json'
        self.srv, self.app = start({'PORT': '0', 'DATA_DIR': self.dir + '/data', 'WIZARD_GRANTS_FILE': str(self.grants)})
        port = self.srv.server_address[1]
        config = self.app.config
        config.port = port
        config.local_hosts |= {f'127.0.0.1:{port}'}
        config.trusted_hosts |= {f'127.0.0.1:{port}'}
        self.c = Client(port)
        self.access = self.app.service.access
        self.access.set('draft_only')  # the owner's pick for the main agent

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def gateway_on_box(self, gateway=True):
        rows = [{'from': 'wizard-mail', 'sha256': digest(MAIL_KEY)}]
        if gateway:
            rows.append({'from': 'host:agent-service', 'sha256': digest(GATEWAY_KEY)})
        self.grants.write_text(json.dumps({'outgoing': {}, 'incoming': rows}))

    def call(self, tool, args=None, headers=None):
        return self.c.send('POST', '/mcp', rpc('tools/call', {'name': tool, 'arguments': args or {}}), headers, browser=False)

    def listed(self, headers):
        status, value, _ = self.c.send('POST', '/mcp', rpc('tools/list'), headers, browser=False)
        self.assertEqual(status, 200, value)
        return {t['name'] for t in value['result']['tools']}

    def code(self, response):
        status, value, _ = response
        if status != 200:
            return status, value['error']['code']
        return status, (value['result']['structuredContent'].get('error') or {}).get('code')

    @staticmethod
    def via_gateway(agent, label=None):
        return {'X-Wizard-Grant': GATEWAY_KEY, 'X-Wizard-Agent': agent, **({'X-Wizard-Agent-Label': label} if label else {})}

    def test_direct_calls_are_refused_once_the_gateway_has_its_grant(self):
        self.gateway_on_box()
        for headers in ({}, {'X-Wizard-Agent': 'secure'}, {'X-Wizard-Agent': 'hermes'}, {'X-Wizard-Agent': 'main'}):
            self.assertEqual(self.code(self.call('mail_search', {}, headers)), (401, 'gateway_grant_required'), headers)
            status, _, _ = self.c.send('POST', '/mcp', rpc('tools/list'), headers, browser=False)
            self.assertEqual(status, 401, headers)
            status, value, _ = self.c.send('POST', '/v1/mail_search', {}, headers, browser=False)
            self.assertEqual((status, value['error']['code']), (401, 'gateway_grant_required'), headers)
        self.assertEqual(self.code(self.call('mail_search', {}, {'X-Wizard-Grant': 'not-a-real-key-000000', 'X-Wizard-Agent': 'secure'})),
                         (401, 'grant_unknown'))

    def test_a_broken_grants_file_fails_closed(self):
        self.grants.write_text('{nope')
        self.assertEqual(self.code(self.call('mail_search')), (401, 'gateway_grant_required'))

    def test_each_agent_has_its_own_level_and_new_agents_start_at_no_access(self):
        self.gateway_on_box()
        secure = self.via_gateway('secure', 'Secure')
        # A new agent: No access, nothing to list, every mail tool refused.
        self.assertEqual(self.code(self.call('mail_search', {}, secure)), (200, 'agent_access_off'))
        self.assertFalse({'mail_search', 'mail_get', 'mail_draft', 'mail_update', 'mail_send'} & self.listed(secure))
        main = self.via_gateway('hermes', 'Oscar')
        self.assertEqual(self.listed(main) & set(agent_access.MAIL_TOOLS), {'mail_search', 'mail_get', 'mail_draft', 'mail_update'})
        # The owner sees both agents, by the names the gateway gave them, and picks Read-only for Secure.
        rows = {r['id']: r for r in self.access.view()['agents']}
        self.assertEqual((rows['main']['level'], rows['main']['label']), ('draft_only', 'Oscar'))
        self.assertEqual((rows['secure']['level'], rows['secure']['label']), ('no_access', 'Secure'))
        self.access.set_from({'mail': 'read_only', 'agent': 'secure'})
        self.assertEqual(self.listed(secure) & set(agent_access.MAIL_TOOLS), {'mail_search', 'mail_get'})
        self.assertNotEqual(self.code(self.call('mail_search', {}, secure))[1], 'agent_access_off')
        self.assertEqual(self.code(self.call('mail_draft', {'to': ['a@example.com'], 'body': 'x'}, secure)), (200, 'agent_access_denied'))
        self.assertEqual(self.access.level('agent:main'), 'draft_only')  # the main agent is unchanged
        # A name nobody verified cannot borrow Secure's level, and Secure cannot borrow the main agent's.
        self.access.set_from({'mail': 'no_access'})
        self.assertEqual(self.code(self.call('mail_search', {}, {'X-Wizard-Agent': 'secure'})), (401, 'gateway_grant_required'))
        self.assertEqual(self.code(self.call('mail_search', {}, main)), (200, 'agent_access_off'))

    def test_send_only_at_draft_and_send(self):
        self.gateway_on_box()
        main = self.via_gateway('hermes')
        self.assertEqual(self.code(self.call('mail_send', {'draft_id': 'd1'}, main)), (200, 'agent_send_refused'))
        self.access.set('draft_and_send')
        self.assertIn('mail_send', self.listed(main))
        # Past the level check: it now fails only because no account is connected.
        self.assertNotIn(self.code(self.call('mail_send', {'draft_id': 'd1'}, main))[1], ('agent_send_refused', 'agent_access_off'))

    def test_mail_relays_a_verified_agent_with_its_grant(self):
        self.gateway_on_box()
        self.call('mail_search', {}, self.via_gateway('secure'))  # the gateway has introduced Secure (No access)
        relayed = {'X-Wizard-Grant': MAIL_KEY, 'X-Wizard-Agent': 'secure'}
        status, value, _ = self.c.send('POST', '/v1/mail_search', {}, relayed, browser=False)
        self.assertEqual((status, value['error']['code']), (403, 'agent_access_off'))
        # Mail's grant alone is the owner's own tap in Mail: not limited by agent levels (it reaches the account check).
        status, value, _ = self.c.send('POST', '/v1/mail_send', {'draft_id': 'd1'}, {'X-Wizard-Grant': MAIL_KEY}, browser=False)
        self.assertNotIn(value['error']['code'], ('agent_send_refused', 'agent_access_off', 'gateway_grant_required'))

    def test_box_without_the_gateway_row_keeps_working_as_the_main_agent(self):
        # Older App Managers write no `host:agent-service` row: the gateway calls without a grant, as before.
        self.gateway_on_box(gateway=False)
        self.assertEqual(self.code(self.call('mail_search'))[1] in ('agent_access_off', 'agent_access_denied'), False)
        self.assertIn('mail_draft', self.listed({}))
        # Naming another agent gets the main agent's level, never that agent's.
        self.gateway_on_box()
        self.call('mail_search', {}, self.via_gateway('secure'))
        self.access.set_from({'mail': 'draft_and_send', 'agent': 'secure'})
        self.gateway_on_box(gateway=False)
        self.assertNotIn('mail_send', self.listed({'X-Wizard-Agent': 'secure'}))
        self.assertEqual(self.code(self.call('mail_send', {'draft_id': 'd1'}, {'X-Wizard-Agent': 'secure'})), (200, 'agent_send_refused'))


class Upgrade(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_existing_owner_keeps_the_shared_level_for_the_main_agent(self):
        (Path(self.dir) / 'agent-access.json').write_text(json.dumps({'version': 1, 'mail': 'read_only', 'calendar': 'can_edit'}))
        access = Service(self.dir, http=FakeMicrosoft()).access
        self.assertEqual((access.level('agent:main'), access.level('agent:hermes'), access.level('agent:secure')),
                         ('read_only', 'read_only', 'no_access'))
        self.assertEqual(access.calendar_level(), 'can_edit')
        saved = json.loads((Path(self.dir) / 'agent-access.json').read_text())
        self.assertEqual((saved['mail'], saved['agents']['main']['mail']), ('read_only', 'read_only'))

    def test_existing_owner_who_never_changed_it_keeps_draft_only(self):
        service = Service(self.dir, http=FakeMicrosoft())
        service.access.path.unlink()
        service.access.migrate(True)
        self.assertEqual(service.access.level(), 'draft_only')

    def test_fresh_install_starts_the_main_agent_at_no_access(self):
        self.assertEqual(Service(self.dir, http=FakeMicrosoft()).access.level(), 'no_access')


if __name__ == '__main__':
    unittest.main()
