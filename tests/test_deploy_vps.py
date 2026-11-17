"""Проверить защиту данных VPS при реальной локальной синхронизации rsync."""

from pathlib import Path
import shlex
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_vps
from scripts.setup_vps import unit_text


class DeploymentTests(unittest.TestCase):
    def test_update_reuses_one_authenticated_connection_and_closes_it(self):
        with patch.object(deploy_vps.subprocess, 'run') as process, \
                patch.object(deploy_vps.shutil, 'which', return_value='/usr/bin/tool'), \
                patch('sys.argv', ['deploy_vps.py', 'update', 'root@vps', '--port', '2222']):
            deploy_vps.main()
        commands = [call.args[0] for call in process.call_args_list]
        self.assertEqual(sum('-M' in command for command in commands), 1)
        master = commands[0]
        self.assertIn('ControlMaster=yes', master)
        self.assertNotIn('BatchMode=yes', master)
        socket = next(value for value in master if value.startswith('ControlPath='))
        for command in commands[1:]:
            transport = shlex.split(command[command.index('-e') + 1]) if command[0] == 'rsync' else command
            self.assertIn(socket, transport)
            self.assertIn('BatchMode=yes', transport)
            self.assertEqual(transport[transport.index('-p') + 1], '2222')
        self.assertIn('-O', commands[-1])
        self.assertEqual(commands[-1][-2:], ['exit', 'root@vps'])
        self.assertFalse(Path(socket.split('=', 1)[1]).parent.exists())

    def test_failed_transfer_closes_connection_and_preserves_original_error(self):
        with patch.object(deploy_vps.subprocess, 'run') as process:
            with self.assertRaisesRegex(RuntimeError, 'transfer failed'):
                with deploy_vps.ssh_connection('root@vps', 22):
                    process.side_effect = OSError('cleanup failed')
                    raise RuntimeError('transfer failed')
        self.assertIn('-O', process.call_args.args[0])

    def test_rsync_quotes_control_socket_path(self):
        socket = 'ControlPath=/tmp/directory with spaces/socket'
        command = deploy_vps.rsync_command('root@vps', 22, ['-o', socket])
        transport = shlex.split(command[command.index('-e') + 1])
        self.assertIn(socket, transport)

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
            collector = unit_text('-m telegram_mcp.collector run')
        self.assertIn('WorkingDirectory=/root/MCP_hh', mcp)
        self.assertIn('ExecStart=/root/MCP_hh/.venv/bin/python scripts/start_mcp.py', mcp)
        self.assertIn('Restart=always', mcp)
        self.assertIn('WantedBy=multi-user.target', collector)
        self.assertIn('/.local/ngrok.env', mcp)
        self.assertNotIn('EnvironmentFile=', collector)


if __name__ == '__main__':
    unittest.main()
