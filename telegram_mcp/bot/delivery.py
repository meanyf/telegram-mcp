"""Настройка бота и доставка сводки только заранее выбранному получателю."""

import argparse
from contextlib import closing
import fcntl
import getpass
import hashlib
import json
import os
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..paths import TELEGRAM_DATA


def bot_request(token, method, parameters=None):
    request = Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(parameters or {}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except HTTPError as error:
        if method in ('editMessageText', 'answerCallbackQuery', 'answerInlineQuery') and error.code == 400:
            try:
                with error:
                    description = json.load(error).get('description', '')
            except (ValueError, AttributeError):
                description = ''
            if method == 'editMessageText' and description.startswith('Bad Request: message is not modified'):
                return {'message_id': parameters['message_id']}
            if method in ('answerCallbackQuery', 'answerInlineQuery') and description.startswith('Bad Request: query is too old'):
                return False
        # В URL есть токен: не выводим исходное исключение или тело ответа.
        raise RuntimeError(f'Telegram Bot API: HTTP {error.code}. Проверьте токен и доступ бота к чату.') from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError('Не удалось получить ответ Telegram Bot API. Проверьте интернет.') from None
    except ValueError:
        raise RuntimeError('Telegram Bot API вернул некорректный ответ.') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram Bot API отклонил запрос.')
    return result['result']


def setup():
    TELEGRAM_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (TELEGRAM_DATA / 'bot-poll.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Перед настройкой бота остановите MCP: он принимает команды Telegram.') from None
        _setup()


def _setup():
    token = getpass.getpass('Токен от BotFather (ввод скрыт): ').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise ValueError('Неверный формат токена.')
    bot = bot_request(token, 'getMe')
    print(f'Бот: @{bot["username"]}')
    if bot_request(token, 'getWebhookInfo').get('url'):
        raise RuntimeError('У бота уже настроен webhook. Используйте отдельного бота для сводок.')
    input('Нажмите Start или отправьте сообщение этому боту в Telegram, затем Enter здесь: ')
    updates = bot_request(token, 'getUpdates', {'limit': 100, 'timeout': 0,
                                              'allowed_updates': ['message']})
    chats = {}
    for update in updates:
        message = update.get('message', {})
        chat = message.get('chat', {})
        if chat.get('type') == 'private' and not message.get('from', {}).get('is_bot', False):
            chats[chat['id']] = chat
    if not chats:
        raise RuntimeError('Личный чат не найден. Отправьте боту сообщение и повторите setup.')
    choices = list(chats.values())
    choice = 1
    if len(choices) == 1:
        print('Найден один личный чат с ботом. Сводки будут приходить туда.')
    else:
        for number, chat in enumerate(choices, 1):
            print(f'{number}. {chat.get("first_name", "Пользователь")} '
                  f'{"@" + chat["username"] if chat.get("username") else ""}')
        choice = int(input('Номер вашего чата для доставки: '))
        if not 1 <= choice <= len(choices):
            raise ValueError('Выберите номер из списка.')
    config = {'token': token, 'bot_id': bot['id'], 'chat_id': choices[choice - 1]['id']}
    TELEGRAM_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    TELEGRAM_DATA.chmod(0o700)
    path = TELEGRAM_DATA / 'bot.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(config), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)
    print('Получатель сохранён. Токен остаётся только в локальном bot.json.')


def split_text(text):
    # Консервативный лимит: 4000 единиц UTF-16, без разрыва emoji.
    parts, current, units = [], [], 0
    for character in text:
        size = 2 if ord(character) > 0xffff else 1
        if units + size > 4000:
            parts.append(''.join(current))
            current, units = [], 0
        current.append(character)
        units += size
    if current:
        parts.append(''.join(current))
    return parts


def send_summary(text):
    return _send_text(text)


def send_reply(request_id, text):
    from ..events.queue import request_message_id

    message_id = request_message_id(request_id)
    return _send_text(text, request_id=request_id, reply_to_message_id=message_id)


def _send_text(text, *, request_id=None, reply_to_message_id=None):
    if not text.strip():
        raise ValueError('Текст сообщения пустой.')
    path = TELEGRAM_DATA / 'bot.json'
    if not path.exists():
        raise ValueError('Сначала выполните: .venv/bin/python -m telegram_mcp.bot setup')
    config = json.loads(path.read_text(encoding='utf-8'))
    parts = split_text(text)
    identity = f'{config["bot_id"]}:{config["chat_id"]}:{text}'
    if request_id is not None:
        identity += f':request:{request_id}'
    key = hashlib.sha256(identity.encode()).hexdigest()
    # Сериализуем отправки, чтобы повторный вызов не дублировал уже доставленные части.
    with (TELEGRAM_DATA / 'bot-send.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        outbox = TELEGRAM_DATA / 'bot-outbox.sqlite3'
        outbox.touch(mode=0o600, exist_ok=True)
        with closing(sqlite3.connect(outbox)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS sent_parts
                          (report_key TEXT, part INTEGER, message_id INTEGER,
                           PRIMARY KEY(report_key, part))''')
            message_ids = []
            sent_now = 0
            for index, part in enumerate(parts):
                saved = db.execute('SELECT message_id FROM sent_parts WHERE report_key = ? AND part = ?',
                                   (key, index)).fetchone()
                if saved:
                    message_ids.append(saved[0])
                    continue
                if sent_now:
                    time.sleep(1.1)
                try:
                    parameters = {
                        'chat_id': config['chat_id'], 'text': part,
                        'link_preview_options': {'is_disabled': True},
                    }
                    if reply_to_message_id is not None:
                        parameters['reply_parameters'] = {
                            'message_id': reply_to_message_id, 'allow_sending_without_reply': True,
                        }
                    message = bot_request(config['token'], 'sendMessage', parameters)
                except RuntimeError as error:
                    raise RuntimeError(f'Отправка остановлена; подтверждено частей: {len(message_ids)}. {error}') from None
                with db:
                    db.execute('INSERT INTO sent_parts VALUES (?, ?, ?)', (key, index, message['message_id']))
                message_ids.append(message['message_id'])
                sent_now += 1
    return {'sent': True, 'parts': len(parts), 'already_sent': sent_now == 0,
            'message_ids': message_ids}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'test'])
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'setup':
        setup()
    else:
        result = send_summary('Тест: доставка сводок из MCP в Telegram работает.')
        print('Тест уже отправлялся.' if result['already_sent'] else 'Тест отправлен в Telegram.')

