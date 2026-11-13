import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from telegram_collect import collect_source, open_database, save_message


SOURCE = {'id': -100123, 'title': 'Test', 'channel': True, 'username': None}


def message(number):
    return SimpleNamespace(id=number, date=datetime.now(timezone.utc),
                           sender_id=7, raw_text=f'Text {number}', media=None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = open_database(':memory:')
        self.addCleanup(self.db.close)

    def test_duplicate_does_not_duplicate_data_or_rewind_cursor(self):
        for number in (1, 2, 1):
            save_message(self.db, SOURCE, message(number))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT last_id FROM cursors').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT link FROM messages LIMIT 1').fetchone()[0],
                         'https://t.me/c/123/1')

    def test_failed_checkpoint_rolls_back_message_too(self):
        self.db.execute('''CREATE TRIGGER fail BEFORE INSERT ON cursors
                           BEGIN SELECT RAISE(ABORT, 'disk failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            save_message(self.db, SOURCE, message(1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_retry_after_disconnect_resumes_from_last_saved_message(self):
        class Client:
            fail = True
            options = []

            async def iter_messages(client, chat_id, **options):
                client.options.append(options)
                for number in (1, 2, 3):
                    if number <= options.get('min_id', 0):
                        continue
                    if number == 2 and client.fail:
                        raise ConnectionError('offline')
                    yield message(number)

        client = Client()
        since = datetime.now(timezone.utc)
        with self.assertRaises(ConnectionError):
            asyncio.run(collect_source(client, self.db, SOURCE, since))
        client.fail = False
        asyncio.run(collect_source(client, self.db, SOURCE, since))
        self.assertEqual(client.options[0]['offset_date'], since)
        self.assertEqual(client.options[1]['min_id'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 3)


class QRLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.data_patch = patch('telegram_collect.DATA', self.path)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.display_patch = patch('telegram_collect.display_login_qr',
                                   side_effect=lambda url, path: path.write_text('test QR'))
        self.display = self.display_patch.start()
        self.addCleanup(self.display_patch.stop)
        self.client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=False),
                                      qr_login=AsyncMock(), sign_in=AsyncMock())

    async def test_expired_qr_refreshes_and_file_is_removed_after_login(self):
        from telegram_collect import login
        expired = SimpleNamespace(url='first', wait=AsyncMock(side_effect=asyncio.TimeoutError))
        valid = SimpleNamespace(url='second', wait=AsyncMock())
        self.client.qr_login.side_effect = [expired, valid]
        await login(self.client)
        self.assertEqual(self.display.call_count, 2)
        self.assertFalse((self.path / 'login-qr.svg').exists())
        self.client.sign_in.assert_not_called()

    async def test_two_factor_password_stays_in_local_prompt(self):
        from telethon.errors import SessionPasswordNeededError
        from telegram_collect import login
        self.client.qr_login.return_value = SimpleNamespace(
            url='test', wait=AsyncMock(side_effect=SessionPasswordNeededError(None)))
        with patch('telegram_collect.getpass.getpass', return_value='test-password'):
            await login(self.client)
        self.client.sign_in.assert_awaited_once_with(password='test-password')
        self.assertFalse((self.path / 'login-qr.svg').exists())

    async def test_saved_session_skips_qr(self):
        from telegram_collect import login
        self.client.is_user_authorized.return_value = True
        await login(self.client)
        self.client.qr_login.assert_not_called()
        self.display.assert_not_called()


if __name__ == '__main__':
    unittest.main()
