"""The installed recipe carries the current source (python3 tools/package.py after every edit)."""
import json
import sys
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import package  # noqa: E402


class Package(unittest.TestCase):
    def test_recipe_is_fresh(self):
        self.assertEqual((ROOT / 'runtipi/docker-compose.yml').read_text(), package.build(), 'run python3 tools/package.py')

    def test_versions_agree(self):
        config = json.loads((ROOT / 'runtipi/config.json').read_text())
        server = (ROOT / 'app/server.py').read_text()
        wizard = (ROOT / 'wizard.md').read_text()
        self.assertIn(f"VERSION = '{config['version']}'", server)
        self.assertIn(f"version: {config['version']}", wizard)
        self.assertEqual(config['port'], package.PORT)

    def test_inline_files_cover_the_app(self):
        for name in ('app/server.py', 'app/service.py', 'app/graph.py', 'app/composio.py', 'web/app.js', 'web/index.html'):
            self.assertIn(name, package.FILES)
        self.assertNotIn('$', package.build().split('command:')[1].split('x-runtipi')[0].replace('${', ''), 'compose would interpolate $')



class GatewayDeclarationTest(unittest.TestCase):
    def test_wizard_md_asks_the_app_manager_for_the_gateway_row(self):
        # Without it the App Manager writes no host:agent-service row, and this app cannot tell the gateway's
        # calls (and the agent each names) from any other caller on a real box: every agent gets the main level.
        text = (ROOT / 'wizard.md').read_text()
        mcp = re.search(r'^mcp:\n((?:  .*\n)+)', text, re.M)
        self.assertIsNotNone(mcp)
        self.assertRegex(mcp.group(1), r'(?m)^  checks_gateway: true$')

if __name__ == '__main__':
    unittest.main()
