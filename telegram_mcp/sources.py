"""Настройки источников и список диалогов, обновляемый сборщиком."""

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import re
import secrets
import tempfile


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(data):
    path = data / 'config.json'
    if not path.exists():
        raise RuntimeError('Сначала выполните: .venv/bin/python -m telegram_mcp.collector setup')
    return json.loads(path.read_text(encoding='utf-8'))


def save_config(data, config):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data / 'config.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write_json(data / 'config.json', config)


def catalog(data):
    path = data / 'dialogs.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'chats': [], 'updated_at': None}


def refresh_token(data):
    path = data / 'dialogs-refresh.json'
    return json.loads(path.read_text())['token'] if path.exists() else None


def request_refresh(data):
    write_json(data / 'dialogs-refresh.json', {'token': secrets.token_hex(16)})


async def refresh_catalog(client, data):
    from telethon import utils

    chats = []
    async for dialog in client.iter_dialogs():
        chat_id = utils.get_peer_id(dialog.entity)
        chats.append({'id': chat_id, 'title': dialog.name or str(chat_id),
                      'username': getattr(dialog.entity, 'username', None), 'channel': dialog.is_channel})
    write_json(data / 'dialogs.json', {'chats': chats, 'updated_at': datetime.now(timezone.utc).isoformat()})


def set_selected(data, chat_id, selected):
    """Явно установить состояние кнопки: повтор callback не переключает его обратно."""
    with (data / 'config.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = load_config(data)
        sources = {source['id']: source for source in config.get('sources', [])}
        available = {source['id']: source for source in catalog(data)['chats']}
        if selected:
            source = available.get(chat_id) or sources.get(chat_id)
            if source is None:
                raise ValueError('Чат отсутствует в списке. Обновите список через /chats.')
            sources[chat_id] = source
        else:
            sources.pop(chat_id, None)
        config['sources'] = list(sources.values())
        config.setdefault('since', (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        write_json(data / 'config.json', config)


def search_chats(chats, query=''):
    """Общий поиск: точное совпадение, начало названия/слова, затем подстрока."""
    needle = query.strip().lstrip('@').casefold()
    matches = [item for item in chats
               if any(needle in str(field).casefold() for field in
                      (item.get('title') or item['id'], item.get('username') or ''))]
    return sorted(matches, key=lambda item: _search_order(item, needle))


def _search_order(item, needle):
    title = str(item.get('title') or item['id']).casefold()
    fields = (title, (item.get('username') or '').casefold())
    if not needle:
        rank = 0
    elif needle in fields:
        rank = 0
    elif any(field.startswith(needle) for field in fields):
        rank = 1
    elif any(word.startswith(needle) for field in fields for word in re.findall(r'\w+', field)):
        rank = 2
    else:
        rank = 3
    return rank, title.isdecimal(), title, item['id']
