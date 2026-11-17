"""Интерфейс управления источниками из sources: /chats, поиск и кнопки с пагинацией."""

import hashlib
import json
import math

from .. import sources
from ..paths import TELEGRAM_DATA
from .delivery import bot_request

PAGE_SIZE = 8


def render(query, page, mode=None):
    mode = mode or ('search' if query else 'selected')
    config = sources.load_config(TELEGRAM_DATA)
    selected = {source['id']: source for source in config.get('sources', [])}
    catalog = sources.catalog(TELEGRAM_DATA)
    available = {source['id']: source for source in catalog['chats']}
    # Keep removed/inaccessible selected chats visible so they can be disabled.
    all_chats = {chat_id: {**source, 'title': source.get('title') or str(chat_id)}
                 for chat_id, source in {**selected, **available}.items()}
    candidates = [source for source in all_chats.values()
                  if mode != 'selected' or source['id'] in selected]
    chats = sources.search_chats(candidates, query if mode == 'search' else '')
    pages = max(1, math.ceil(len(chats) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    if mode == 'selected':
        text = (f'Выбранные чаты: {len(selected)}.\n'
                'Нажми чат, чтобы отключить сбор. Для нового источника нажми «Добавить чат».')
        if not chats:
            text = 'Пока нет выбранных чатов. Нажми «Добавить чат», чтобы найти первый источник.'
    elif mode == 'search':
        text = f'Поиск: {query}\nНайдено: {len(chats)}. Нажми чат, чтобы включить или отключить сбор.'
    else:
        text = f'Все чаты: {len(chats)}. Выбрано: {len(selected)}.\n✅ — сбор включён; ⬜ — выключен.'
    if pages > 1:
        text += f'\nСтраница {page + 1}/{pages}.'
    if not catalog['updated_at']:
        text += '\nСборщик ещё готовит список. Повтори /chats через минуту.'
    keyboard = []
    for source in chats[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        enabled = source['id'] in selected
        title = ' '.join(source['title'].split())
        username = source.get('username')
        if username:
            suffix = ' · @' + username
            title = title[:max(1, 54 - len(suffix))] + suffix
        elif title == str(source['id']):
            title = 'Без названия · ' + title
        title = title[:64]
        keyboard.append([{
            'text': ('✅ ' if enabled else '⬜ ') + title,
            'callback_data': f'chats:set:{source["id"]}:{int(not enabled)}:{page}',
        }])
    navigation = []
    if page:
        navigation.append({'text': '←', 'callback_data': f'chats:page:{page - 1}'})
    if page < pages - 1:
        navigation.append({'text': '→', 'callback_data': f'chats:page:{page + 1}'})
    if navigation:
        keyboard.append(navigation)
    keyboard.append([{'text': 'Добавить чат', 'switch_inline_query_current_chat': ''}])
    if mode != 'selected':
        keyboard.append([{'text': 'Выбранные чаты', 'callback_data': 'chats:view:selected'}])
    if mode != 'all':
        keyboard.append([{'text': 'Все чаты', 'callback_data': 'chats:view:all'}])
    if mode != 'selected':
        keyboard.append([{'text': 'Обновить список', 'callback_data': 'chats:refresh'}])
    return {'text': text, 'reply_markup': {'inline_keyboard': keyboard}}


def fingerprint(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def open_menu(config, query='', mode=None):
    query = query.strip()[:200]
    mode = mode or ('search' if query else 'selected')
    sources.request_refresh(TELEGRAM_DATA)
    content = render(query, 0, mode)
    sent = bot_request(config['token'], 'sendMessage', {'chat_id': config['chat_id'], **content})
    sources.write_json(TELEGRAM_DATA / 'chat-menu.json', {
        'bot_id': config['bot_id'], 'chat_id': config['chat_id'],
        'message_id': sent['message_id'], 'query': query, 'mode': mode, 'fingerprint': fingerprint(content),
    })


def handle_callback(config, callback):
    callback_id = callback.get('id')
    if not callback_id:
        return

    def answer(text):
        bot_request(config['token'], 'answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})

    message = callback.get('message', {})
    if (callback.get('from', {}).get('id') != config['chat_id']
            or message.get('chat', {}).get('id') != config['chat_id']
            or message.get('chat', {}).get('type') != 'private'
            or message.get('from', {}).get('id') != config['bot_id']):
        answer('Этот список доступен только владельцу бота.')
        return
    path = TELEGRAM_DATA / 'chat-menu.json'
    menu = json.loads(path.read_text()) if path.exists() else {}
    if (menu.get('message_id') != message.get('message_id') or menu.get('bot_id') != config['bot_id']
            or menu.get('chat_id') != config['chat_id']):
        answer('Этот список устарел. Отправь /chats.')
        return
    parts = callback.get('data', '').split(':')
    try:
        if parts == ['chats', 'refresh']:
            sources.request_refresh(TELEGRAM_DATA)
            answer('Обновление запрошено. Повтори /chats после ближайшего прохода сборщика.')
            return
        if len(parts) == 3 and parts[:2] == ['chats', 'view'] and parts[2] in ('selected', 'all'):
            menu['mode'], menu['query'] = parts[2], ''
            page, notice = 0, 'Список обновлён.'
        elif len(parts) == 3 and parts[:2] == ['chats', 'page']:
            page = int(parts[2])
            notice = 'Страница обновлена.'
        elif len(parts) == 5 and parts[:2] == ['chats', 'set'] and parts[3] in ('0', '1'):
            chat_id, page = int(parts[2]), int(parts[4])
            sources.set_selected(TELEGRAM_DATA, chat_id, parts[3] == '1')
            notice = 'Сбор включён.' if parts[3] == '1' else 'Сбор выключен. Сохранённые сообщения остаются в базе.'
        else:
            raise ValueError('Неизвестная кнопка. Отправь /chats.')
        content = render(menu['query'], page, menu.get('mode', 'all'))
    except (ValueError, RuntimeError) as error:
        answer(str(error))
        return
    answer(notice)
    if menu.get('fingerprint') != fingerprint(content):
        bot_request(config['token'], 'editMessageText', {
            'chat_id': config['chat_id'], 'message_id': menu['message_id'], **content,
        })
    menu['fingerprint'] = fingerprint(content)
    sources.write_json(path, menu)
