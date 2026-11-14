from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from telegram_mcp import telegram_bot as bot


class TelegramBotTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        override = patch.object(bot, 'TELEGRAM_DATA', self.path)
        override.start()
        self.addCleanup(override.stop)
        (self.path / 'bot.json').write_text(json.dumps({
            'token': '123:secret', 'bot_id': 123, 'chat_id': 456}))

    def test_unicode_parts_fit_and_preserve_text(self):
        text = ('😀Текст\n' * 2000)
        parts = bot.split_text(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(part.encode('utf-16-le')) // 2 <= 4000 for part in parts))

    def test_fixed_recipient_and_repeated_summary_not_sent_twice(self):
        with patch.object(bot, 'bot_request', return_value={'message_id': 77}) as api, \
                patch.object(bot.time, 'sleep'):
            result = bot.send_summary('x' * 5000)
            self.assertEqual(result['parts'], 2)
            self.assertEqual(api.call_count, 2)
            for call in api.call_args_list:
                self.assertEqual(call.args[2]['chat_id'], 456)
                self.assertNotIn('parse_mode', call.args[2])
            self.assertTrue(bot.send_summary('x' * 5000)['already_sent'])
            self.assertEqual(api.call_count, 2)
            self.assertNotIn('secret', json.dumps(result))

    def test_retry_resumes_after_confirmed_parts(self):
        text = 'x' * 5000
        with patch.object(bot, 'bot_request', side_effect=[{'message_id': 1}, RuntimeError('offline')]), \
                patch.object(bot.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                bot.send_summary(text)
        with patch.object(bot, 'bot_request', return_value={'message_id': 2}) as api:
            result = bot.send_summary(text)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(result['message_ids'], [1, 2])

    def test_network_error_does_not_include_token(self):
        with patch.object(bot, 'urlopen', side_effect=URLError('https://api.telegram.org/bot123:secret')):
            with self.assertRaises(RuntimeError) as error:
                bot.bot_request('123:secret', 'getMe')
        self.assertNotIn('secret', str(error.exception))

    def test_setup_selects_private_chat_and_does_not_send_anything(self):
        updates = [
            {'message': {'chat': {'id': -1, 'type': 'group'}}},
            {'message': {'chat': {'id': 999, 'type': 'private', 'first_name': 'Owner'},
                         'from': {'is_bot': False}}},
        ]
        with patch.object(bot.getpass, 'getpass', return_value='123:secret'), \
                patch('builtins.input', side_effect=['']), \
                patch('sys.stdout', new=StringIO()), \
                patch.object(bot, 'bot_request', side_effect=[
                    {'id': 123, 'username': 'test_bot'}, {'url': ''}, updates]) as api:
            bot.setup()
        config = json.loads((self.path / 'bot.json').read_text())
        self.assertEqual(config['chat_id'], 999)
        self.assertEqual((self.path / 'bot.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual([call.args[1] for call in api.call_args_list],
                         ['getMe', 'getWebhookInfo', 'getUpdates'])


if __name__ == '__main__':
    unittest.main()
