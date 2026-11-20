"""Reading attachments (contract 1.6, wizard-mail#32): mail_get lists each file's `part`, mail_attachment returns one
file's bytes to the owner. Microsoft Graph is a fake (no network)."""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph  # noqa: E402
import service as service_mod  # noqa: E402
from test_app import GOOD, Base  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 40
PHOTO_ID = 'AAMkAGI2THVSAAA=/x-_+'
FILE = '#microsoft.graph.fileAttachment'


def photo_mail(graph_fake):
    """A message with a named photo in the body, a nameless cid: picture, a PDF and an attached message."""
    graph_fake.messages['AAMk2'] = {'id': 'AAMk2', 'conversationId': 'c2', 'subject': 'Photos', 'isRead': True, 'hasAttachments': True,
                                    'from': {'emailAddress': {'name': 'Miguel', 'address': 'miguel@example.com'}},
                                    'receivedDateTime': '2026-11-19T02:00:00Z', 'body': {'contentType': 'text', 'content': 'See'},
                                    'html': '<p>See</p><img src="cid:photo1@example"><img src="cid:logo@example">'}
    graph_fake.attachments['AAMk2'] = [
        {'@odata.type': FILE, 'id': PHOTO_ID, 'name': 'beach.png', 'contentType': 'image/png', 'size': len(PNG),
         'isInline': True, 'contentId': '<photo1@example>', 'contentBytes': base64.b64encode(PNG).decode()},
        {'@odata.type': FILE, 'id': 'LOGO', 'name': '', 'contentType': 'image/gif', 'size': 3,
         'isInline': True, 'contentId': 'logo@example', 'contentBytes': base64.b64encode(b'GIF').decode()},
        {'@odata.type': FILE, 'id': 'PDF1', 'name': 'menu.pdf', 'contentType': 'application/pdf', 'size': 4,
         'isInline': False, 'contentId': None, 'contentBytes': base64.b64encode(b'%PDF').decode()},
        {'@odata.type': '#microsoft.graph.itemAttachment', 'id': 'ITEM1', 'name': 'Fwd: older', 'contentType': None, 'size': 900,
         'isInline': False, 'contentId': None},
        {'@odata.type': FILE, 'id': 'BIG', 'name': 'video.mov', 'contentType': 'video/quicktime', 'size': 26 * 1024 * 1024,
         'isInline': False, 'contentId': None, 'contentBytes': 'AAAA'},
    ]


