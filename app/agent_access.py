"""Agent access to the owner's mail and calendar: owner-chosen levels, enforced here for every agent call.

Shared byte-for-byte by the Google and Microsoft apps (copy it after every change). Modelled on Wizard Messenger's
agent levels (wizard-messenger `wizard_agents.py`), with the same names and wording.

Mail: one level per agent, as in Messenger: No access, Read-only, Draft-only (recommended) and Draft-and-send. An agent
the owner has not picked a level for is No access. On the update that brought per-agent levels the main agent keeps
the one level every agent had before (Draft-only when the owner never changed it), so nothing changes for an existing
owner until he picks again; on a fresh install the main agent starts at No access like every other agent.

Calendar: one level for every agent: No access, Read-only, Suggest only (recommended, the default) and Can edit. At
Suggest only an agent's create / update / delete / respond call changes nothing: it is kept here as a pending
suggestion that Calendar shows with Approve / Dismiss, and only the owner's tap there applies it.

Who is calling (`resolve_caller`), never from what the caller says about itself:
- The Agent Service gateway proves itself with its grant key (`X-Wizard-Grant`, hashed in this app's grants file as
  `from: host:agent-service`, wizard-os spec/PERMISSIONS.md layer 2). Only then is `X-Wizard-Agent` believed; the
  gateway sets it from each agent's own gateway token.
- Front ends (Mail, Calendar) prove themselves with their App Manager grant (`from: <app id>`). Without `X-Wizard-Agent`
  the request is the owner's own tap in that app and is not limited by agent levels; with it, the app is relaying an
  agent it has itself verified through the gateway, and that agent's level applies.
- Anything else on this box (a bare call, or a self-declared `X-Wizard-Agent`) is refused once the gateway's row is in
  the grants file. Until the box has that row (older App Managers do not write it) a bare call is the main agent, at
  the main agent's level, whatever name it gives: no caller can borrow another agent's level.

Only the owner's signed-in screen (this app's page, or Mail / Calendar Settings through this app's owner routes) can
change a level. Application defense in depth, not protection against host root: an agent with shell access to the
box's Wizard files can read the keys. Messenger documents the same boundary.
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
from urllib.parse import unquote

LEVELS = ('no_access', 'read_only', 'draft_only', 'draft_and_send')
DEFAULT = 'draft_only'      # recommended; and the main agent's level when an existing owner never changed it
NEW_AGENT = 'no_access'     # every agent the owner has not picked a level for
LABELS = {'no_access': 'No access', 'read_only': 'Read-only', 'draft_only': 'Draft-only', 'draft_and_send': 'Draft-and-send'}
# Messenger's wording (Settings > Agent access), for mail.
WHAT = {'no_access': 'Can\'t read your mail.',
        'read_only': 'Reads mail. Can\'t draft.',
        'draft_only': 'Reads and drafts. Only you send.',
        'draft_and_send': 'Reads and sends without your review.'}
RANK = {level: n for n, level in enumerate(LEVELS)}
# What each mail tool needs.
MAIL_TOOLS = {'mail_search': 'read_only', 'mail_get': 'read_only', 'mail_update': 'draft_only', 'mail_draft': 'draft_only',
              'mail_send': 'draft_and_send'}

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
OWNER_ONLY = ('calendar_suggestion_approve', 'calendar_suggestion_dismiss')

APP_ID = re.compile(r'[a-z0-9][a-z0-9-]{1,40}')  # app ids only: a gateway row such as "host:agent-service" never matches
GATEWAY = 'host:agent-service'  # outside the app-ID namespace: no app grant can produce this row
MAIN = 'main'
MAIN_NAMES = ('main', 'hermes')  # the gateway calls the main agent "hermes"; older callers said "main"
AGENT_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
MAX_AGENTS = 32
SHA256_HEX = re.compile(r'[0-9a-f]{64}')
KEY_RE = re.compile(r'[A-Za-z0-9_.~+/=-]{16,512}')
SUGGESTION_ID = re.compile(r'sg_[0-9a-f]{16}')
MAX_GRANTS_FILE = 256 * 1024
MAX_SUGGESTIONS = 50
MAX_SUGGESTION_ARGS = 16_000
SUGGESTION_DAYS = 30
SEND_REFUSED = ('{name} is {label} for mail (Mail > Settings > Agent access), so it can\'t send. Write it with '
                'mail_draft; the owner reads and sends it from Mail.')
APPROVE_REFUSED = 'Only the owner approves or dismisses suggestions, in Calendar.'


class AccessRefused(Exception):
    def __init__(self, status, code, message):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def is_agent(caller):
    """Agents (MCP, the box-only /v1 mirror, or an agent relayed by Mail / Calendar) as opposed to an app acting for
    the owner."""
    return not str(caller or '').startswith('app:')


def agent_id(caller):
    """'agent:<id>' -> '<id>' with the main agent's names folded into 'main'."""
    ident = str(caller or '').split(':', 1)[-1] if str(caller or '').startswith('agent:') else ''
    return MAIN if ident in MAIN_NAMES else ident


