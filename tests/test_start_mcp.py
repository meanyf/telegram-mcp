"""Параметры запуска и остановка MCP вместе с процессами Uvicorn."""

import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts.start_mcp import PROJECT_DIR, server_command, stop_processes


class LauncherTests(unittest.TestCase):
    def test_reload_uses_mcp_factory_and_watches_code_and_instructions(self):
        command = server_command('/venv/bin/python', reload=True)
        self.assertEqual(command[:3], ['/venv/bin/python', '-m', 'uvicorn'])
        self.assertIn('telegram_mcp.mcp_server:server.streamable_http_app', command)
        self.assertIn('--factory', command)
        self.assertIn('--reload', command)
        watched = [command[i + 1] for i, arg in enumerate(command) if arg == '--reload-dir']
        self.assertEqual(watched, [str(PROJECT_DIR / 'telegram_mcp'), str(PROJECT_DIR / 'prompts')])
        patterns = [command[i + 1] for i, arg in enumerate(command) if arg == '--reload-include']
        self.assertEqual(patterns, ['*.py', '*.md'])

    def test_normal_launch_runs_mcp_module(self):
        self.assertEqual(server_command('/venv/bin/python'),
                         ['/venv/bin/python', '-m', 'telegram_mcp.mcp_server'])

    @patch('scripts.start_mcp.os.killpg')
    def test_shutdown_signals_entire_group_and_forces_it_after_timeout(self, killpg):
        process = Mock(pid=123, poll=Mock(return_value=None))
        process.wait.side_effect = [subprocess.TimeoutExpired('uvicorn', 5), 0]
        stop_processes([process])
        self.assertEqual([call.args for call in killpg.call_args_list],
                         [(123, signal.SIGTERM), (123, signal.SIGKILL)])

    @patch('scripts.start_mcp.os.killpg', side_effect=ProcessLookupError)
    def test_shutdown_tolerates_process_exiting_before_signal(self, killpg):
        process = Mock(pid=123, poll=Mock(return_value=None))
        stop_processes([process])
        process.wait.assert_called_once_with(timeout=5)


if __name__ == '__main__':
    unittest.main()
