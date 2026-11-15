"""Проверить защиту данных VPS при реальной локальной синхронизации rsync."""

from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_vps
from scripts.setup_vps import unit_text


class DeploymentTests(unittest.TestCase):
    def test_update_targets_existing_project_in_ssh_users_home(self):
        command = deploy_vps.rsync_command('root@vps', 22)
        self.assertEqual(command[-1], 'root@vps:MCP_hh/')

    @unittest.skipUnless(shutil.which('rsync'), 'rsync не установлен')
    def test_update_replaces_code_but_preserves_vps_data_and_environment(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            target = Path(directory) / 'target'
            source.mkdir()
            target.mkdir()
            for root in (source, target):
                for name in ('telegram_mcp', 'scripts', 'prompts', 'tests', '.local', '.venv', '.git', '.aws'):
                    (root / name).mkdir()
                for name in ('README.md', 'requirements.txt'):
                    (root / name).write_text('code')
            (source / 'telegram_mcp/server.py').write_text('new code')
            (target / 'telegram_mcp/removed.py').write_text('obsolete code')
            for name in ('.local', '.venv', '.git', '.aws'):
                (source / name / 'state').write_text('local state')
                (target / name / 'state').write_text('VPS state')
            (source / 'chat-hh-codex.json').write_text('private data')
            (source / 'telegram_mcp/__pycache__').mkdir()
            (source / 'telegram_mcp/__pycache__/module.pyc').write_bytes(b'cached')
            with patch.object(deploy_vps, 'PROJECT_DIR', source):
                command = deploy_vps.rsync_command('unused', 22)
            command[-1] = str(target) + '/'
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((target / 'telegram_mcp/server.py').read_text(), 'new code')
            self.assertFalse((target / 'telegram_mcp/removed.py').exists())
            self.assertFalse((target / 'telegram_mcp/__pycache__').exists())
            self.assertFalse((target / 'chat-hh-codex.json').exists())
            for name in ('.local', '.venv', '.git', '.aws'):
                self.assertEqual((target / name / 'state').read_text(), 'VPS state')

    def test_services_have_restart_policy_and_only_mcp_loads_ngrok_token(self):
        with patch('scripts.setup_vps.PROJECT_DIR', Path('/root/MCP_hh')):
            mcp = unit_text('scripts/start_mcp.py', environment_file=True)
            collector = unit_text('-m telegram_mcp.telegram_collector run')
        self.assertIn('WorkingDirectory=/root/MCP_hh', mcp)
        self.assertIn('ExecStart=/root/MCP_hh/.venv/bin/python scripts/start_mcp.py', mcp)
        self.assertIn('Restart=always', mcp)
        self.assertIn('WantedBy=multi-user.target', collector)
        self.assertIn('/.local/ngrok.env', mcp)
        self.assertNotIn('EnvironmentFile=', collector)


if __name__ == '__main__':
    unittest.main()
