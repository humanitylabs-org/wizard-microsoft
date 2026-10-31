---
wizard: 0.2
id: wizard-microsoft
name: Microsoft
type: external
category: system
version: 0.2.2
summary: Connect Outlook mail and calendar. Quick connect through Composio, or your own Microsoft app; several accounts.
tags: [communication, productivity]
license: GPL-3.0-only
source: https://github.com/oscarhenrycollins/wizard-microsoft
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 18814
  path: /v1
  spec: openapi.json
mcp:
  path: /mcp
  port: 18814
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 18814
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [microsoft-connection, mail-calendar]
system: true
uses: []
needs: []
health: /healthz
---

# Microsoft

Connect Outlook mail and calendar (Outlook.com, Hotmail, Microsoft 365) to your Wizard, then switch Mail and Calendar on per account. Work and personal accounts sit side by side.

- **Quick connect (about a minute).** Goes through [Composio](https://composio.dev), a third party whose Microsoft sign-in is already approved. Tap Quick connect: one window asks you to sign in to Composio (or create a free account; the key is saved by itself, or paste one), then to Microsoft. Composio holds the sign-in keys, so technically it can access the account. Free for normal personal use; heavy use can cost money. You can switch any time.
- **Your own Microsoft app (about ten minutes).** The app walks you through registering an app in Microsoft Entra, step by step with copy buttons; you paste its ID and secret. Sign-ins stay on this box and nobody else is involved.
- **For your agent and apps:** the same mail and calendar tools whichever way you connected: `mail_search`, `mail_get`, `mail_draft`, `mail_send` (sends an approved draft), `calendar_list_events`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event`, `calendar_respond`. MCP at `/mcp`, and the same tools over HTTP at `/v1/<tool>` for scripts and apps on this box. `mail_update` marks read, archives or trashes. Contract `wizard.mail-calendar/1` (shared with the Google app). Mail and Calendar can run Quick connect from inside their own window (owner routes in docs/mail-calendar-contract.md). Mail text is returned as data, never acted on.

Keys, secrets and tokens stay in this app's data folder (0600). The page and the tools never return them.
