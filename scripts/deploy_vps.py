"""Первый перенос через scp и обновление через rsync."""

import argparse
from contextlib import contextmanager
import fcntl
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tempfile


PROJECT_DIR = Path(__file__).resolve().parents[1]
CODE_PATHS = ('README.md', 'requirements.txt', 'telegram_mcp', 'scripts', 'prompts', 'tests')
DATA_FILES = ('config.json', 'account.session', 'messages.sqlite3',
              'delivery.sqlite3', 'bot.json', 'bot-outbox.sqlite3',
              'events-key', 'events.sqlite3', 'preferences.md')
REMOTE_DIR = 'MCP_hh'


def rsync_command(target, port, ssh_options=()):
    command = ['rsync', '-az', '--delete', '--itemize-changes',
               '-e', shlex.join(['ssh', '-p', str(port), *ssh_options]), '--exclude=__pycache__/', '--exclude=*.pyc']
    for name in CODE_PATHS:
        pattern = f'/{name}/***' if (PROJECT_DIR / name).is_dir() else f'/{name}'
        command.append(f'--include={pattern}')
    # Excluded files are protected from --delete, including all VPS state.
    command += ['--exclude=*', f'{PROJECT_DIR}/', f'{target}:{REMOTE_DIR}/']
    return command


def run(command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


@contextmanager
def ssh_connection(target, port):
    """Одна авторизация на весь перенос, включая scp и rsync."""
    with tempfile.TemporaryDirectory(prefix='mcp-ssh-') as directory:
        control = ['-o', f'ControlPath={directory}/socket']
        run(['ssh', '-M', '-N', '-f', '-o', 'ControlMaster=yes',
             '-o', 'ControlPersist=no', *control, '-p', str(port), target])
        options = [*control, '-o', 'ControlMaster=no', '-o', 'BatchMode=yes']
        try:
            yield options
        finally:
            try:
                subprocess.run(['ssh', *options, '-p', str(port), '-O', 'exit', target],
                               check=False, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'update'])
    parser.add_argument('target', help='SSH-адрес, например ubuntu@203.0.113.10 или имя из ~/.ssh/config')
    parser.add_argument('--port', type=int, default=22)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', args.target):
        parser.error('Укажите SSH-адрес user@host или имя из ~/.ssh/config.')
    if not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535.')
    for tool in ('ssh', 'scp' if args.command == 'init' else 'rsync'):
        if not shutil.which(tool):
            parser.error(f'Установите {tool}.')
    with ssh_connection(args.target, args.port) as ssh_options:
        deploy(args, ssh_options)


def deploy(args, ssh_options):
    ssh = ['ssh', *ssh_options, '-p', str(args.port), args.target]

    if args.command == 'init':
        # Refuse to overwrite a previous installation or its Telegram state.
        data = PROJECT_DIR / '.local/telegram'
        if not (data / 'config.json').exists() or not (data / 'account.session').exists():
            raise RuntimeError('Сначала настройте Telegram локально: нужны config.json и account.session.')
        with (data / 'collector.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('Перед переносом остановите локальный сборщик.') from error
            with tempfile.TemporaryDirectory(prefix='telegram-mcp-deploy-') as directory:
                staging = Path(directory)
                for name in CODE_PATHS:
                    source = PROJECT_DIR / name
                    if source.is_dir():
                        shutil.copytree(source, staging / name,
                                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                    else:
                        shutil.copy2(source, staging / name)
                copied_data = staging / '.local/telegram'
                copied_data.mkdir(parents=True, mode=0o700)
                # SQLite backup also includes committed WAL pages, if present.
                import sqlite3
                from contextlib import closing
                for name in DATA_FILES:
                    source = data / name
                    if not source.exists():
                        continue
                    destination = copied_data / name
                    if name.endswith(('.sqlite3', '.session')):
                        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
                            with closing(sqlite3.connect(destination)) as dst:
                                src.backup(dst)
                    else:
                        shutil.copy2(source, destination)
                    destination.chmod(0o600)
                run([*ssh, f'mkdir -m 700 {REMOTE_DIR}'])
                run(['scp', *ssh_options, '-P', str(args.port), '-r',
                     *[str(staging / name) for name in CODE_PATHS],
                     str(staging / '.local'), f'{args.target}:{REMOTE_DIR}/'])
        run(['ssh', *ssh_options, '-t', '-p', str(args.port), args.target,
             f'cd {REMOTE_DIR} && python3 scripts/setup_vps.py install'])
    else:
        run([*ssh, f'test -x {REMOTE_DIR}/.venv/bin/python && command -v rsync >/dev/null'])
        # Stop before replacing code, so a running process cannot import half an update.
        run(['ssh', *ssh_options, '-t', '-p', str(args.port), args.target,
             f'cd {REMOTE_DIR} && python3 scripts/setup_vps.py stop'])
        try:
            run(rsync_command(args.target, args.port, ssh_options))
            run(['ssh', *ssh_options, '-t', '-p', str(args.port), args.target,
                 f'cd {REMOTE_DIR} && python3 scripts/setup_vps.py update'])
        except (Exception, KeyboardInterrupt):
            print('Обновление прервано. Сервисы могут быть остановлены. Исправьте ошибку и повторите update.')
            raise


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Ошибка: {error}')
