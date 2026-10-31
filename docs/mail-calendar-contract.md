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
| `mail_search` | `account?`, `query?`, `unread_only?`, `max?` (1–25), `page_token?` | `messages[]` (summary), `next_page_token`, `untrusted` |
| `mail_get` | `account?`, `id` | `message` (summary + `body_text`, `truncated`, `attachments[]`, `message_id_header`), `untrusted` |
| `mail_draft` | `account?`, `body`, `to[]`, `cc[]`, `bcc[]`, `subject`, `reply_to_id?` | `draft` {`id`, `message_id`, `thread_id`, `to`, `cc`, `subject`, `body_text`}, `sent: false` |
| `mail_send` | `account?`, `draft_id` | `sent: true`, `id`, `thread_id` |
| `calendar_list_events` | `account?`, `calendar_id?`, `time_min?`, `time_max?`, `query?`, `max?`, `page_token?` | `events[]`, `next_page_token`, `untrusted` |
| `calendar_create_event` | `account?`, `calendar_id?`, `title`, `start`, `end`, `time_zone?`, `location?`, `description?`, `attendees[]` | `created`, `event` |
| `calendar_update_event` | `account?`, `calendar_id?`, `id`, any of the create fields | `updated`, `event` |
| `calendar_delete_event` | `account?`, `calendar_id?`, `id` | `deleted`, `id` |
| `calendar_respond` | `account?`, `calendar_id?`, `id`, `response` (`accepted` \| `declined` \| `tentative`), `comment?` | `responded`, `event` |

Message summary: `id`, `thread_id`, `from` {name, email}, `to[]`, `cc[]`, `subject`, `date` (ISO 8601), `snippet`,
`unread`, `labels[]`.

Event: `id`, `calendar_id`, `title`, `start`/`end` (`{date}` for all-day, else `{date_time, time_zone}`), `all_day`,
`location`, `description`, `attendees[]` {email, name, response, self, organizer}, `organizer`, `is_organizer`,
`my_response` (`none` | `accepted` | `declined` | `tentative` | `organizer`), `status`, `link`, `recurring`,
`online_meeting`.

Times in: a date (`2026-11-04`) for all-day, an RFC 3339 time with offset, or a local time plus `time_zone` (IANA).

## Rules every source keeps

- `mail_draft` never sends. `mail_send` only sends an existing draft, so the owner can approve the exact text first. It
  is annotated `destructiveHint` and its description starts with SENDS MAIL.
- Mail and event text is untrusted data from other people: results carry an `untrusted` note; sources never act on it.
- Errors are `{error: {code, message}}` with stable codes: `not_connected`, `service_off`, `account_required`,
  `account_not_found`, `reauth_required`, `invalid_arguments`, `not_found`, `rate_limited`, `permission_missing` /
  `google_permission_missing`, `composio_quota`, `composio_key_invalid`, `provider_unavailable`.
- No source returns a password, key or token.
