"""Agent access to the owner's mail and calendar: owner-chosen levels, enforced here for every agent call.

Shared byte-for-byte by the Google and Microsoft apps (copy it after every change). Modelled on Wizard Messenger's
agent levels.

Mail: No access, Read-only and Draft only (recommended, the default). Levels for agents stop at Draft only, so nothing
here lets an agent send mail. The owner sends from Mail.

Calendar: No access, Read-only, Suggest only (recommended, the default) and Can edit. At Suggest only an agent's
create / update / delete / respond call changes nothing: it is kept here as a pending suggestion that Calendar shows
with Approve / Dismiss, and only the owner's tap there applies it.

Front ends (Mail, Calendar) prove they act for the owner with their App Manager grant (X-Wizard-Grant, hashed in this
app's grants file as `from: <app id>`), sent only for requests made by the owner's signed-in screen; such app callers
are not limited by the agent levels. Only the owner's signed-in screen (this app's page, or Mail / Calendar Settings
through this app's owner routes) can change a level.

Application defense in depth, not protection against host root: an agent with shell access to the box can read
the files that hold grants. Messenger documents the same boundary.
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
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

CAL_LEVELS = ('no_access', 'read_only', 'suggest_only', 'can_edit')
CAL_DEFAULT = 'suggest_only'
CAL_LABELS = {'no_access': 'No access', 'read_only': 'Read-only', 'suggest_only': 'Suggest only', 'can_edit': 'Can edit'}
CAL_WHAT = {'no_access': 'Agents can\'t see your calendar.',
            'read_only': 'Agents see your events. They can\'t change anything.',
            'suggest_only': 'Agents suggest changes. Nothing changes until you approve it in Calendar.',
            'can_edit': 'Agents add, change and delete events and answer invites for you.'}
CAL_RANK = {level: n for n, level in enumerate(CAL_LEVELS)}
# Calendar tools: reads need Read-only; writes become suggestions at Suggest only and apply at Can edit.
CAL_READ = ('calendar_list_events',)
CAL_WRITES = ('calendar_create_event', 'calendar_update_event', 'calendar_delete_event', 'calendar_respond')
# Pending suggestions: agents (Suggest only and up) may list them; approving and dismissing is the owner's tap.
SUGGESTION_TOOLS = ('calendar_suggestions', 'calendar_suggestion_approve', 'calendar_suggestion_dismiss')
OWNER_ONLY = ('mail_send', 'calendar_suggestion_approve', 'calendar_suggestion_dismiss')

APP_ID = re.compile(r'[a-z0-9][a-z0-9-]{1,40}')  # app ids only: a gateway row such as "host:agent-service" never matches
SHA256_HEX = re.compile(r'[0-9a-f]{64}')
KEY_RE = re.compile(r'[A-Za-z0-9_.~+/=-]{16,512}')
SUGGESTION_ID = re.compile(r'sg_[0-9a-f]{16}')
MAX_GRANTS_FILE = 256 * 1024
MAX_SUGGESTIONS = 50
MAX_SUGGESTION_ARGS = 16_000
SUGGESTION_DAYS = 30
SEND_REFUSED = ('Agents can\'t send mail on this Wizard (Mail > Settings > Agent access). Write it with mail_draft; '
                'the owner reads and sends it from Mail.')
APPROVE_REFUSED = 'Only the owner approves or dismisses suggestions, in Calendar.'


class AccessRefused(Exception):
    def __init__(self, status, code, message):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def is_agent(caller):
    """Agents (MCP, the box-only /v1 mirror without an app grant) as opposed to an app acting for the owner."""
    return not str(caller or '').startswith('app:')


class AgentAccess:
    """The levels live in DATA_DIR/agent-access.json (0600): {"version": 1, "mail": .., "calendar": ..}.
    Missing file or key = the default; unreadable or corrupt file = No access for both."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'agent-access.json'
        self.lock = threading.Lock()

    def _doc(self):
        """The saved levels, or None when the file can't be trusted."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return {}
        except OSError:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) and value.get('version') == 1 else None

    def level(self):
        doc = self._doc()
        if doc is None:
            return 'no_access'
        level = doc.get('mail', DEFAULT)
        return level if level in LEVELS else 'no_access'  # a corrupt file fails closed

    def calendar_level(self):
        doc = self._doc()
        if doc is None:
            return 'no_access'
        level = doc.get('calendar', CAL_DEFAULT)
        return level if level in CAL_LEVELS else 'no_access'

    def set(self, level=None, calendar=None):
        """Owner only. `level` is mail's level (kept for older callers); `calendar` the calendar level."""
        if level is None and calendar is None:
            raise AccessRefused(400, 'invalid_level', 'Choose a level.')
        if level is not None and level not in LEVELS:
            raise AccessRefused(400, 'invalid_level', 'Choose No access, Read-only or Draft only.')
        if calendar is not None and calendar not in CAL_LEVELS:
            raise AccessRefused(400, 'invalid_level', 'Choose No access, Read-only, Suggest only or Can edit.')
        with self.lock:
            doc = {'version': 1, 'mail': self.level() if level is None else level,
                   'calendar': self.calendar_level() if calendar is None else calendar}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name('.agent-access.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        return self.view()

    def set_from(self, body):
        """Owner route body: {mail: <level>} and/or {calendar: <level>}."""
        body = body if isinstance(body, dict) else {}
        return self.set(body.get('mail'), body.get('calendar'))

    def view(self):
        level, cal = self.level(), self.calendar_level()
        return {'mail': level, 'label': LABELS[level], 'what': WHAT[level], 'default': DEFAULT,
                'levels': [{'id': x, 'label': LABELS[x], 'what': WHAT[x], 'recommended': x == DEFAULT} for x in LEVELS],
                'calendar': cal, 'calendar_label': CAL_LABELS[cal], 'calendar_what': CAL_WHAT[cal], 'calendar_default': CAL_DEFAULT,
                'calendar_levels': [{'id': x, 'label': CAL_LABELS[x], 'what': CAL_WHAT[x], 'recommended': x == CAL_DEFAULT}
                                    for x in CAL_LEVELS]}

    def agent_view(self):
        """What an agent is told in its status: its mail level and what it means."""
        level = self.level()
        return {'level': level, 'label': LABELS[level], 'what': WHAT[level], 'can_send': False,
                'change': 'Only the owner changes this, in Mail > Settings > Agent access.'}

    def calendar_agent_view(self):
        level = self.calendar_level()
        out = {'level': level, 'label': CAL_LABELS[level], 'what': CAL_WHAT[level],
               'change': 'Only the owner changes this, in Calendar > Settings > Agent access.'}
        if level == 'suggest_only':
            out['how'] = ('calendar_create_event, calendar_update_event, calendar_delete_event and calendar_respond save a '
                          'suggestion and change nothing. Tell the owner it is waiting in Calendar.')
        return out

    def check(self, caller, tool):
        """Raise AccessRefused when an agent may not use this tool. Returns 'suggest' when an agent's calendar change
        must become a pending suggestion instead of being applied; None to go ahead."""
        if not is_agent(caller):
            return None
        if tool in MAIL_TOOLS:
            need = MAIL_TOOLS[tool]
            if need is None:
                raise AccessRefused(403, 'agent_send_refused', SEND_REFUSED)
            level = self.level()
            if RANK[level] < RANK[need]:
                raise AccessRefused(403, 'agent_access_denied', f'Agent access to mail is {LABELS[level]}: {WHAT[level]} '
                                                                 'The owner changes it in Mail > Settings > Agent access.')
            return None
        if tool in ('calendar_suggestion_approve', 'calendar_suggestion_dismiss'):
            raise AccessRefused(403, 'agent_approve_refused', APPROVE_REFUSED)
        if tool in CAL_READ or tool in CAL_WRITES or tool in SUGGESTION_TOOLS:
            level = self.calendar_level()
            need = 'suggest_only' if tool in SUGGESTION_TOOLS or tool in CAL_WRITES else 'read_only'
            if CAL_RANK[level] < CAL_RANK[need]:
                raise AccessRefused(403, 'agent_access_denied', f'Agent access to the calendar is {CAL_LABELS[level]}: '
                                                                 f'{CAL_WHAT[level]} The owner changes it in Calendar > Settings > Agent access.')
            if tool in CAL_WRITES and level == 'suggest_only':
                return 'suggest'
        return None

    def visible_tools(self, caller, names):
        """Tools listed to this caller: agents never see mail_send or the owner's approve / dismiss."""
        return [n for n in names if not (is_agent(caller) and n in OWNER_ONLY)]

    def gog_flags(self, caller, root):
        """Extra gog flags for an agent's gmail or calendar command (the Google app's google_run), so gog itself
        refuses the rest."""
        if not is_agent(caller) or root not in ('gmail', 'calendar'):
            return []
        if root == 'calendar':
            level = self.calendar_level()
            if level == 'no_access':
                raise AccessRefused(403, 'agent_access_denied', f'Agent access to the calendar is No access: {CAL_WHAT["no_access"]}')
            if level == 'can_edit':
                return []
            # Read-only and Suggest only: gog only reads. Suggestions go through calendar_create_event and friends.
            off = ['calendar.' + c for c in ('subscribe', 'unsubscribe', 'create-calendar', 'delete-calendar', 'create', 'update',
                                             'move', 'delete', 'respond', 'propose-time', 'focus-time', 'out-of-office',
                                             'working-location', 'acl')]
            return ['--readonly', '--disable-commands=' + ','.join(off)]
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


class Suggestions:
    """Pending calendar changes agents suggested at Suggest only (DATA_DIR/calendar-suggestions.json, 0600). Nothing in
    here has touched the calendar. The owner approves (the change is applied through the owner path) or dismisses."""

    def __init__(self, data_dir, clock=time.time):
        self.path = Path(data_dir) / 'calendar-suggestions.json'
        self.lock = threading.Lock()
        self.clock = clock

    def _load(self):
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        items = doc.get('items') if isinstance(doc, dict) and doc.get('version') == 1 else None
        cutoff = self.clock() - SUGGESTION_DAYS * 86400
        return [i for i in (items if isinstance(items, list) else []) if isinstance(i, dict)
                and SUGGESTION_ID.fullmatch(str(i.get('id', ''))) and i.get('tool') in CAL_WRITES
                and isinstance(i.get('args'), dict) and isinstance(i.get('at'), int) and i['at'] >= cutoff]

    def _save(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name('.calendar-suggestions.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'version': 1, 'items': items}, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @staticmethod
    def public(item):
        return {'id': item['id'], 'tool': item['tool'], 'action': item['tool'].replace('calendar_', '').replace('_event', ''),
                'account': item.get('account', ''), 'args': item.get('args', {}), 'agent': item.get('agent', ''),
                'created': datetime.datetime.fromtimestamp(item.get('at', 0), datetime.timezone.utc).isoformat(timespec='seconds')}

    def add(self, caller, tool, args, account):
        if tool not in CAL_WRITES:
            raise AccessRefused(400, 'invalid_suggestion', 'Only calendar changes can be suggested.')
        clean = {k: v for k, v in (args or {}).items() if isinstance(k, str) and len(k) <= 40}
        if len(json.dumps(clean)) > MAX_SUGGESTION_ARGS:
            raise AccessRefused(413, 'suggestion_too_large', 'That suggestion is too large.')
        with self.lock:
            items = self._load()
            if len(items) >= MAX_SUGGESTIONS:
                raise AccessRefused(429, 'too_many_suggestions', f'{MAX_SUGGESTIONS} suggestions are already waiting for the owner '
                                                                 'in Calendar. Wait until they are approved or dismissed.')
            item = {'id': 'sg_' + secrets.token_hex(8), 'tool': tool, 'account': str(account or '')[:320], 'args': clean,
                    'agent': str(caller or '')[:80], 'at': int(self.clock())}
            items.append(item)
            self._save(items)
        return self.public(item)

    def list(self):
        with self.lock:
            return [self.public(i) for i in self._load()]

    def get(self, sid):
        if not isinstance(sid, str) or not SUGGESTION_ID.fullmatch(sid):
            raise AccessRefused(400, 'invalid_suggestion', 'Unknown suggestion.')
        with self.lock:
            found = next((i for i in self._load() if i['id'] == sid), None)
        if not found:
            raise AccessRefused(404, 'suggestion_not_found', 'That suggestion was already approved or dismissed.')
        return found

    def drop(self, sid):
        with self.lock:
            items = self._load()
            kept = [i for i in items if i['id'] != sid]
            if len(kept) != len(items):
                self._save(kept)
            return len(kept) != len(items)


# MCP descriptions for the suggestion tools (the same in every source app). Approve / dismiss are never listed to agents.
SUGGESTION_MCP_TOOLS = [
    {'name': 'calendar_suggestions', 'description': 'Calendar changes agents suggested that are waiting for the owner (Suggest only). '
                                                    'The owner approves or dismisses them in Calendar.',
     'annotations': {'readOnlyHint': True}, 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'calendar_suggestion_approve', 'description': 'Owner only (Calendar\'s Approve): apply one pending suggestion.',
     'annotations': {'destructiveHint': True},
     'inputSchema': {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'string'}}}},
    {'name': 'calendar_suggestion_dismiss', 'description': 'Owner only (Calendar\'s Dismiss): drop one pending suggestion.',
     'inputSchema': {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'string'}}}},
]


class DryRun(Exception):
    """Raised by dry_call: the tool checked its arguments and was about to call the provider."""


def dry_call(*_args, **_kw):
    raise DryRun()


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
