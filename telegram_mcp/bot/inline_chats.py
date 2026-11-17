"""Поиск источников по мере ввода через встроенный inline-режим Telegram."""

from .. import telegram_sources as sources
from ..paths import TELEGRAM_DATA
from .delivery import bot_request

PAGE_SIZE = 30
ADD_COMMAND = '/collect_chat'


def answer(config, query):
    parameters = {'inline_query_id': query['id'], 'results': [],
                  'cache_time': 0, 'is_personal': True, 'next_offset': ''}
    # The catalog belongs to one account. Never offer it in someone else's chat.
    if (query.get('from', {}).get('id') != config['chat_id']
            or query.get('chat_type') not in (None, 'sender')):
        bot_request(config['token'], 'answerInlineQuery', parameters)
        return
    try:
        offset = int(query.get('offset') or '0')
        if offset < 0:
            raise ValueError
    except ValueError:
        bot_request(config['token'], 'answerInlineQuery', parameters)
        return
    selected = {item['id'] for item in sources.load_config(TELEGRAM_DATA).get('sources', [])}
    matches = sources.search_chats(sources.catalog(TELEGRAM_DATA)['chats'], query.get('query', ''))
    for item in matches[offset:offset + PAGE_SIZE]:
        enabled = item['id'] in selected
        title = str(item.get('title') or item['id'])
        username = '@' + item['username'] if item.get('username') else ''
        parameters['results'].append({
            'type': 'article', 'id': str(item['id']),
            'title': ('✅ ' if enabled else '⬜ ') + title,
            'description': ('Уже выбран' if enabled else 'Нажми, чтобы включить сбор') +
                           (f' · {username}' if username else ''),
            # Selecting a result sends this command to the bot's private chat.
            # Processing the message avoids requiring inline-feedback delivery.
            'input_message_content': {'message_text': f'{ADD_COMMAND} {item["id"]}'},
        })
    if offset + PAGE_SIZE < len(matches):
        parameters['next_offset'] = str(offset + PAGE_SIZE)
    bot_request(config['token'], 'answerInlineQuery', parameters)


def select(config, message):
    """Обрабатывать только результат нашего бота, отправленный владельцем в ЛС."""
    if message.get('via_bot', {}).get('id') != config['bot_id']:
        return
    from . import chats

    try:
        parts = message['text'].split()
        if len(parts) != 2:
            raise ValueError('Открой /chats и выбери чат заново.')
        sources.set_selected(TELEGRAM_DATA, int(parts[1]), True)
    except (ValueError, RuntimeError) as error:
        bot_request(config['token'], 'sendMessage', {'chat_id': config['chat_id'], 'text': str(error)})
        return
    chats.open_menu(config)
