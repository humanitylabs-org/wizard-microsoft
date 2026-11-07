"""Outlook drafts with attachments (contract 1.2), against a recording fake of Microsoft Graph (no network)."""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))
import graph  # noqa: E402

G = graph.GRAPH
PDF = b'%PDF-1.4 tiny\n%%EOF\n'


class Recorder:
    def __init__(self):
        self.calls = []
        self.files = {'D1': [{'id': 'A1', 'name': 'plan.pdf', 'contentType': 'application/pdf', 'size': 900},
                             {'id': 'A2', 'name': 'notes.txt', 'contentType': 'text/plain', 'size': 9}],
                      'F1': [{'id': 'A3', 'name': 'invoice.pdf', 'contentType': 'application/pdf', 'size': 500}]}

    def __call__(self, method, url, query=None, body=None, headers=None):
        self.calls.append((method, url[len(G):], body))
        path = url[len(G):]
        parts = path.split('/')
        if path.endswith('/attachments') and method == 'GET':
            return {'value': list(self.files.get(parts[3], []))}
        if path.endswith('/attachments') and method == 'POST':
            self.files.setdefault(parts[3], []).append({'id': 'N', 'name': body['name'], 'contentType': body['contentType'],
                                                        'size': len(base64.b64decode(body['contentBytes']))})
            return body
        if '/attachments/' in path and method == 'DELETE':
            self.files[parts[3]] = [a for a in self.files[parts[3]] if a['id'] != parts[5]]
            return {}
        if path.endswith('/createForward'):
            return {'id': 'F1', 'conversationId': 'c9'}
        if method == 'PATCH':
            return {'id': parts[3], 'conversationId': 'c1', 'subject': (body or {}).get('subject', 'Old'),
                    'toRecipients': (body or {}).get('toRecipients', [{'emailAddress': {'address': 'jay@example.com'}}])}
        if path == '/me/messages' and method == 'POST':
            return {'id': 'D2', 'conversationId': 'c2', **body}
        raise AssertionError(f'unexpected {method} {path}')


class Attachments(unittest.TestCase):
    def test_new_draft_uploads_files(self):
        g = Recorder()
        out = graph.mail_draft(g, {'to': ['a@example.com'], 'subject': 'Hi', 'body': 'See attached',
                                   'attachments': [{'name': 'a.pdf', 'base64': base64.b64encode(PDF).decode()}]})
        posted = [c for c in g.calls if c[0] == 'POST' and c[1].endswith('/attachments')]
        self.assertEqual(posted[0][1], '/me/messages/D2/attachments')
        self.assertEqual(posted[0][2]['@odata.type'], '#microsoft.graph.fileAttachment')
        self.assertEqual(base64.b64decode(posted[0][2]['contentBytes']), PDF)
        self.assertEqual(posted[0][2]['contentType'], 'application/pdf')
        self.assertEqual(out['draft']['attachments'], [{'name': 'a.pdf', 'mime': 'application/pdf', 'size': len(PDF)}])

    def test_edit_in_place_keeps_and_removes(self):
        g = Recorder()
        out = graph.mail_draft(g, {'draft_id': 'D1', 'body': 'New text', 'remove_attachments': ['notes.txt']})
        self.assertEqual(g.calls[0], ('PATCH', '/me/messages/D1', {'body': {'contentType': 'Text', 'content': 'New text'}}))
        self.assertIn(('DELETE', '/me/messages/D1/attachments/A2', None), g.calls)
        self.assertFalse(any(c[0] == 'POST' for c in g.calls))  # no new draft
        self.assertEqual([a['name'] for a in out['draft']['attachments']], ['plan.pdf'])
        self.assertTrue(out['updated'])

    def test_forward_carries_files(self):
        g = Recorder()
        out = graph.mail_draft(g, {'to': ['b@example.com'], 'subject': 'Fwd: Invoice', 'body': 'fyi', 'attachments_from': 'M9'})
        self.assertEqual(g.calls[0][:2], ('POST', '/me/messages/M9/createForward'))
        self.assertEqual(g.calls[1][:2], ('PATCH', '/me/messages/F1'))
        self.assertEqual([a['name'] for a in out['draft']['attachments']], ['invoice.pdf'])

    def test_limits(self):
        for name in ('../x.pdf', 'a/b', '..', ''):
            with self.assertRaises(graph.BadArgs):
                graph.mail_draft(Recorder(), {'to': ['a@example.com'], 'subject': 's', 'attachments': [{'name': name, 'base64': 'QUFB'}]})
        big = base64.b64encode(b'\0' * (3 * 1024 * 1024 + 1)).decode()
        with self.assertRaises(graph.BadArgs) as caught:
            graph.mail_draft(Recorder(), {'to': ['a@example.com'], 'subject': 's', 'attachments': [{'name': 'big.bin', 'base64': big}]})
        self.assertIn('3 MB', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
