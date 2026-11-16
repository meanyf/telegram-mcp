"""Локальные настройки, секрет адреса и SQLite для событий."""

from contextlib import contextmanager
import json
import os
import secrets
import sqlite3

from ..paths import TELEGRAM_DATA
from . import EventError

def endpoint_path():
    """Секретный адрес заменяет отдельный логин для персонального сервера."""
    TELEGRAM_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = TELEGRAM_DATA / 'events-key'
    try:
        # Atomic publication: readers never see a partially written key.
        temporary = path.with_name(f'.events-key-{secrets.token_hex(8)}')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink()
        key = path.read_text().strip()
        if len(key) < 32:
            raise RuntimeError('Некорректный файл events-key.')
        return '/mcp/' + key
    except OSError:
        raise RuntimeError('Не удалось прочитать или сохранить events-key.') from None

def bot_config():
    try:
        return json.loads((TELEGRAM_DATA / 'bot.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise EventError('Сначала настройте Telegram-бота.') from None

def owner_id():
    config = bot_config()
    return f'{config["bot_id"]}:{config["chat_id"]}'

@contextmanager
def database():
    TELEGRAM_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = TELEGRAM_DATA / 'events.sqlite3'
    path.touch(mode=0o600, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL,
                secret TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                event_id TEXT PRIMARY KEY, owner TEXT NOT NULL, update_id INTEGER NOT NULL,
                body TEXT NOT NULL, UNIQUE(owner, update_id));
            CREATE TABLE IF NOT EXISTS deliveries (
                event_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(event_id, subscription_id));
            CREATE TABLE IF NOT EXISTS offsets (bot_id INTEGER PRIMARY KEY, value INTEGER NOT NULL);
        ''')
        with db:
            yield db
    finally:
        db.close()
