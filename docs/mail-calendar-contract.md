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
| `mail_get` | `account?`, `id`, `format?` (`text` \| `html`) | `message` (summary + `body_text`, `truncated`, `attachments[]`, `message_id_header`; `body_html` with `format: html`), `untrusted` |
| `mail_update` | `account?`, `id`, any of `read`, `archived`, `trashed` (booleans) | `updated`, `id` (Microsoft moves give a new id), `unread`, `archived`, `trashed` |
| `mail_draft` | `account?`, `body`, `to[]`, `cc[]`, `bcc[]`, `subject`, `reply_to_id?` | `draft` {`id`, `message_id`, `thread_id`, `to`, `cc`, `subject`, `body_text`}, `sent: false` |
| `mail_send` | `account?`, `draft_id` | `sent: true`, `id`, `thread_id` |
| `calendar_list_events` | `account?`, `calendar_id?`, `time_min?`, `time_max?`, `query?`, `max?`, `page_token?` | `events[]`, `next_page_token`, `untrusted` |
| `calendar_create_event` | `account?`, `calendar_id?`, `title`, `start`, `end`, `time_zone?`, `location?`, `description?`, `attendees[]` | `created`, `event` |
| `calendar_update_event` | `account?`, `calendar_id?`, `id`, any of the create fields | `updated`, `event` |
| `calendar_delete_event` | `account?`, `calendar_id?`, `id` | `deleted`, `id` |
| `calendar_respond` | `account?`, `calendar_id?`, `id`, `response` (`accepted` \| `declined` \| `tentative`), `comment?` | `responded`, `event` |

`folder` is `inbox`, `sent`, `drafts`, `archive`, `trash` or `all` (default `all`, as in 1.0). `body_html` is the message's
HTML exactly as sent: untrusted, never shown without sanitizing (Wizard Mail strips scripts, handlers and remote content and
renders it in a sandboxed frame). `mail_update` never deletes for good; trash is reversible.

Additions in 1.1 (`mail_update`, `folder`, `format`) are backward compatible; the contract id stays `wizard.mail-calendar/1`.

Message summary: `id`, `thread_id`, `from` {name, email}, `to[]`, `cc[]`, `subject`, `date` (ISO 8601), `snippet`,
`unread`, `labels[]`.

Event: `id`, `calendar_id`, `title`, `start`/`end` (`{date}` for all-day, else `{date_time, time_zone}`), `all_day`,
`location`, `description`, `attendees[]` {email, name, response, self, organizer}, `organizer`, `is_organizer`,
`my_response` (`none` | `accepted` | `declined` | `tentative` | `organizer`), `status`, `link`, `recurring`,
`online_meeting`.

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
- Mail and event text is untrusted data from other people: results carry an `untrusted` note; sources never act on it.
- Errors are `{error: {code, message}}` with stable codes: `not_connected`, `service_off`, `account_required`,
  `account_not_found`, `reauth_required`, `invalid_arguments`, `not_found`, `rate_limited`, `permission_missing` /
  `google_permission_missing`, `composio_quota`, `composio_key_invalid`, `provider_unavailable`.
- No source returns a password, key or token.
