"""Чтение выбранных Telegram-чатов и хранение отметок передачи сообщений."""

from contextlib import closing
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


def read_new_messages(chat_id, limit):
    path = TELEGRAM_DATA / 'delivery.sqlite3'
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as state, state:
        state.execute('''CREATE TABLE IF NOT EXISTS delivered
                         (chat_id INTEGER PRIMARY KEY, last_id INTEGER NOT NULL)''')
        state.execute('BEGIN IMMEDIATE')
        cursor = state.execute('SELECT last_id FROM delivered WHERE chat_id = ?',
                               (chat_id,)).fetchone()
        with closing(open_messages()) as db:
            if cursor is None:
                # На первом вызове берём только последние limit сообщений, не весь архив.
                rows = db.execute('''SELECT message_id, sent_at, sender_id, text, link, has_media
                                     FROM messages WHERE chat_id = ?
                                     ORDER BY message_id DESC LIMIT ?''',
                                  (chat_id, limit + 1)).fetchall()
                skipped = len(rows) > limit
                messages = [dict(row) for row in reversed(rows[:limit])]
                has_more = False
            else:
                rows = db.execute('''SELECT message_id, sent_at, sender_id, text, link, has_media
                                     FROM messages WHERE chat_id = ? AND message_id > ?
                                     ORDER BY message_id LIMIT ?''',
                                  (chat_id, cursor[0], limit + 1)).fetchall()
                skipped = False
                messages = [dict(row) for row in rows[:limit]]
                has_more = len(rows) > limit
        last_id = messages[-1]['message_id'] if messages else (cursor[0] if cursor else 0)
        state.execute('''INSERT INTO delivered VALUES (?, ?)
                         ON CONFLICT(chat_id) DO UPDATE SET last_id = excluded.last_id''',
                      (chat_id, last_id))
    return {'messages': messages, 'has_more': has_more,
            'older_history_skipped': skipped}


def list_chats() -> dict:
    sources = selected_sources()
    with closing(open_messages()) as db:
        chats = []
        for chat_id, source in sources.items():
            stats = db.execute('''SELECT COUNT(*) AS message_count, MAX(sent_at) AS latest_at
                                  FROM messages WHERE chat_id = ?''', (chat_id,)).fetchone()
            chats.append({**source, **dict(stats)})
    return {'chats': chats}


def get_messages(chat_id: int, limit: int = 50) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError('limit должен быть от 1 до 100')
    source = selected_sources().get(chat_id)
    if source is None:
        raise ValueError('Чат не выбран для сбора')
    return {**source, **read_new_messages(chat_id, limit)}
