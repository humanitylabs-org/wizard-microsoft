# Source and license notice

Microsoft for Wizard, by Humanity Labs. GPL-3.0-only (see LICENSE); no warranty.

- Started from Wizard App Manager's `hello-wizard` template (first-party Humanity Labs code). The canonical Wizard OS style
  snapshot (`web/wizard-tokens.css`, `web/wizard-ui.css`, `web/wizard-sheet.js`, `web/boot.js`) is pinned by
  `appearance-provenance.json` and left unmodified. The view loads the shared `/wizard-os-home/core/app-client.js`.
- `app/composio.py` is the shared Composio client also shipped in `humanitylabs-org/wizard-google`.
- Talks to Composio (https://composio.dev, REST API v3.1), Microsoft identity platform and Microsoft Graph v1.0 over their
  public HTTP APIs; no third-party code is bundled. Python and the base image keep their upstream licenses.
