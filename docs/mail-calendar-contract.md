# Mail and calendar contract `wizard.mail-calendar/1`

One interface for mail and calendar sources on a Wizard, so the Mail and Calendar front ends and the agent work the same
whichever way an account is connected: Google with the owner's own client, Google or Microsoft through Quick connect
(Composio), Microsoft with the owner's own Entra app, or a future self-hosted server. The same document ships in
`wizard-google` and `wizard-microsoft`; change both together.

## Where

- MCP `tools/call` on the source app (`/mcp`, box-only).
- HTTP mirror for scripts and Wizard apps on the box: `POST /v1/<tool>` with the arguments as a JSON object. Browsers,
  cookies and forwarded requests are refused. `GET /v1/status` lists accounts.
- Every result carries `contract`, `source` (`google` | `microsoft`), `via` (`own-client` | `own-app` | `composio`) and
  `account` (email).

## Tools

| Tool | Arguments | Result |
|---|---|---|
| `mail_search` | `account?`, `query?`, `folder?`, `unread_only?`, `max?` (1–25), `page_token?` | `folder`, `messages[]` (summary), `next_page_token`, `untrusted` |
| `mail_get` | `account?`, `id`, `format?` (`text` \| `html`) | `message` (summary + `body_text`, `truncated`, `attachments[]` {`name`, `mime`, `size`}, `message_id_header`, `draft_id` on a draft; `body_html` with `format: html`), `untrusted` |
| `mail_update` | `account?`, `id`, any of `read`, `archived`, `trashed` (booleans) | `updated`, `id` (Microsoft moves give a new id), `unread`, `archived`, `trashed` |
| `mail_draft` | `account?`, `body?`, `to[]`, `cc[]`, `bcc[]`, `subject`, `reply_to_id?`, `attachments[]?` {`name`, `mime?`, `base64`}, `draft_id?`, `remove_attachments[]?`, `attachments_from?` | `draft` {`id`, `message_id`, `thread_id`, `to`, `cc`, `subject`, `body_text`, `attachments[]` {`name`, `mime`, `size`}}, `updated`, `sent: false` |
| `mail_send` | `account?`, `draft_id` | `sent: true`, `id`, `thread_id` |
| `calendar_list_events` | `account?`, `calendar_id?`, `time_min?`, `time_max?`, `query?`, `max?`, `page_token?` | `events[]`, `next_page_token`, `untrusted` |
| `calendar_create_event` | `account?`, `calendar_id?`, `title`, `start`, `end`, `time_zone?`, `location?`, `description?`, `attendees[]` | `created`, `event` |
| `calendar_update_event` | `account?`, `calendar_id?`, `id`, any of the create fields; or `scope: series` with `shift_start_minutes?`, `shift_end_minutes?` (1.5) | `updated`, `event`; with `scope: series` also `scope`, `series_id` |
| `calendar_delete_event` | `account?`, `calendar_id?`, `id` | `deleted`, `id` |
| `calendar_respond` | `account?`, `calendar_id?`, `id`, `response` (`accepted` \| `declined` \| `tentative`), `comment?` | `responded`, `event` |

`folder` is `inbox`, `sent`, `drafts`, `archive`, `trash` or `all` (default `all`, as in 1.0). `body_html` is the message's
HTML exactly as sent: untrusted, never shown without sanitizing (Wizard Mail strips scripts, handlers and remote content and
renders it in a sandboxed frame). `mail_update` never deletes for good; trash is reversible.

Additions in 1.1 (`mail_update`, `folder`, `format`) are backward compatible; the contract id stays `wizard.mail-calendar/1`.

