from io import BytesIO, StringIO
from http.client import IncompleteRead
import fcntl
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from telegram_mcp.bot import delivery as bot


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

    def test_timeout_records_method_type_and_duration_without_raw_error(self):
        with patch.object(bot, 'urlopen', side_effect=URLError(TimeoutError('123:secret'))), \
                patch.object(bot.time, 'monotonic', side_effect=[10.0, 30.0]):
            with self.assertRaises(RuntimeError) as error:
                bot.bot_request('123:secret', 'getUpdates')
        text = str(error.exception)
        self.assertIn('method=getUpdates', text)
        self.assertIn('type=TimeoutError', text)
        self.assertIn('duration_seconds=20.000', text)
        self.assertNotIn('secret', text)

    def test_http_failure_records_method_and_status_without_response_body(self):
        error = HTTPError('https://api.telegram.org/bot123:secret', 429, 'private response', {},
                          BytesIO(b'{"description":"123:secret private message"}'))
        with patch.object(bot, 'urlopen', side_effect=error):
            with self.assertRaises(RuntimeError) as result:
                bot.bot_request('123:secret', 'sendMessage', {'text': 'private message'})
        self.assertIn('method=sendMessage', str(result.exception))
        self.assertIn('HTTP 429', str(result.exception))
        self.assertNotIn('private', str(result.exception))
        self.assertNotIn('secret', str(result.exception))

    def test_truncated_response_is_recoverable_and_does_not_expose_partial_content(self):
        with patch.object(bot, 'urlopen', side_effect=IncompleteRead(b'123:secret', 100)):
            with self.assertRaisesRegex(RuntimeError, 'type=IncompleteRead') as result:
                bot.bot_request('123:secret', 'getUpdates')
        self.assertNotIn('secret', str(result.exception))

    def test_malformed_response_structure_does_not_crash_polling_with_key_error(self):
        for response in ([], {'ok': True}, {'ok': False, 'description': '123:secret'}):
            with self.subTest(response=response), \
                    patch.object(bot, 'urlopen', return_value=BytesIO(json.dumps(response).encode())):
                with self.assertRaises(RuntimeError) as result:
                    bot.bot_request('123:secret', 'getUpdates')
                self.assertIn('method=getUpdates', str(result.exception))
                self.assertNotIn('secret', str(result.exception))

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

    def test_setup_refuses_to_compete_with_running_command_listener(self):
        with (self.path / 'bot-poll.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(bot, 'bot_request') as api:
                with self.assertRaisesRegex(RuntimeError, 'остановите MCP'):
                    bot.setup()
                api.assert_not_called()

    def test_same_reply_text_is_sent_for_each_question_but_not_twice_for_one_request(self):
        with patch('telegram_mcp.events.queue.request_message_id', return_value=50), \
                patch.object(bot, 'bot_request', return_value={'message_id': 77}) as api:
            bot.send_reply('request-1', 'Привет!')
            self.assertTrue(bot.send_reply('request-1', 'Привет!')['already_sent'])
            bot.send_reply('request-2', 'Привет!')
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args.args[2]['reply_parameters']['message_id'], 50)

    def test_reply_with_unknown_request_does_not_send_anything(self):
        with patch('telegram_mcp.events.queue.request_message_id', side_effect=ValueError('Unknown')), \
                patch.object(bot, 'bot_request') as api:
            with self.assertRaises(ValueError):
                bot.send_reply('missing', 'Ответ')
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
