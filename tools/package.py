"""Write runtipi/docker-compose.yml: one stdlib Python container with this app's source inlined (zlib + base64).
Run after every source edit; tests/test_package.py fails when the recipe is stale. (`wam package-inline` would
regenerate the template recipe and drop this app's environment, so this script owns the whole file.)"""
import base64
import json
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted(['openapi.json'] + [f'app/{p.name}' for p in (ROOT / 'app').glob('*.py') if p.name != 'build_view.py']
               + [f'web/{p.name}' for p in (ROOT / 'web').iterdir() if p.suffix in ('.html', '.js', '.css')])
IMAGE = 'python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4'
PORT = 18814
LOADER = ("import base64,json,pathlib,runpy,zlib\nroot=pathlib.Path('/tmp/wizard-app');root.mkdir(exist_ok=True)\n"
          "files=json.loads(zlib.decompress(base64.b64decode('{blob}')))\n"
          "for name,text in files.items():\n p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)\n"
          "runpy.run_path(str(root/'app/server.py'),run_name='__main__')\n")
HEAD = f"""# Microsoft: one stdlib Python container; the app source is inlined below (refresh with `python3 tools/package.py`).
# Secrets (Composio keys, the owner's own app secret, Microsoft tokens) live in /data/private: 0700 dir, 0600 file,
# re-tightened on every start, because Runtipi leaves app-data itself world-writable.
services:
  wizard-microsoft:
    image: {IMAGE}
    pids_limit: 64
    user: 1000:1000
    cap_drop:
    - ALL
    security_opt:
    - no-new-privileges:true
    mem_limit: 128m
    read_only: true
    tmpfs:
    - /tmp:rw,nosuid,size=32m,uid=1000,gid=1000
    environment:
      PORT: '{PORT}'
      APP_PORT: ${{APP_PORT:-{PORT}}}
      BIND: 0.0.0.0
      PYTHONDONTWRITEBYTECODE: '1'
      DATA_DIR: /data/private
      WIZARD_SHELL_AUTH: ${{WIZARD_SHELL_AUTH:-tailscale-serve}}
      WIZARD_OWNER_LOGINS: ${{WIZARD_OWNER_LOGINS:-}}
      WIZARD_SHELL_PATH: ${{WIZARD_SHELL_PATH:-/wizard-microsoft/}}
      WIZARD_TRUSTED_HOSTS: ${{WIZARD_TRUSTED_HOSTS:-}}
      WIZARD_TRUSTED_ORIGINS: ${{WIZARD_TRUSTED_ORIGINS:-}}
      WIZARD_OWNER_TOKEN: ${{WIZARD_OWNER_TOKEN:-}}
      WIZARD_ADMIN_TOKEN: ${{WIZARD_ADMIN_TOKEN:-}}
      COMPOSIO_SIGNUP_URL: ${{COMPOSIO_SIGNUP_URL:-}}
    volumes:
    - ${{APP_DATA_DIR}}/data:/data
    command:
    - python
    - -c
"""
TAIL = f"""    x-runtipi:
      is_main: true
      internal_port: {PORT}
x-runtipi:
  schema_version: 2
  overrides: []
"""


def build():
    files = {name: (ROOT / name).read_text() for name in FILES}
    blob = base64.b64encode(zlib.compress(json.dumps(files, sort_keys=True).encode(), 9)).decode()
    command = LOADER.replace('{blob}', blob)
    if len(command.encode()) > 120_000:
        sys.exit(f'inline command is {len(command)} bytes; Linux allows 128 KiB per argument')
    compile(command, 'loader', 'exec')
    return HEAD + '    - ' + json.dumps(command) + '\n' + TAIL


if __name__ == '__main__':
    text = build()
    (ROOT / 'runtipi' / 'docker-compose.yml').write_text(text)
    print('embedded', len(FILES), 'files,', len(text), 'bytes ->', 'runtipi/docker-compose.yml')
