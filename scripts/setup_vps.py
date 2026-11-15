"""Установить или обновить сервисы проекта на Linux VPS с systemd."""

import argparse
import getpass
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile
import venv


PROJECT_DIR = Path(__file__).resolve().parents[1]
SERVICES = ('telegram-mcp.service', 'telegram-collector.service')


def unit_text(command, *, environment_file=False):
    user = pwd.getpwuid(os.getuid()).pw_name
    environment = f'EnvironmentFile={PROJECT_DIR}/.local/ngrok.env\n' if environment_file else ''
    return (
        '[Unit]\nDescription=Telegram MCP project\n'
        'Wants=network-online.target\nAfter=network-online.target\n\n'
        f'[Service]\nType=exec\nUser={user}\nWorkingDirectory={PROJECT_DIR}\n'
        'Environment=PYTHONUNBUFFERED=1\n'
        f'{environment}ExecStart={PROJECT_DIR}/.venv/bin/python {command}\n'
        'Restart=always\nRestartSec=10\nTimeoutStopSec=20\nUMask=0077\n\n'
        '[Install]\nWantedBy=multi-user.target\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'update', 'stop'])
    args = parser.parse_args()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(PROJECT_DIR)):
        parser.error('Путь проекта должен содержать только латиницу, цифры, /, _, . и -.')
    for executable in ('systemctl', 'rsync'):
        if not shutil.which(executable):
            parser.error(f'На VPS не установлен {executable}.')
    sudo = [] if os.getuid() == 0 else ['sudo']

    def run(command):
        subprocess.run(command, check=True, cwd=PROJECT_DIR)

    if args.command == 'stop':
        run([*sudo, 'systemctl', 'stop', *SERVICES])
        return

    local = PROJECT_DIR / '.local'
    local.mkdir(mode=0o700, exist_ok=True)
    token_file = local / 'ngrok.env'
    if args.command == 'install' and not token_file.exists():
        token = getpass.getpass('ngrok authtoken (ввод скрыт): ').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
            parser.error('Нужен непустой ngrok authtoken из dashboard.ngrok.com.')
        with token_file.open('x') as stream:
            token_file.chmod(0o600)
            stream.write(f'NGROK_AUTHTOKEN={token}\n')
    if not token_file.exists():
        parser.error('Нет .local/ngrok.env. Выполните install.')

    if args.command == 'update':
        run([*sudo, 'systemctl', 'stop', *SERVICES])
    python = PROJECT_DIR / '.venv/bin/python'
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(PROJECT_DIR / '.venv')
    run([str(python), '-m', 'pip', 'install', '-r', 'requirements.txt'])
    with tempfile.TemporaryDirectory(prefix='telegram-mcp-units-') as directory:
        for name, command in (
            ('telegram-mcp.service', 'scripts/start_mcp.py'),
            ('telegram-collector.service', '-m telegram_mcp.telegram_collector run'),
        ):
            path = Path(directory) / name
            path.write_text(unit_text(command, environment_file=name == 'telegram-mcp.service'))
            run([*sudo, 'install', '-m', '644', str(path), f'/etc/systemd/system/{name}'])
    run([*sudo, 'systemctl', 'daemon-reload'])
    run([*sudo, 'systemctl', 'enable', *SERVICES])
    run([*sudo, 'systemctl', 'restart', *SERVICES])
    run([*sudo, 'systemctl', '--no-pager', 'status', *SERVICES])
    print('\nURL подключения: sudo journalctl -u telegram-mcp -n 50 --no-pager')


if __name__ == '__main__':
    main()
