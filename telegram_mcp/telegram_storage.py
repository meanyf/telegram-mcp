"""Чтение выбранных Telegram-чатов и хранение отметок передачи сообщений."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from .paths import TELEGRAM_DATA


def selected_sources():
    path = TELEGRAM_DATA / 'config.json'
    if not path.exists():
        raise ValueError('Сначала настройте Telegram: .venv/bin/python -m telegram_mcp.telegram_collector setup')
    # Конфигурация содержит ключи: наружу возвращаем только явно выбранные поля.
    config = json.loads(path.read_text())
    return {source['id']: {'chat_id': source['id'], 'title': source['title']}
            for source in config.get('sources', [])}


def open_messages():
    path = TELEGRAM_DATA / 'messages.sqlite3'
    if not path.exists():
        raise ValueError('База пока не создана. Запустите .venv/bin/python -m telegram_mcp.telegram_collector run')
    db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def read_new_messages(chat_id):
    path = TELEGRAM_DATA / 'delivery.sqlite3'
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as state, state:
        state.execute("""CREATE TABLE IF NOT EXISTS delivered
                         (chat_id INTEGER PRIMARY KEY, last_id INTEGER NOT NULL)""")
        state.execute('BEGIN IMMEDIATE')
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)
        cursor = state.execute('SELECT last_id FROM delivered WHERE chat_id = ?',
                               (chat_id,)).fetchone()
        last_id = cursor[0] if cursor else 0
        with closing(open_messages()) as db:
            rows = db.execute("""SELECT message_id, sent_at, sender_id, text, link, has_media
                                 FROM messages WHERE chat_id = ? AND message_id > ?
                                 AND julianday(sent_at) >= julianday(?)
                                 AND julianday(sent_at) <= julianday(?)
                                 ORDER BY message_id""",
                              (chat_id, last_id, start.isoformat(), end.isoformat())).fetchall()
        messages = [dict(row) for row in rows]
        if messages:
            last_id = messages[-1]['message_id']
        state.execute("""INSERT INTO delivered VALUES (?, ?)
                         ON CONFLICT(chat_id) DO UPDATE SET last_id = excluded.last_id""",
                      (chat_id, last_id))
    return {'messages': messages, 'window_start_utc': start.isoformat(),
            'window_end_utc': end.isoformat()}


def list_chats() -> dict:
    sources = selected_sources()
    with closing(open_messages()) as db:
        chats = []
        for chat_id, source in sources.items():
            stats = db.execute('''SELECT COUNT(*) AS message_count, MAX(sent_at) AS latest_at
                                  FROM messages WHERE chat_id = ?''', (chat_id,)).fetchone()
            chats.append({**source, **dict(stats)})
    return {'chats': chats}


def get_messages(chat_id: int) -> dict:
    source = selected_sources().get(chat_id)
    if source is None:
        raise ValueError('Чат не выбран для сбора')
    return {**source, **read_new_messages(chat_id)}
