# Microsoft for Wizard

Outlook mail and calendar for a Wizard box: connect Outlook.com, Hotmail or Microsoft 365 accounts, switch Mail and
Calendar on per account, and give the agent and other apps one set of mail/calendar tools.

## Two ways to connect

| | Quick connect | Your own Microsoft app |
|---|---|---|
| Owner effort | Free Composio account, paste its API key, tap Quick connect (about a minute) | Register an app in Microsoft Entra with the app's guided steps (about ten minutes) |
| Who holds the sign-in | Composio (a third party) holds and refreshes the Microsoft tokens | This box only |
| Cost | Composio Hobby: free, hard-capped; Composio-managed sign-ins include about 20,000 calls a month | Free |
| Calls go | box → Composio proxy → Microsoft Graph | box → Microsoft Graph |

Both paths expose the same tools (contract `wizard.mail-calendar/1`, see `docs/mail-calendar-contract.md`), shared with
the Google app, so the Mail and Calendar front ends work with any source. `mail_draft` takes attachments (Graph
fileAttachment, 3 MB per file), changes a draft in place with `draft_id` (files kept unless in `remove_attachments`) and
forwards files with `attachments_from` (createForward) (contract 1.2, 0.3.0).

## Layout

- `app/server.py` HTTP: owner page API (Tailscale owner + same-origin + CSRF), Microsoft and Composio return pages,
  MCP (`/mcp`) and its box-only HTTP mirror (`/v1/<tool>`).
- `app/service.py` accounts, the owner's own Entra app (OAuth code + PKCE, refresh), Quick connect, tool dispatch.
- `app/graph.py` Outlook mail and calendar in the contract's shapes; one code path for both ways of connecting.
- `app/composio.py` small reusable Composio client (keys with validation and fallback, Connect Links, proxy). Copy it
  unchanged into other apps.
- `web/` vanilla view on the Wizard style kit.
- `tools/package.py` inlines the source into `runtipi/docker-compose.yml` (run after every edit).

## Develop

```sh
python3 -m unittest discover -s tests -v     # fakes stand in for Composio and Microsoft
python3 tools/package.py && wam check . --proposal
MICROSOFT_DEV_MODE=loopback-single-owner DATA_DIR=.data PORT=18899 BIND=127.0.0.1 python3 app/server.py
```

The signup link for Composio is one constant (`composio.SIGNUP_URL`, or env `COMPOSIO_SIGNUP_URL`).
