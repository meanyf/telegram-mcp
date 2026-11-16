"""Приём /summary от владельца бота; работает в фоне MCP-процесса."""

import fcntl
import logging
import sqlite3
import time

from ..events import EventError, queue, storage
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
    if command not in ('/summary', '/help', '/start'):
        return
    if time.time() - message.get('date', 0) > 300:
        return
    if command in ('/start', '/help'):
        reply = 'Для запроса выжимки отправьте /summary. Анализ выполняет подключённое задание ChatGPT.'
    else:
        result = queue.enqueue_summary(update['update_id'])
        if result == 'duplicate':
            return
        reply = ('Запрос выжимки сохранён. Отправляю событие в ChatGPT; готовая сводка придёт сюда.'
                 if result == 'queued' else
                 'Нет активной подписки ChatGPT. В ChatGPT подключите MCP и попросите '
                 'реагировать на summary.requested: анализировать новые сообщения и отправлять сводку в Telegram.')
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
        logger.info('Приём команд Telegram включён: /summary.')
        while not stop.is_set():
            try:
                config = storage.bot_config()
                failed = queue.deliver_pending()
                if failed:
                    bot_request(config['token'], 'sendMessage', {
                        'chat_id': config['chat_id'],
                        'text': 'Не удалось передать запрос сводки в ChatGPT. '
                                'Проверьте подписку на summary.requested и отправьте /summary заново.',
                    })
                poll_once(config)
            except (RuntimeError, EventError, OSError, sqlite3.Error) as error:
                logger.warning('Команды Telegram: %s', error)
                stop.wait(10)
