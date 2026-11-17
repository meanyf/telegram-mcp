"""Локальный сбор выбранных чатов Telegram: setup, run, status."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import getpass
import os
import sqlite3
import time


from .paths import TELEGRAM_DATA
from . import telegram_sources

DATA = TELEGRAM_DATA


def prepare_directory():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)


def open_database(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS cursors (
            chat_id INTEGER PRIMARY KEY, last_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            chat_title TEXT NOT NULL, sent_at TEXT NOT NULL,
            sender_id INTEGER, text TEXT NOT NULL, link TEXT,
            has_media INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
    ''')
    return db


def save_message(db, source, message):
    """Сообщение и курсор фиксируются вместе, только после получения текста."""
    username = source.get('username')
    chat_id = source['id']
    link = None
    if source.get('channel'):
        link = (f'https://t.me/{username}/{message.id}' if username else
                f'https://t.me/c/{str(chat_id).removeprefix("-100")}/{message.id}')
    with db:
        db.execute('''INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                   (chat_id, message.id, source['title'], message.date.isoformat(),
                    message.sender_id, message.raw_text or '', link,
                    int(bool(message.media))))
        db.execute('''INSERT INTO cursors VALUES (?, ?)
                      ON CONFLICT(chat_id) DO UPDATE
                      SET last_id = MAX(last_id, excluded.last_id)''',
                   (chat_id, message.id))


async def collect_source(client, db, source, since):
    row = db.execute('SELECT last_id FROM cursors WHERE chat_id = ?',
                     (source['id'],)).fetchone()
    options = {'min_id': row[0]} if row else {'offset_date': since}
    count = 0
    async for message in client.iter_messages(source['id'], reverse=True, **options):
        save_message(db, source, message)
        count += 1
    # Пустой чат при первом проходе остаётся с прежней нижней границей since.
    return count


def load_config():
    return telegram_sources.load_config(DATA)


def save_config(config):
    telegram_sources.save_config(DATA, config)


def display_login_qr(url, path):
    import qrcode
    from qrcode.image.svg import SvgPathImage

    code = qrcode.QRCode(border=4)
    code.add_data(url)
    code.make(fit=True)
    code.make_image(image_factory=SvgPathImage).save(str(path))
    print('Telegram на телефоне → Настройки → Устройства → Подключить устройство.')
    print('Отсканируйте QR ниже. Если он плохо виден, откройте файл:')
    print(path, flush=True)
    code.print_ascii(invert=True)


async def login(client):
    from telethon.errors import SessionPasswordNeededError

    if await client.is_user_authorized():
        return
    path = DATA / 'login-qr.svg'
    try:
        for attempt in range(3):
            qr = await client.qr_login()
            display_login_qr(qr.url, path)
            try:
                await qr.wait()
                print('Вход выполнен.')
                return
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass('Пароль двухэтапной проверки: '))
                print('Вход выполнен.')
                return
            except asyncio.TimeoutError:
                if attempt < 2:
                    print('QR истёк. Показываю новый — сканируйте последний QR.', flush=True)
        raise RuntimeError('Время входа истекло. Запустите setup снова, когда телефон будет готов.')
    finally:
        path.unlink(missing_ok=True)


async def setup():
    from telethon import TelegramClient, utils

    config = load_config() if (DATA / 'config.json').exists() else {}
    if not config:
        print('Получите api_id и api_hash: https://my.telegram.org → API development tools')
        config['api_id'] = int(input('api_id: ').strip())
        config['api_hash'] = getpass.getpass('api_hash (ввод скрыт): ').strip()
        if not config['api_hash']:
            raise ValueError('api_hash не указан.')
        save_config(config)
    client = TelegramClient(str(DATA / 'account'), config['api_id'], config['api_hash'],
                            receive_updates=True)
    await client.connect()
    try:
        await login(client)
        dialogs = [dialog async for dialog in client.iter_dialogs()]
        existing = {source['id']: source for source in config.get('sources', [])}
        mode = '1'
        if existing:
            print(f'Выбрано чатов: {len(existing)}')
            print('1. Добавить чаты к выбранным (по умолчанию)')
            print('2. Заменить список целиком')
            mode = input('Режим [1]: ').strip() or '1'
            if mode not in ('1', '2'):
                raise ValueError('Выберите режим 1 или 2.')
        for index, dialog in enumerate(dialogs, 1):
            selected = ' — уже выбран' if dialog.id in existing else ''
            print(f'{index}. {dialog.name} [{dialog.id}]{selected}')
        choices = sorted(set(int(part.strip()) for part in
                             input('Номера чатов через запятую: ').split(',')))
        if not choices or any(i < 1 or i > len(dialogs) for i in choices):
            raise ValueError('Укажите номера из списка.')
        sources = dict(existing) if mode == '1' else {}
        for index in choices:
            dialog = dialogs[index - 1]
            chat_id = utils.get_peer_id(dialog.entity)
            sources[chat_id] = {'id': chat_id, 'title': dialog.name,
                                'username': getattr(dialog.entity, 'username', None),
                                'channel': dialog.is_channel}
        config['sources'] = list(sources.values())
        # Общая граница первого запуска фиксируется, чтобы простой не терял сообщения.
        config.setdefault('since', (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        save_config(config)
        print(f'Сохранено источников: {len(sources)}. Теперь запустите команду run.')
    finally:
        await client.disconnect()


async def run(interval, once):
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError

    config = load_config()
    client = TelegramClient(str(DATA / 'account'), config['api_id'], config['api_hash'],
                            receive_updates=False)
    await client.connect()
    db = None
    try:
        if not await client.is_user_authorized():
            raise RuntimeError('Сессия недействительна. Выполните setup в терминале.')
        db = open_database(DATA / 'messages.sqlite3')
        next_catalog_refresh = 0
        last_refresh_token = None
        while True:
            token = telegram_sources.refresh_token(DATA)
            if time.monotonic() >= next_catalog_refresh or token != last_refresh_token:
                try:
                    await telegram_sources.refresh_catalog(client, DATA)
                    next_catalog_refresh = time.monotonic() + 300
                except FloodWaitError as error:
                    next_catalog_refresh = time.monotonic() + error.seconds + 1
                    print(f'Обновление списка чатов: пауза Telegram {error.seconds} с.', flush=True)
                    if not once:
                        await asyncio.sleep(error.seconds + 1)
                except Exception as error:
                    next_catalog_refresh = time.monotonic() + 60
                    print(f'Не удалось обновить список чатов: {type(error).__name__}.', flush=True)
                last_refresh_token = token
            config = load_config()
            since = datetime.fromisoformat(config.get('since') or
                                           (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
            failures = 0
            for source in config.get('sources', []):
                if source['id'] not in {item['id'] for item in load_config().get('sources', [])}:
                    continue
                try:
                    count = await collect_source(client, db, source, since)
                    print(f'{source["title"]}: сохранено {count}', flush=True)
                except FloodWaitError as error:
                    print(f'Telegram запросил паузу {error.seconds} с.', flush=True)
                    if once:
                        raise
                    await asyncio.sleep(error.seconds + 1)
                    failures += 1
                    break
                except Exception as error:
                    failures += 1
                    print(f'{source["title"]}: {type(error).__name__}: {error}', flush=True)
            if once:
                if failures:
                    raise RuntimeError('Не все источники собраны; повторный запуск продолжит сбор.')
                return
            await asyncio.sleep(interval)
    finally:
        if db is not None:
            db.close()
        await client.disconnect()


def status():
    path = DATA / 'messages.sqlite3'
    if not path.exists():
        print('Сообщений пока нет. Выполните setup, затем run.')
        return
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
        for title, count, latest in db.execute('''SELECT chat_title, COUNT(*), MAX(sent_at)
                                                 FROM messages GROUP BY chat_id'''):
            print(f'{title}: {count} сообщений, последнее {latest}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'run', 'status'])
    parser.add_argument('--once', action='store_true', help='Один проход сбора')
    parser.add_argument('--interval', type=int, default=60, help='Пауза между проходами, секунд')
    args = parser.parse_args()
    if args.interval < 10:
        parser.error('--interval должен быть не меньше 10 секунд')
    os.umask(0o077)
    prepare_directory()
    # Одновременно работать с Telegram-сессией должен только один процесс.
    import fcntl
    with (DATA / 'collector.lock').open('w') as lock:
        if args.command != 'status':
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Сборщик уже запущен. Остановите его перед setup/run.')
        if args.command == 'status':
            status()
        else:
            asyncio.run(setup() if args.command == 'setup' else run(args.interval, args.once))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nОстановлено.')
    except Exception as error:
        raise SystemExit(f'Ошибка: {error}')
