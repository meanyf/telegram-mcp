"""Интерфейс просмотра и редактирования пожеланий к анализу."""

import json
import secrets
from urllib.parse import urlencode

from .. import instructions, sources
from ..paths import TELEGRAM_DATA
from .delivery import bot_request


def draft_url(username, text):
    text = text.strip()
    instructions.validate_preferences(text)
    return f'https://t.me/{username}?' + urlencode({'text': text})


def session(config):
    path = TELEGRAM_DATA / 'prompt-edit.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get('bot_id') == config['bot_id'] and state.get('chat_id') == config['chat_id']:
        return state
    return {}


def store(config, **state):
    sources.write_json(TELEGRAM_DATA / 'prompt-edit.json', {
        'bot_id': config['bot_id'], 'chat_id': config['chat_id'], **state})


def send(config, text, **parameters):
    return bot_request(config['token'], 'sendMessage', {
        'chat_id': config['chat_id'], 'text': text, **parameters})


def show(config):
    text = instructions.read_preferences()
    try:
        username = bot_request(config['token'], 'getMe', {})['username']
        url = draft_url(username, text)
    except ValueError as error:
        send(config, str(error))
        return
    token = secrets.token_hex(8)
    send(config, 'Твои пожелания к анализу. Здесь можно указать интересующие темы, '
         'что пропускать и как оформлять выжимку.')
    send(config, text)
    sent = send(config, 'Нажми «Редактировать пожелания», поправь текст в поле ввода и отправь. '
                'Следующее обычное сообщение станет черновиком; затем можно сохранить его или отменить. '
                'Чтобы вернуться к общению с ИИ, нажми «Отмена» или отправь /cancel.',
                reply_markup={'inline_keyboard': [
                    [{'text': 'Редактировать пожелания', 'url': url}],
                    [{'text': 'Отмена', 'callback_data': f'prompt:cancel:{token}'}],
                ]})
    store(config, phase='input', token=token, confirmation_id=sent['message_id'])


def cancel(config):
    store(config, phase='closed')
    send(config, 'Редактирование отменено.')


def handle_callback(config, callback):
    message = callback.get('message', {})
    owner = (callback.get('from', {}).get('id') == config['chat_id']
             and message.get('chat', {}).get('id') == config['chat_id']
             and message.get('chat', {}).get('type') == 'private'
             and message.get('from', {}).get('id') == config['bot_id'])
    bot_request(config['token'], 'answerCallbackQuery', {
        'callback_query_id': callback['id'],
        'text': 'Готово.' if owner else 'Только для владельца бота.',
    })
    if not owner:
        return
    action = callback.get('data', '')
    state = session(config)
    if action == 'prompt:edit':
        show(config)
    elif (action in (f'prompt:save:{state.get("token")}', f'prompt:cancel:{state.get("token")}')
          and state.get('token') and message.get('message_id') == state.get('confirmation_id')):
        if action.startswith('prompt:cancel:'):
            cancel(config)
        elif state.get('phase') == 'confirm':
            instructions.save_preferences(state['candidate'])
            store(config, **{**state, 'phase': 'saved'})
            send(config, 'Пожелания сохранены. Они применятся при следующем получении инструкций ИИ.')
        else:
            send(config, 'Эти изменения уже обработаны. Открой /prompt.')
    else:
        send(config, 'Эта кнопка устарела. Открой /prompt заново.')


def handle_reply(config, message):
    state = session(config)
    text = message['text']
    # Old drafts must never become questions to the AI or replace preferences.
    if text.split(maxsplit=1)[0].split('@', 1)[0] == '/save_prompt':
        send(config, 'Этот черновик устарел. Открой /prompt заново.')
        return True
    if state.get('phase') not in ('input', 'confirm'):
        return False
    if text.lstrip().startswith('/'):
        return False
    if state.get('candidate_message_id') == message['message_id']:
        return True
    try:
        instructions.validate_preferences(text)
    except ValueError as error:
        send(config, str(error))
        return True
    token = secrets.token_hex(8)
    send(config, 'Новые пожелания:\n\n' + text.strip())
    confirmation = send(config, 'Сохранить эти пожелания? До сохранения можешь прислать другой вариант.',
                        reply_markup={'inline_keyboard': [[
                            {'text': 'Сохранить', 'callback_data': f'prompt:save:{token}'},
                            {'text': 'Отмена', 'callback_data': f'prompt:cancel:{token}'},
                        ]]})
    store(config, **{**state, 'phase': 'confirm', 'token': token, 'candidate': text,
                     'candidate_message_id': message['message_id'],
                     'confirmation_id': confirmation['message_id']})
    return True
