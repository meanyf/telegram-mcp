"""Приём запросов выжимки и вопросов от владельца Telegram-бота."""

import fcntl
import logging
import sqlite3
import time

from ..events import EVENT_NAME, MESSAGE_EVENT_NAME, EventError, queue, storage
from ..paths import TELEGRAM_DATA
from . import chat_menu, inline_chats, prompt_menu
from .delivery import bot_request

logger = logging.getLogger(__name__)


def confirm_request(config, update_id, text, *, retry=False):
    """Повторить неподтверждённую отправку, не создавая новое событие."""
    with storage.database() as db:
        sent = db.execute('SELECT 1 FROM command_confirmations WHERE bot_id = ? AND update_id = ?',
                          (config['bot_id'], update_id)).fetchone()
    if sent:
        return
    if retry:
        logger.info('TIMING update=%s stage=confirmation_retry', update_id)
    bot_request(config['token'], 'sendMessage', {'chat_id': config['chat_id'], 'text': text})
    with storage.database() as db:
        db.execute('INSERT OR IGNORE INTO command_confirmations VALUES (?, ?)', (config['bot_id'], update_id))
    logger.info('TIMING update=%s stage=confirmation_sent', update_id)


def register_menu(config):
    """Зарегистрировать подсказки команд и кнопку меню в личном чате владельца."""
    bot_request(config['token'], 'setMyCommands', {
        'scope': {'type': 'chat', 'chat_id': config['chat_id']},
        'commands': [
            {'command': 'summary', 'description': 'Получить новую выжимку'},
            {'command': 'chats', 'description': 'Выбрать чаты для сбора'},
            {'command': 'prompt', 'description': 'Настроить пожелания к анализу'},
            {'command': 'help', 'description': 'Показать справку'},
        ],
    })
    bot_request(config['token'], 'setChatMenuButton', {
        'chat_id': config['chat_id'], 'menu_button': {'type': 'commands'},
    })


def process_update(config, update):
    if 'inline_query' in update:
        inline_chats.answer(config, update['inline_query'])
        return
    if 'callback_query' in update:
        callback = update['callback_query']
        if callback.get('data', '').startswith('prompt:'):
            prompt_menu.handle_callback(config, callback)
        else:
            chat_menu.handle_callback(config, callback)
        return
    message = update.get('message', {})
    chat, sender = message.get('chat', {}), message.get('from', {})
    if (chat.get('type') != 'private' or chat.get('id') != config['chat_id']
            or sender.get('id') != config['chat_id'] or sender.get('is_bot', False)):
        return
    text = message.get('text', '').strip()
    command = text.split(maxsplit=1)[0].split('@', 1)[0] if text else ''
    if not text:
        return
    if time.time() - message.get('date', 0) > 300:
        queued = False
        if command == '/summary':
            # Retry an acknowledgment even after a long outage if the request was already saved.
            with storage.database() as db:
                queued = db.execute('SELECT 1 FROM requests WHERE owner = ? AND update_id = ?',
                                    (f'{config["bot_id"]}:{config["chat_id"]}', update['update_id'])).fetchone()
            if not queued:
                logger.warning('TIMING update=%s stage=summary_ignored reason=older_than_300_seconds',
                               update['update_id'])
        if not queued:
            return
    if command == '/cancel':
        prompt_menu.cancel(config)
        return
    if prompt_menu.handle_reply(config, message):
        return
    if command.startswith('/') and command not in ('/summary', '/help', '/start', '/chats', '/prompt', inline_chats.ADD_COMMAND):
        return
    if command == '/prompt':
        prompt_menu.show(config)
        return
    if command == inline_chats.ADD_COMMAND:
        inline_chats.select(config, message)
        return
    if command == '/chats':
        query = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ''
        try:
            chat_menu.open_menu(config, query)
        except (ValueError, RuntimeError) as error:
            bot_request(config['token'], 'sendMessage', {'chat_id': config['chat_id'], 'text': str(error)})
        return
    if command in ('/start', '/help'):
        reply = ('/summary — новая выжимка. /chats — выбор чатов для сбора. '
                 '/prompt — пожелания к анализу. '
                 'Обычный текст — вопрос к ИИ. '
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
        if result in ('queued', 'duplicate'):
            confirm_request(config, update['update_id'], confirmation, retry=result == 'duplicate')
            return
        reply = (f'Нет активной подписки ChatGPT на {event_name}. '
                 'В том же чате ChatGPT, где делается выжимка, подключите MCP '
                 'и попросите подписаться на это событие и отвечать вам в Telegram.')
    bot_request(config['token'], 'sendMessage', {'chat_id': config['chat_id'], 'text': reply})


def poll_once(config):
    with storage.database() as db:
        row = db.execute('SELECT value FROM offsets WHERE bot_id = ?', (config['bot_id'],)).fetchone()
    updates = bot_request(config['token'], 'getUpdates', {
        'offset': row['value'] if row else 0, 'limit': 100, 'timeout': 10,
        'allowed_updates': ['message', 'callback_query', 'inline_query'],
    })
    for update in updates:
        message = update.get('message', {})
        if (message.get('chat', {}).get('id') == config['chat_id']
                and message.get('from', {}).get('id') == config['chat_id']):
            text = message.get('text', '').strip()
            command = text.split(maxsplit=1)[0].split('@', 1)[0] if text else ''
            if command == '/summary':
                logger.info('TIMING update=%s stage=summary_received age_seconds=%.3f',
                            update['update_id'], time.time() - message.get('date', 0))
        process_update(config, update)
        with storage.database() as db:
            db.execute('INSERT INTO offsets VALUES (?, ?) ON CONFLICT(bot_id) '
                       'DO UPDATE SET value = MAX(value, excluded.value)',
                       (config['bot_id'], update['update_id'] + 1))


def run(stop):
    TELEGRAM_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (TELEGRAM_DATA / 'bot-poll.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.warning('Команды Telegram уже принимает другой процесс на этом компьютере.')
            return
        logger.info('Приём Telegram включён: /summary и текстовые вопросы.')
        menu_owner = None
        while not stop.is_set():
            try:
                config = storage.bot_config()
                owner = (config['bot_id'], config['chat_id'])
                if menu_owner != owner:
                    try:
                        register_menu(config)
                    except RuntimeError as error:
                        logger.warning('Не удалось обновить меню Telegram: %s', error)
                    else:
                        menu_owner = owner
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
