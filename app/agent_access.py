"""Agent access to the owner's mail: one owner-chosen level, enforced here for every agent call.

Shared byte-for-byte by the Google and Microsoft apps (copy it after every change). Modelled on Wizard Messenger's
agent levels: No access, Read-only and Draft only (recommended, the default). Levels for agents stop at Draft only,
so nothing here lets an agent send mail. The owner sends from Mail: Mail's server calls this app with its App
Manager grant (X-Wizard-Grant, hashed in this app's grants file as `from: wizard-mail`) only for requests made by
the owner's signed-in screen, and such app callers are not limited by the agent level. Only the owner's signed-in
screen (this app's page, or Mail's Settings through this app's owner routes) can change the level.

Application defense in depth, not protection against host root: an agent with shell access to the box can read
the files that hold grants. Messenger documents the same boundary.
"""
import hashlib
import hmac
import json
import os
import re
import stat
import threading
from pathlib import Path

LEVELS = ('no_access', 'read_only', 'draft_only')
DEFAULT = 'draft_only'
LABELS = {'no_access': 'No access', 'read_only': 'Read-only', 'draft_only': 'Draft only'}
WHAT = {'no_access': 'Agents can\'t read your mail.',
        'read_only': 'Agents read and search your mail. They can\'t draft or send.',
        'draft_only': 'Agents read, sort and write drafts. Only you send, from Mail.'}
RANK = {level: n for n, level in enumerate(LEVELS)}
# What each mail tool needs. None: agents never get it (sending is the owner's tap in Mail).
MAIL_TOOLS = {'mail_search': 'read_only', 'mail_get': 'read_only', 'mail_update': 'draft_only', 'mail_draft': 'draft_only',
              'mail_send': None}
APP_ID = re.compile(r'[a-z0-9][a-z0-9-]{1,40}')  # app ids only: a gateway row such as "host:agent-service" never matches
SHA256_HEX = re.compile(r'[0-9a-f]{64}')
KEY_RE = re.compile(r'[A-Za-z0-9_.~+/=-]{16,512}')
MAX_GRANTS_FILE = 256 * 1024
SEND_REFUSED = ('Agents can\'t send mail on this Wizard (Mail > Settings > Agent access). Write it with mail_draft; '
                'the owner reads and sends it from Mail.')


class AccessRefused(Exception):
    def __init__(self, status, code, message):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def is_agent(caller):
    """Agents (MCP, the box-only /v1 mirror without an app grant) as opposed to an app acting for the owner."""
    return not str(caller or '').startswith('app:')


class AgentAccess:
    """The level lives in DATA_DIR/agent-access.json (0600). Missing = Draft only; unreadable = No access."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'agent-access.json'
        self.lock = threading.Lock()

    def level(self):
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return DEFAULT
        except OSError:
            return 'no_access'
        try:
            value = json.loads(raw)
            level = value.get('mail') if isinstance(value, dict) and value.get('version') == 1 else None
        except ValueError:
            level = None
        return level if level in LEVELS else 'no_access'  # a corrupt file fails closed

    def set(self, level):
        if level not in LEVELS:
            raise AccessRefused(400, 'invalid_level', 'Choose No access, Read-only or Draft only.')
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name('.agent-access.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'version': 1, 'mail': level}, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        return self.view()

    def view(self):
        level = self.level()
        return {'mail': level, 'label': LABELS[level], 'what': WHAT[level], 'default': DEFAULT,
                'levels': [{'id': x, 'label': LABELS[x], 'what': WHAT[x], 'recommended': x == DEFAULT} for x in LEVELS]}

    def agent_view(self):
        """What an agent is told in its status: its level and what it means."""
        level = self.level()
        return {'level': level, 'label': LABELS[level], 'what': WHAT[level], 'can_send': False,
                'change': 'Only the owner changes this, in Mail > Settings > Agent access.'}

    def check(self, caller, tool):
        """Raise AccessRefused when an agent may not use this mail tool. Calendar tools are not limited here."""
        if not is_agent(caller) or tool not in MAIL_TOOLS:
            return
        need = MAIL_TOOLS[tool]
        if need is None:
            raise AccessRefused(403, 'agent_send_refused', SEND_REFUSED)
        level = self.level()
        if RANK[level] < RANK[need]:
            raise AccessRefused(403, 'agent_access_denied', f'Agent access to mail is {LABELS[level]}: {WHAT[level]} '
                                                             'The owner changes it in Mail > Settings > Agent access.')

    def visible_tools(self, caller, names):
        """Tools listed to this caller: agents never see mail_send."""
        return [n for n in names if not (is_agent(caller) and n in MAIL_TOOLS and MAIL_TOOLS[n] is None)]

    def gog_flags(self, caller, root):
        """Extra gog flags for an agent's gmail command (the Google app's google_run), so gog itself refuses the rest."""
        if not is_agent(caller) or root != 'gmail':
            return []
        level = self.level()
        if level == 'no_access':
            raise AccessRefused(403, 'agent_access_denied', f'Agent access to mail is No access: {WHAT["no_access"]}')
        # Sending, replying, forwarding and auto-replies are refused by gog's own agent-safety switch; settings
        # (forwarding, filters, vacation replies) and open tracking could send mail indirectly, so they are off too.
        off = ['gmail.settings', 'gmail.track', 'gmail.autoreply', 'gmail.batch.delete']
        flags = ['--gmail-no-send']
        if level == 'read_only':
            flags.insert(0, '--readonly')  # gog blocks mutating requests at runtime; the list below names them as well
            off += ['gmail.send', 'gmail.reply', 'gmail.reply-all', 'gmail.forward', 'gmail.import', 'gmail.drafts', 'gmail.batch',
                    'gmail.archive', 'gmail.mark-read', 'gmail.unread', 'gmail.trash', 'gmail.thread.modify', 'gmail.messages.modify',
                    'gmail.labels.create', 'gmail.labels.rename', 'gmail.labels.style', 'gmail.labels.modify', 'gmail.labels.delete']
        return flags + ['--disable-commands=' + ','.join(off)]


class AppGrants:
    """Which Wizard app is calling: `X-Wizard-Grant` hashed and looked up in the App Manager's grants file
    (`incoming: [{from: <app id>, sha256}]`). Only app ids match, never a gateway row."""

    def __init__(self, path):
        self.path = Path(path) if path else None

    def _rows(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_GRANTS_FILE:
                return []
            data = os.read(fd, MAX_GRANTS_FILE + 1)
        finally:
            os.close(fd)
        doc = json.loads(data)
        rows = doc.get('incoming') if isinstance(doc, dict) else None
        return [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]

    def caller(self, token):
        if not self.path or not isinstance(token, str) or not KEY_RE.fullmatch(token):
            return None
        try:
            rows = self._rows()
        except Exception:  # missing, unreadable or malformed: no app is recognised
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        found = None
        for row in rows:
            name, want = str(row.get('from', '')), str(row.get('sha256', ''))
            if APP_ID.fullmatch(name) and SHA256_HEX.fullmatch(want) and hmac.compare_digest(want, digest):
                found = name
        return found