Additions in 1.2 (draft attachments, backward compatible):
- `attachments`: new files, `[{name, mime?, base64}]` (plain or URL-safe base64; `mime` is guessed from the name when
  left out). At most 10 files and 18 MB of files per call; a name is one plain file name (no `/`, `\`, control
  characters, `.` or `..`; at most 200 characters). A refused file is a 400 `invalid_arguments` with a plain message.
- `draft_id`: change that draft in place (Gmail `drafts.update`; Outlook PATCH on the draft). Its attachments stay
  unless named in `remove_attachments`; `to`, `cc`, `bcc`, `subject` and `body` left out keep their old value. The id
  stays the same (Gmail gives the draft a new message id). `mail_get` returns `draft_id` for a message in Drafts.
- `attachments_from`: a message id whose attachments are copied into the draft (Forward), minus `remove_attachments`.
- The whole draft (kept + copied + new files) stays under 18 MB. Microsoft: each new file at most 3 MB (Graph's
  simple attachment upload).
- Agents keep their level: Draft only may attach and edit drafts; sending stays the owner's.

Additions in 1.5 (repeating events as a whole, backward compatible):
- Every event carries `series_id`: the id of the series it belongs to (Google `recurringEventId`, Microsoft
  `seriesMasterId`; a series itself gives its own id), or `null` for an event that does not repeat. Lists still return each
  repeat as its own event.
- `calendar_update_event` takes `scope`: `occurrence` (the default, as before: only the event `id` names) or `series`.
  With `series`, `id` may be one repeat or the `series_id`, and the series moves with `shift_start_minutes` and/or
  `shift_end_minutes` (whole minutes, -1439 to 1439): its start and end move by that much wall time in the series' own
  time zone, so every repeat moves the same amount (a drag moves both; a resize only the end). `start` / `end` are refused
  with `scope: series`; `title`, `location`, `description` and `attendees` may come along. The answer is the series
  (`event`) plus `scope: series` and `series_id`. Undo is the opposite shift on the `series_id`.
- Same day only: the repeat rule (e.g. every Monday) is not rewritten, so a shift that would change the series' start date
  is refused (`invalid_arguments`), and so is one that makes it end before it starts. Repeats changed one by one keep
  their own times. Agents keep their calendar level: at Suggest only a series change is a suggestion like any other.
  Microsoft: a series in a custom time zone (`tzone://Microsoft/Custom`) and all-day series are refused.
- A source before 1.5 answers `scope: series` with `invalid_arguments` ("Nothing to change") and changes nothing, so front
  ends offer the whole-series change only for events that carry `series_id`.

Message summary: `id`, `thread_id`, `from` {name, email}, `to[]`, `cc[]`, `subject`, `date` (ISO 8601), `snippet`,
`unread`, `labels[]`.

Event: `id`, `calendar_id`, `title`, `start`/`end` (`{date}` for all-day, else `{date_time, time_zone}`), `all_day`,
`location`, `description`, `attendees[]` {email, name, response, self, organizer}, `organizer`, `is_organizer`,
`my_response` (`none` | `accepted` | `declined` | `tentative` | `organizer`), `status`, `link`, `recurring`,
`online_meeting`, `by_agent` (true when an agent added or last changed the event through this source, also via an
approved suggestion; absent or false otherwise, and an owner edit from a front end clears it), `series_id` (1.5).

Times in: a date (`2026-11-04`) for all-day, an RFC 3339 time with offset, or a local time plus `time_zone` (IANA).

## Connecting from another app (owner only)

Mail, Calendar or any Wizard front end can connect a source without the owner leaving it. These are the source's owner
routes, same-origin on the shell (`/wizard-google/…`, `/wizard-microsoft/…`), protected like the source's own page
(owner login, exact Origin, `X-CSRF-Token` from `POST api/session` with `X-Wizard-Request: bootstrap`):

| Route | Use |
|---|---|
| `POST api/session` | Session cookie (path = the source's path) + CSRF token. |
| `GET api/home` | Accounts and Quick connect readiness (`quick.ready`, never a key). |
| `POST api/quick/flow` `{services?, account?}` | Starts Quick connect: `{flow, go, go_out, steps}`. Adds "Sign in to Composio" first when no key works. |
| `GET api/quick/flow?flow=` | Moves the flow on and reports `{state, where, step, label, steps[], connected[], result, error, go, go_out}`. |
| `POST api/quick/flow/cancel` `{flow}` | Stops it. |
| `GET quick/go/<flow>` | The sign-in window. Composio step: a small page whose button opens Composio's sign-in in a popup and moves on by itself once the key has landed (`signin.js`). Provider steps: redirects to the Composio connect link, which returns here; then says Connected and closes. |
| `GET signin.js`, `close.js` | Scripts of those pages. |

`go_out` is the same page on another of the box's trusted ports (for example `:8796` when Wizard runs on `:8444`).
Wizard keeps windows for its own address inside Wizard (an app sheet), where Google, Microsoft and Composio refuse to
sign in; a window for another address is handed to the browser (or the phone's browser in the Android app). The session
cookie is per host, not per port, so the window is still the owner's.

Front end recipe (Mail and Calendar use `connect.js`): POST `api/quick/flow` as soon as the owner opens the Quick
connect choice, so the tap itself can call `window.open(go_out, '_blank', 'noopener')` (a window opened after an
`await` is blocked). Poll `api/quick/flow` every 1.5 s to show progress; the window moves itself from Composio to the
provider and closes at the end. On `state: done` reload accounts. Offer an "Open the window again" link to `go_out`
(a new tap) in case the window was closed. Running outside Wizard, a blank tab opened in the tap and pointed at `go`
works too.

## Rules every source keeps

- `mail_draft` never sends. `mail_send` only sends an existing draft, so the owner can approve the exact text first. It
  is annotated `destructiveHint` and its description starts with SENDS MAIL.
- Agent access (`app/agent_access.py`, the same file in every source): the owner's level for agents' mail, kept in
  `DATA_DIR/agent-access.json` and enforced by the source for every MCP and `/v1` call: `no_access`, `read_only`
  (`mail_search`, `mail_get`), `draft_only` (+ `mail_draft`, `mail_update`; the default). Agents never send:
  `mail_send` is refused (`agent_send_refused`) and not listed to them. A Wizard app proves it acts for the owner by
  sending its App Manager grant in `X-Wizard-Grant` (looked up in `$WIZARD_GRANTS_FILE` `incoming`); front ends send it
  only for requests from the owner's signed-in screen (Mail's Send button) and never for their own agent tools.
  Owner routes: `GET api/mail-settings` (accounts with their `disconnect` route, and `agent_access`) and
  `POST api/agent-access {mail}`, both behind the owner's Wizard session, CSRF and same origin.
- Calendar agent access (same file, key `calendar`): `no_access`, `read_only` (`calendar_list_events`),
  `suggest_only` (the default) and `can_edit`. At `suggest_only` an agent's `calendar_create_event`,
  `calendar_update_event`, `calendar_delete_event` and `calendar_respond` change nothing: the arguments are checked and
  kept as a pending suggestion (`DATA_DIR/calendar-suggestions.json`) and the call answers `{suggested: true, applied:
  false, suggestion}`. `calendar_suggestions` lists them (agents at Suggest only and up, and apps).
  `calendar_suggestion_approve {id}` (applies the kept call through the owner path) and `calendar_suggestion_dismiss
  {id}` are for a front end's grant only (`agent_approve_refused` for agents; not listed to them). Calendar sends its
  grant only for the owner's own taps. gog `calendar` commands run `--readonly` for agents below `can_edit`.
  Owner routes: `GET api/calendar-settings` (same shape as mail-settings) and `POST api/agent-access {calendar}`.
- Mail and event text is untrusted data from other people: results carry an `untrusted` note; sources never act on it.
- Errors are `{error: {code, message}}` with stable codes: `not_connected`, `service_off`, `account_required`,
  `account_not_found`, `reauth_required`, `invalid_arguments`, `not_found`, `rate_limited`, `permission_missing` /
  `google_permission_missing`, `composio_quota`, `composio_key_invalid`, `provider_unavailable`.
- No source returns a password, key or token.