def clean_label(value, fallback):
    try:
        text = unquote(str(value or ''))
    except Exception:  # noqa: BLE001
        text = ''
    text = ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', text).split())[:60]
    return text or fallback


class AgentAccess:
    """The levels live in DATA_DIR/agent-access.json (0600):
    {"version": 1, "mail": <main agent's level, kept for older readers>, "calendar": ..,
     "agents": {"<id>": {"mail": <level>, "label": "..", "last_used": <ms>}}}.
    No "agents" yet (an older version wrote the file) = the main agent at the shared "mail" level, every other agent
    No access. Unreadable or corrupt file = No access for everyone."""

    def __init__(self, data_dir, clock=time.time):
        self.path = Path(data_dir) / 'agent-access.json'
        self.lock = threading.RLock()
        self.clock = clock

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
        if not isinstance(value, dict) or value.get('version') != 1:
            return None
        if 'agents' in value and not isinstance(value['agents'], dict):
            return None
        return value

    def _write(self, doc):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name('.agent-access.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(doc, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def migrate(self, existing):
        """Once, on the first start with per-agent levels. `existing`: this app already had accounts or a client (an
        update, not a fresh install). The main agent keeps the level every agent had; a fresh install starts it at No
        access. A corrupt file is left alone (everyone stays No access until the owner picks)."""
        with self.lock:
            doc = self._doc()
            if doc is None or 'agents' in doc:
                return
            had_file = self.path.exists()
            main = doc.get('mail', DEFAULT) if (had_file or existing) else NEW_AGENT
            main = main if main in LEVELS else NEW_AGENT
            self._write({'version': 1, 'mail': main, 'calendar': doc.get('calendar', CAL_DEFAULT),
                         'agents': {MAIN: {'mail': main, 'label': '', 'last_used': None}}})

    def _agents(self, doc):
        rows = doc.get('agents')
        if rows is None:  # not migrated: the main agent has the one shared level
            legacy = doc.get('mail', DEFAULT)
            return {MAIN: {'mail': legacy if legacy in LEVELS else NEW_AGENT, 'label': '', 'last_used': None}}
        return {k: v for k, v in rows.items() if AGENT_ID.fullmatch(str(k)) and isinstance(v, dict)}

    def level(self, caller=None):
        """The mail level of one agent (default: the main agent)."""
        doc = self._doc()
        if doc is None:
            return 'no_access'
        ident = agent_id(caller) if caller else MAIN
        level = self._agents(doc).get(ident, {}).get('mail', NEW_AGENT)
        return level if level in LEVELS else 'no_access'  # a corrupt entry fails closed

    def calendar_level(self):
        doc = self._doc()
        if doc is None:
            return 'no_access'
        level = doc.get('calendar', CAL_DEFAULT)
        return level if level in CAL_LEVELS else 'no_access'

    def seen(self, caller, label=None):
        """A verified agent called: remember it (at No access when new) so the owner sees it in Settings."""
        ident = agent_id(caller)
        if not AGENT_ID.fullmatch(ident or ''):
            return
        with self.lock:
            doc = self._doc()
            if doc is None:
                return
            rows = self._agents(doc)
            now = int(self.clock() * 1000)
            row = rows.get(ident)
            fresh = clean_label(label, '') if label else ''
            if row is None:
                if len(rows) >= MAX_AGENTS:
                    return
                row = rows[ident] = {'mail': NEW_AGENT, 'label': fresh, 'last_used': now}
            elif (fresh and row.get('label') != fresh) or not isinstance(row.get('last_used'), int) or now - row['last_used'] > 60000:
                row['label'] = fresh or row.get('label', '')
                row['last_used'] = now
            else:
                return
            try:
                self._write({**doc, 'version': 1, 'mail': rows.get(MAIN, {}).get('mail', NEW_AGENT), 'agents': rows})
            except OSError:
                pass

    def set(self, level=None, calendar=None, agent=None):
        """Owner only. `level` is one agent's mail level (`agent`, default the main agent: an older Mail sends a bare
        level for "agents"); `calendar` the calendar level."""
        if level is None and calendar is None:
            raise AccessRefused(400, 'invalid_level', 'Choose a level.')
        if level is not None and level not in LEVELS:
            raise AccessRefused(400, 'invalid_level', 'Choose No access, Read-only, Draft-only or Draft-and-send.')
        if calendar is not None and calendar not in CAL_LEVELS:
            raise AccessRefused(400, 'invalid_level', 'Choose No access, Read-only, Suggest only or Can edit.')
        ident = MAIN if agent in (None, '') or agent in MAIN_NAMES else str(agent)
        with self.lock:
            doc = self._doc()
            if doc is None:
                doc = {'agents': {}}  # the owner's pick replaces a corrupt file; nobody else gains a level
            rows = self._agents(doc)
            if level is not None:
                if ident not in rows and ident != MAIN:
                    raise AccessRefused(404, 'not_found', 'No such agent.')
                rows.setdefault(ident, {'label': '', 'last_used': None})['mail'] = level
            self._write({'version': 1, 'mail': rows.get(MAIN, {}).get('mail', NEW_AGENT),
                         'calendar': self.calendar_level() if calendar is None else calendar, 'agents': rows})
        return self.view()

    def set_from(self, body):
        """Owner route body: {mail: <level>, agent?: <id>} and/or {calendar: <level>}."""
        body = body if isinstance(body, dict) else {}
        return self.set(body.get('mail'), body.get('calendar'), body.get('agent'))

    def agents_view(self):
        doc = self._doc()
        rows = self._agents(doc) if doc is not None else {}
        rows.setdefault(MAIN, {'mail': NEW_AGENT, 'label': '', 'last_used': None})
        out = []
        for ident, row in rows.items():
            level = row.get('mail') if row.get('mail') in LEVELS else 'no_access'
            out.append({'id': ident, 'label': row.get('label') or ('Your Wizard agent' if ident == MAIN else ident),
                        'level': level, 'level_label': LABELS[level], 'main': ident == MAIN,
                        'last_used': row.get('last_used') if isinstance(row.get('last_used'), int) else None})
        return sorted(out, key=lambda r: (not r['main'], r['label'].casefold()))

    def view(self):
        level, cal = self.level(), self.calendar_level()
        return {'mail': level, 'label': LABELS[level], 'what': WHAT[level], 'default': DEFAULT, 'per_agent': True,
                'agents': self.agents_view(),
                'levels': [{'id': x, 'label': LABELS[x], 'what': WHAT[x], 'recommended': x == DEFAULT} for x in LEVELS],
                'calendar': cal, 'calendar_label': CAL_LABELS[cal], 'calendar_what': CAL_WHAT[cal], 'calendar_default': CAL_DEFAULT,
                'calendar_levels': [{'id': x, 'label': CAL_LABELS[x], 'what': CAL_WHAT[x], 'recommended': x == CAL_DEFAULT}
                                    for x in CAL_LEVELS]}

    def agent_view(self, caller='agent:main'):
        """What an agent is told in its status: its own mail level and what it means."""
        level = self.level(caller)
        return {'level': level, 'label': LABELS[level], 'what': WHAT[level], 'can_send': level == 'draft_and_send',
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
            level = self.level(caller)
            if RANK[level] < RANK[need]:
                if tool == 'mail_send' and level != 'no_access':
                    raise AccessRefused(403, 'agent_send_refused', SEND_REFUSED.format(name='This agent', label=LABELS[level]))
                code = 'agent_access_off' if level == 'no_access' else 'agent_access_denied'
                raise AccessRefused(403, code, f'The owner set this agent to {LABELS[level]} for mail: {WHAT[level]} '
                                               f'This tool needs {LABELS[need]} or higher. Only the owner can change it, '
                                               'in Mail > Settings > Agent access.')
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
        """Tools listed to this caller: an agent sees only the mail tools its own level allows, and never the owner's
        approve / dismiss."""
        if not is_agent(caller):
            return list(names)
        level = RANK[self.level(caller)]
        return [n for n in names if n not in OWNER_ONLY and (n not in MAIL_TOOLS or level >= RANK[MAIL_TOOLS[n]])]

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
        level = self.level(caller)
        if level == 'no_access':
            raise AccessRefused(403, 'agent_access_off', f'The owner set this agent to No access for mail: {WHAT["no_access"]}')
        # Settings (forwarding, filters, vacation replies) and open tracking could send mail indirectly: always off.
        off = ['gmail.settings', 'gmail.track', 'gmail.autoreply', 'gmail.batch.delete']
        if level == 'draft_and_send':
            return ['--disable-commands=' + ','.join(off)]
        # Below Draft-and-send, sending, replying and forwarding are refused by gog's own agent-safety switch.
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


class GatewayGrant:
    """The Agent Service gateway's grant (wizard-messenger `wizard_agents.py` GatewayGrant, same file rules): the
    `from: host:agent-service` rows of this app's grants file (only key hashes, never keys), re-read when it changes.

    `state()` is 'missing' (no grants file), 'none' (a good file without the gateway's row: an App Manager that does not
    write it yet), 'rows' or 'bad' (unreadable, oversized, not a regular file or malformed). 'rows' and 'bad' mean the
    gateway is the only way in for agents; a bad file therefore fails closed instead of reopening bare calls.
    """

    def __init__(self, path=None, clock=time.time_ns):
        self.path = Path(path) if path else None
        self.stamp = None
        self.cached = ('missing', ())
        self.lock = threading.Lock()
        self.clock = clock

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None, ('missing', ())
        try:
            st = os.fstat(fd)
            stamp = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)  # ctime: utime cannot fake it
            if stamp == self.stamp:
                return stamp, self.cached
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_GRANTS_FILE:
                return stamp, ('bad', ())
            data = b''
            while len(data) <= MAX_GRANTS_FILE:
                chunk = os.read(fd, MAX_GRANTS_FILE + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) > MAX_GRANTS_FILE:
                return stamp, ('bad', ())
        finally:
            os.close(fd)
        try:
            doc = json.loads(data)
        except ValueError:
            return stamp, ('bad', ())
        rows = doc.get('incoming') if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return stamp, ('bad', ())
        hashes = tuple(row['sha256'] for row in rows if isinstance(row, dict) and row.get('from') == GATEWAY
                       and isinstance(row.get('sha256'), str) and SHA256_HEX.fullmatch(row['sha256']))
        return stamp, ('rows' if hashes else 'none', hashes)

    def current(self):
        if not self.path:
            return ('missing', ())
        with self.lock:
            try:
                stamp, value = self._read()
            except Exception:  # noqa: BLE001  unreadable or pathological: fail closed
                self.stamp, self.cached = None, ('bad', ())
                return self.cached
            # A stamp younger than 2 s is not trusted: an in-place rewrite within one mtime tick keeps inode and size.
            self.stamp = stamp if stamp and self.clock() - max(stamp[0], stamp[1]) > 2_000_000_000 else None
            self.cached = value
            return value

    def state(self):
        return self.current()[0]

    def matches(self, grant):
        if not isinstance(grant, str) or not KEY_RE.fullmatch(grant):
            return False
        hashes = self.current()[1]
        if not hashes:
            return False
        digest = hashlib.sha256(grant.encode()).hexdigest().encode()
        return any(hmac.compare_digest(digest, h.encode()) for h in hashes)


def resolve_caller(headers, apps, gateway, access=None):
    """Who an MCP or /v1 request (already box-local, not a browser, not forwarded) speaks for: 'app:<id>' for a front
    end's own owner tap, 'agent:<id>' for an agent. `headers.get` reads the request headers. Raises AccessRefused.

    The agent name is believed only behind a grant: the gateway's (it authenticates each agent with its own token), or
    a front end's (Mail / Calendar relaying an agent they verified through the gateway). A self-declared name never is.
    """
    grant = headers.get('X-Wizard-Grant') or ''
    named = (headers.get('X-Wizard-Agent') or '').strip().lower()
    label = headers.get('X-Wizard-Agent-Label')
    if named and not AGENT_ID.fullmatch(named):
        raise AccessRefused(400, 'invalid_agent', 'Invalid agent name.')
    if grant:
        app = apps.caller(grant)
        if app:
            if not named:
                return 'app:' + app
            caller = 'agent:' + (MAIN if named in MAIN_NAMES else named)
        elif gateway.matches(grant):
            caller = 'agent:' + (MAIN if not named or named in MAIN_NAMES else named)
        else:
            raise AccessRefused(401, 'grant_unknown', 'This grant is not known here.')
        if access is not None:
            access.seen(caller, label)
        return caller
    if gateway.state() in ('rows', 'bad'):
        raise AccessRefused(401, 'gateway_grant_required', 'Agents reach this app through the Wizard gateway '
                                                           '(mcp__wizard__...), not directly.')
    # A box whose App Manager does not write the gateway's row yet: a bare call is the main agent, whatever it says.
    return 'agent:' + MAIN
