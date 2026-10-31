"""QA preview: the real server in loopback dev mode. --fake uses the labelled FAKE Composio and Microsoft from tests and can seed
a connected state; without it, Composio and Microsoft are the real services (no key is ever stored by this script).

    python3 qa/preview.py --port 18895 [--fake --scenario fresh|ready]
"""
import argparse
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'tests')]
import server  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18895)
    parser.add_argument('--fake', action='store_true')
    parser.add_argument('--scenario', default='fresh', choices=['fresh', 'ready'])
    args = parser.parse_args()
    config = server.Config({'PORT': str(args.port), 'DATA_DIR': tempfile.mkdtemp(prefix='wizard-microsoft-qa-'),
                            'MICROSOFT_DEV_MODE': 'loopback-single-owner', 'WIZARD_SHELL_PATH': '/'})
    kwargs = {}
    if args.fake:
        from test_app import CLIENT_ID, GOOD, SECRET, FakeComposio, FakeGraph, FakeMicrosoft
        ms = FakeMicrosoft()
        kwargs = {'http': ms, 'composio_transport': FakeComposio(FakeGraph(email='alex@hotmail.com', oid='o2'))}
    app = server.App(config, **kwargs)
    if args.fake and args.scenario == 'ready':
        s = app.service
        s.add_key(GOOD, 'Personal')
        s.start_quick('seed', None, None)
        kwargs['composio_transport'].activate()
        s.quick_status('seed')
        s.save_client(CLIENT_ID, SECRET)
        out = s.start_own('seed', 'https://box.example/oauth/callback')
        import urllib.parse
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(out['authorization_url']).query))['state']
        ms.codes['seed'] = True
        account = s.finish_own(state, 'seed', 'seed')['account']
        s.set_label(account, 'Work')
        s.call_tool('agent:main', 'mail_search', {'account': 'alex@hotmail.com'})
    httpd = server.ThreadingHTTPServer(('127.0.0.1', args.port), server.handler_for(app))
    print(f'preview on http://127.0.0.1:{args.port}/ ({"FAKE" if args.fake else "real"} Composio and Microsoft)', flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == '__main__':
    main()