class ReadAttachments(Base):
    def setUp(self):
        super().setUp()
        self.s.add_key(GOOD)
        self.quick_connect()  # Quick connect: every Graph call goes through Composio's proxy
        self.g = self.composio.graph
        photo_mail(self.g)

    def test_mail_get_lists_parts_and_inline_pictures(self):
        out = self.s.call_tool('app:wizard-mail', 'mail_get', {'id': 'AAMk2', 'format': 'html'})['message']
        listed = [r for r in self.g.requests if r[1] == '/me/messages/AAMk2/attachments'][-1]
        self.assertEqual(listed[2]['$select'], 'id,name,contentType,size,isInline,contentId')
        self.assertEqual(out['attachments'], [
            {'name': 'beach.png', 'mime': 'image/png', 'size': len(PNG), 'part': PHOTO_ID, 'content_id': 'photo1@example', 'inline': True},
            {'name': 'menu.pdf', 'mime': 'application/pdf', 'size': 4, 'part': 'PDF1', 'content_id': None, 'inline': False},
            {'name': 'Fwd: older', 'mime': None, 'size': 900, 'part': 'ITEM1', 'content_id': None, 'inline': False},
            {'name': 'video.mov', 'mime': 'video/quicktime', 'size': 26 * 1024 * 1024, 'part': 'BIG', 'content_id': None, 'inline': False}])
        self.assertEqual(out['inline_parts'], [{'part': 'LOGO', 'mime': 'image/gif', 'size': 3, 'content_id': 'logo@example'}])
        self.assertIn('cid:photo1@example', out['body_html'])
        # Agents read the same list (as text); only fetching bytes is the owner's.
        agent = self.s.call_tool('agent:main', 'mail_get', {'id': 'AAMk2'})['message']
        self.assertEqual(agent['attachments'][0]['part'], PHOTO_ID)

    def test_inline_only_message_lists_its_pictures_in_html(self):
        # Outlook says hasAttachments: false when every attachment is inline; with format html the cid: pictures still list.
        self.g.messages['AAMk2']['hasAttachments'] = False
        out = self.s.call_tool('app:wizard-mail', 'mail_get', {'id': 'AAMk2', 'format': 'html'})['message']
        self.assertEqual(out['inline_parts'][0]['part'], 'LOGO')
        text = self.s.call_tool('app:wizard-mail', 'mail_get', {'id': 'AAMk2'})['message']
        self.assertEqual((text['attachments'], text['inline_parts']), ([], []))

    def test_mail_attachment_returns_the_bytes(self):
        out = self.s.call_tool('app:wizard-mail', 'mail_attachment', {'id': 'AAMk2', 'part': PHOTO_ID})
        self.assertEqual((out['contract'], out['source'], out['via']), ('wizard.mail-calendar/1', 'microsoft', 'composio'))
        self.assertEqual(set(out['attachment']), {'name', 'mime', 'size', 'base64'})
        self.assertEqual(base64.b64decode(out['attachment']['base64'], validate=True), PNG)
        self.assertEqual((out['attachment']['name'], out['attachment']['mime'], out['attachment']['size']), ('beach.png', 'image/png', len(PNG)))
        fetched = [r for r in self.g.requests if '/attachments/' in r[1]]
        self.assertEqual(fetched[0][1], '/me/messages/AAMk2/attachments/AAMkAGI2THVSAAA%3D%2Fx-_%2B')  # quoted; size checked first
        self.assertIn('size', fetched[0][2]['$select'])
        nameless = self.s.call_tool('app:wizard-mail', 'mail_attachment', {'id': 'AAMk2', 'part': 'LOGO'})['attachment']
        self.assertEqual(base64.b64decode(nameless['base64']), b'GIF')

    def test_too_big_unknown_part_and_not_a_file(self):
        cases = (({'part': 'BIG'}, 413, 'attachment_too_big'), ({'part': 'NOPE'}, 404, 'not_found'),
                 ({'part': 'ITEM1'}, 404, 'not_found'))
        for args, status, code in cases:
            with self.assertRaises(service_mod.Problem) as caught:
                self.s.call_tool('app:wizard-mail', 'mail_attachment', {'id': 'AAMk2', **args})
            self.assertEqual((caught.exception.status, caught.exception.code), (status, code), args)
        self.assertFalse(any(r[1].endswith('/BIG') and '$select' not in r[2] for r in self.g.requests))  # never downloaded
        for bad in ('', 'a' * 1025, '../x y', None, 5):
            with self.assertRaises(service_mod.Problem) as caught:
                self.s.call_tool('app:wizard-mail', 'mail_attachment', {'id': 'AAMk2', 'part': bad})
            self.assertEqual(caught.exception.code, 'invalid_arguments', bad)

    def test_agents_are_refused(self):
        before = len(self.g.requests)
        for level in ('read_only', 'draft_only'):
            self.s.access.set(level)
            for caller in ('agent:main', 'agent:wizard-mail'):
                with self.assertRaises(service_mod.Problem) as caught:
                    self.s.call_tool(caller, 'mail_attachment', {'id': 'AAMk2', 'part': PHOTO_ID})
                self.assertEqual((caught.exception.status, caught.exception.code), (403, 'owner_only'))
                self.assertEqual(caught.exception.message, 'Agents read mail as text; attachments open in Mail for the owner.')
        self.assertEqual(len(self.g.requests), before)  # nothing reached Microsoft
        self.assertNotIn('mail_attachment', self.s.status()['tools'])
        self.assertNotIn('mail_attachment', [t['name'] for t in graph.mcp_tools()])
        self.assertIn('mail_attachment', self.s.access.visible_tools('app:wizard-mail', list(graph.TOOLS)))


class AttachmentLimitOnOwnApp(unittest.TestCase):
    def test_bytes_checked_after_download_too(self):
        big = base64.b64encode(b'\0' * (graph.MAX_READ_BYTES + 1)).decode()

        def call(method, url, query=None, body=None, headers=None):  # Graph under-reports the size
            return {'@odata.type': FILE, 'id': 'X', 'name': 'a.bin', 'size': 10, **({} if query else {'contentBytes': big})}
        with self.assertRaises(graph.GraphError) as caught:
            graph.mail_attachment(call, {'id': 'M1', 'part': 'X'})
        self.assertEqual((caught.exception.status, caught.exception.code), (413, 'attachment_too_big'))


if __name__ == '__main__':
    unittest.main()
