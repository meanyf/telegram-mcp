"""Приём запросов выжимки и вопросов от владельца Telegram-бота."""

import fcntl
import logging
import sqlite3
import time

from ..events import EVENT_NAME, MESSAGE_EVENT_NAME, EventError, queue, storage
from .delivery import bot_request

logger = logging.getLogger(__name__)


def process_update(config, update):
    message = update.get('message', {})
    chat, sender = message.get('chat', {}), message.get('from', {})
    if (chat.get('type') != 'private' or chat.get('id') != config['chat_id']
            or sender.get('id') != config['chat_id'] or sender.get('is_bot', False)):
        return
    text = message.get('text', '').strip()
    command = text.split(maxsplit=1)[0].split('@', 1)[0] if text else ''
    if not text or (command.startswith('/') and command not in ('/summary', '/help', '/start')):
        return
    if time.time() - message.get('date', 0) > 300:
        return
    if command in ('/start', '/help'):
        reply = ('/summary — новая выжимка. Обычный текст — вопрос к ИИ. '
                 'Можно ответить на сообщение бота, чтобы уточнить его. '
                 'Ответы выполняет подключённое задание ChatGPT.')
    else:
        if command == '/summary':
            result = queue.enqueue_summary(update['update_id'])
            event_name = EVENT_NAME
            confirmation = 'Запрос выжимки сохранён. Передаю в ChatGPT; готовая сводка придёт сюда.'
        else:
            reply_to = message.get('reply_to_message', {})
            quote = None
            if (reply_to.get('from', {}).get('id') == config['bot_id']
                    and reply_to.get('text') and isinstance(reply_to.get('message_id'), int)):
                quote = {'message_id': reply_to['message_id'], 'text': reply_to['text']}
            result = queue.enqueue_message(update['update_id'], text, message['message_id'], quote)
            event_name = MESSAGE_EVENT_NAME
            confirmation = 'Вопрос сохранён. Передаю в ChatGPT; ответ придёт сюда.'
        if result == 'duplicate':
            return
        reply = (confirmation if result == 'queued' else
                 f'Нет активной подписки ChatGPT на {event_name}. '
                 'В том же чате ChatGPT, где делается выжимка, подключите MCP '
                 'и попросите подписаться на это событие и отвечать вам в Telegram.')
    bot_request(config['token'], 'sendMessage', {'chat_id': config['chat_id'], 'text': reply})


def poll_once(config):
    with storage.database() as db:
        row = db.execute('SELECT value FROM offsets WHERE bot_id = ?', (config['bot_id'],)).fetchone()
    updates = bot_request(config['token'], 'getUpdates', {
        'offset': row['value'] if row else 0, 'limit': 100, 'timeout': 10,
        'allowed_updates': ['message'],
    })
    for update in updates:
        process_update(config, update)
        with storage.database() as db:
            db.execute('INSERT INTO offsets VALUES (?, ?) ON CONFLICT(bot_id) '
                       'DO UPDATE SET value = MAX(value, excluded.value)',
                       (config['bot_id'], update['update_id'] + 1))


def run(stop):
    storage.TELEGRAM_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (storage.TELEGRAM_DATA / 'bot-poll.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.warning('Команды Telegram уже принимает другой процесс на этом компьютере.')
            return
        logger.info('Приём Telegram включён: /summary и текстовые вопросы.')
        while not stop.is_set():
            try:
                config = storage.bot_config()
                failed = queue.deliver_pending()
                if failed:
                    bot_request(config['token'], 'sendMessage', {
                        'chat_id': config['chat_id'],
                        'text': 'Не удалось передать один из запросов в ChatGPT. '
                                'Проверьте подписку и повторите вопрос или /summary.',
                    })
                poll_once(config)
            except (RuntimeError, EventError, OSError, sqlite3.Error) as error:
                logger.warning('Команды Telegram: %s', error)
                stop.wait(10)
