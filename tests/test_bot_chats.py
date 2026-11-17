import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

from telegram_mcp import telegram_collector as collector, telegram_sources as sources
from telegram_mcp.bot import chats, commands, delivery, inline_chats
from telegram_mcp.events import storage


class ChatSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        for module in (storage, chats, inline_chats, commands):
            override = patch.object(module, 'TELEGRAM_DATA', self.path)
            override.start()
            self.addCleanup(override.stop)
        self.bot = {'token': '123:secret', 'bot_id': 123, 'chat_id': 456}
        self.config = {'api_id': 123, 'api_hash': 'private-key',
                       'since': '2026-11-01T00:00:00+00:00', 'sources': []}
        sources.save_config(self.path, self.config)
        self.catalog = [{'id': number, 'title': f'Chat {number:02}', 'channel': False, 'username': None}
                        for number in range(1, 18)]
        sources.write_json(self.path / 'dialogs.json', {'chats': self.catalog, 'updated_at': '2026-11-17'})
        self.api_patch = patch.object(chats, 'bot_request', return_value={'message_id': 50})
        self.api = self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def update(self, text='/chats', sender=456):
        return {'update_id': 1, 'message': {'message_id': 10, 'text': text, 'date': int(time.time()),
                'chat': {'id': 456, 'type': 'private'}, 'from': {'id': sender}}}

    def callback(self, data, *, sender=456, message_id=50):
        return {'id': 'callback', 'data': data, 'from': {'id': sender}, 'message': {
            'message_id': message_id, 'chat': {'id': 456, 'type': 'private'}, 'from': {'id': 123}}}

    def test_chats_command_opens_buttons_without_calling_ai(self):
        for chat_id in (2, 4, 6):
            sources.set_selected(self.path, chat_id, True)
        with patch.object(commands.queue, 'enqueue_message') as question, \
                patch.object(commands.queue, 'enqueue_summary') as summary:
            commands.process_update(self.bot, self.update())
            question.assert_not_called()
            summary.assert_not_called()
        parameters = self.api.call_args.args[2]
        self.assertIn('Выбранные чаты: 3', parameters['text'])
        self.assertNotIn('Страница', parameters['text'])
        self.assertNotIn('private-key', json.dumps(parameters))
        self.assertEqual(parameters['chat_id'], 456)
        keyboard = parameters['reply_markup']['inline_keyboard']
        rows = [row for row in keyboard if row[0].get('callback_data', '').startswith('chats:set:')]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row[0]['text'].startswith('✅') for row in rows))
        self.assertTrue(all(len(button.get('callback_data', '').encode()) <= 64 for row in keyboard for button in row))

    def test_all_chats_are_separate_and_paginated(self):
        chats.open_menu(self.bot)
        chats.handle_callback(self.bot, self.callback('chats:view:all'))
        content = self.api.call_args.args[2]
        self.assertIn('Все чаты: 17', content['text'])
        self.assertIn('Страница 1/3', content['text'])
        chats.handle_callback(self.bot, self.callback('chats:page:1'))
        self.assertIn('Страница 2/3', self.api.call_args.args[2]['text'])
        chats.handle_callback(self.bot, self.callback('chats:view:selected'))
        self.assertIn('Пока нет выбранных чатов', self.api.call_args.args[2]['text'])

    def test_add_button_starts_inline_search_without_prompt(self):
        chats.open_menu(self.bot)
        keyboard = self.api.call_args.args[2]['reply_markup']['inline_keyboard']
        button = next(row[0] for row in keyboard if row[0]['text'] == 'Добавить чат')
        self.assertEqual(button, {'text': 'Добавить чат', 'switch_inline_query_current_chat': ''})

    def inline_query(self, text='', offset='', sender=456, chat_type='sender'):
        return {'inline_query': {'id': 'inline-id', 'from': {'id': sender},
                'query': text, 'offset': offset, 'chat_type': chat_type}}

    def test_inline_empty_query_scroll_filter_and_personal_cache(self):
        sources.write_json(self.path / 'dialogs.json', {
            'chats': [{'id': n, 'title': f'Роман {n:02}', 'username': f'user{n}', 'channel': False}
                      for n in range(65)], 'updated_at': 'now'})
        with patch.object(inline_chats, 'bot_request') as api, \
                patch.object(commands.queue, 'enqueue_message') as ai:
            commands.process_update(self.bot, self.inline_query())
            first = api.call_args.args[2]
            self.assertEqual(len(first['results']), 30)
            self.assertEqual(first['next_offset'], '30')
            self.assertTrue(first['is_personal'])
            self.assertEqual(first['cache_time'], 0)
            commands.process_update(self.bot, self.inline_query(offset='30'))
            second = api.call_args.args[2]
            self.assertFalse({x['id'] for x in first['results']} & {x['id'] for x in second['results']})
            commands.process_update(self.bot, self.inline_query(offset='60'))
            self.assertEqual(len(api.call_args.args[2]['results']), 5)
            self.assertEqual(api.call_args.args[2]['next_offset'], '')
            for query in ('Роман 02', '@user2'):
                commands.process_update(self.bot, self.inline_query(query))
                self.assertIn('2', [x['id'] for x in api.call_args.args[2]['results']])
            ai.assert_not_called()

    def test_inline_foreign_users_other_chats_and_bad_offsets_get_no_catalog(self):
        with patch.object(inline_chats, 'bot_request') as api:
            for update in (self.inline_query(sender=999), self.inline_query(chat_type='group'),
                           self.inline_query(chat_type='private'), self.inline_query(offset='bad'),
                           self.inline_query(offset='-1')):
                commands.process_update(self.bot, update)
                self.assertEqual(api.call_args.args[2]['results'], [])

    def test_inline_search_prioritizes_exact_prefix_and_word_before_substring(self):
        names = ['Герман', 'Максим', 'Александр Максимов', 'Ма', 'Роман']
        catalog = [{'id': n, 'title': title, 'username': None} for n, title in enumerate(names)]
        sources.write_json(self.path / 'dialogs.json', {'chats': catalog, 'updated_at': 'now'})
        with patch.object(inline_chats, 'bot_request') as api:
            commands.process_update(self.bot, self.inline_query('МА'))
            self.assertEqual([item['id'] for item in api.call_args.args[2]['results']], ['3', '1', '2', '0', '4'])
            catalog[4]['username'] = 'maxim'
            sources.write_json(self.path / 'dialogs.json', {'chats': catalog, 'updated_at': 'now'})
            commands.process_update(self.bot, self.inline_query('@max'))
            self.assertEqual([item['id'] for item in api.call_args.args[2]['results']], ['4'])

    def test_inline_selection_enables_chat_without_feedback_or_ai_and_is_idempotent(self):
        with patch.object(inline_chats, 'bot_request') as api:
            commands.process_update(self.bot, self.inline_query('Chat 02'))
            text = api.call_args.args[2]['results'][0]['input_message_content']['message_text']
        update = self.update(text)
        update['message']['via_bot'] = {'id': 123}
        with patch.object(commands.queue, 'enqueue_message') as ai:
            commands.process_update(self.bot, update)
            commands.process_update(self.bot, update)
            ai.assert_not_called()
        self.assertEqual([x['id'] for x in sources.load_config(self.path)['sources']], [2])
        self.assertIn('Выбранные чаты: 1', self.api.call_args.args[2]['text'])

    def test_inline_selection_rejects_foreign_sender_bot_or_unknown_chat(self):
        with patch.object(inline_chats, 'bot_request'), patch.object(commands.queue, 'enqueue_message') as ai:
            for sender, bot_id, chat_id in ((999, 123, 2), (456, 999, 2), (456, 123, 999)):
                update = self.update(f'/collect_chat {chat_id}', sender=sender)
                update['message']['via_bot'] = {'id': bot_id}
                commands.process_update(self.bot, update)
            ai.assert_not_called()
        self.assertEqual(sources.load_config(self.path)['sources'], [])

    def test_legacy_add_button_requests_fresh_menu_without_search_prompt(self):
        chats.open_menu(self.bot)
        self.api.reset_mock()
        chats.handle_callback(self.bot, self.callback('chats:add'))
        self.assertEqual(self.api.call_args.args[1], 'answerCallbackQuery')
        self.assertIn('/chats', self.api.call_args.args[2]['text'])
        self.assertEqual(sources.load_config(self.path)['sources'], [])

    def test_button_and_inline_search_use_same_ranking(self):
        catalog = [{'id': n, 'title': title, 'username': None}
                   for n, title in enumerate(['Герман', 'Максим', 'Александр Максимов', 'Ма'])]
        sources.write_json(self.path / 'dialogs.json', {'chats': catalog, 'updated_at': 'now'})
        with patch.object(inline_chats, 'bot_request') as api:
            commands.process_update(self.bot, self.inline_query('ма'))
            inline_ids = [item['id'] for item in api.call_args.args[2]['results']]
        commands.process_update(self.bot, self.update('/chats ма'))
        keyboard = self.api.call_args.args[2]['reply_markup']['inline_keyboard']
        button_ids = [row[0]['callback_data'].split(':')[2] for row in keyboard
                      if row[0].get('callback_data', '').startswith('chats:set:')]
        self.assertEqual(button_ids, ['3', '1', '2', '0'])
        self.assertEqual(button_ids, inline_ids)

    def test_search_matches_username_and_button_displays_it(self):
        self.catalog[1]['username'] = 'example_chat'
        sources.write_json(self.path / 'dialogs.json', {'chats': self.catalog, 'updated_at': '2026-11-17'})
        chats.open_menu(self.bot, '@example_chat')
        choices = [row[0]['text'] for row in self.api.call_args.args[2]['reply_markup']['inline_keyboard']
                   if row[0].get('callback_data', '').startswith('chats:set:')]
        self.assertEqual(choices, ['⬜ Chat 02 · @example_chat'])

    def test_non_search_questions_still_go_to_ai_after_opening_menu(self):
        chats.open_menu(self.bot)
        with patch.object(commands.queue, 'enqueue_message', return_value='queued') as question, \
                patch.object(commands, 'bot_request'):
            commands.process_update(self.bot, self.update('Объясни выжимку'))
        question.assert_called_once()

    def test_selection_is_idempotent_and_preserves_credentials_and_since(self):
        chats.open_menu(self.bot)
        callback = self.callback('chats:set:2:1:0')
        commands.process_update(self.bot, {'callback_query': callback})
        edits = sum(call.args[1] == 'editMessageText' for call in self.api.call_args_list)
        commands.process_update(self.bot, {'callback_query': callback})
        self.assertEqual(sum(call.args[1] == 'editMessageText' for call in self.api.call_args_list), edits)
        saved = sources.load_config(self.path)
        self.assertEqual([item['id'] for item in saved['sources']], [2])
        self.assertEqual(saved['api_hash'], self.config['api_hash'])
        self.assertEqual(saved['since'], self.config['since'])
        self.assertEqual((self.path / 'config.json').stat().st_mode & 0o777, 0o600)

    def test_disabling_last_chat_does_not_delete_saved_messages(self):
        sources.set_selected(self.path, 2, True)
        (self.path / 'messages.sqlite3').write_bytes(b'existing messages')
        chats.open_menu(self.bot)
        chats.handle_callback(self.bot, self.callback('chats:set:2:0:0'))
        self.assertEqual(sources.load_config(self.path)['sources'], [])
        self.assertEqual((self.path / 'messages.sqlite3').read_bytes(), b'existing messages')

    def test_search_and_page_navigation(self):
        commands.process_update(self.bot, self.update('/chats 1'))
        labels = [row[0]['text'] for row in self.api.call_args.args[2]['reply_markup']['inline_keyboard']
                  if row[0].get('callback_data', '').startswith('chats:set:')]
        self.assertIn('⬜ Chat 10', labels)
        self.assertNotIn('⬜ Chat 02', labels)
        chats.handle_callback(self.bot, self.callback('chats:page:1'))
        self.assertEqual(self.api.call_args.args[1], 'editMessageText')
        self.assertIn('Страница 2/2', self.api.call_args.args[2]['text'])
        self.assertIn('⬜ Chat 01', str(self.api.call_args.args[2]['reply_markup']))

    def test_foreign_users_and_stale_menus_cannot_change_selection(self):
        chats.open_menu(self.bot)
        self.api.reset_mock()
        commands.process_update(self.bot, self.update(sender=999))
        self.api.assert_not_called()
        for callback in (self.callback('chats:set:2:1:0', sender=999),
                         self.callback('chats:set:2:1:0', message_id=49)):
            chats.handle_callback(self.bot, callback)
        self.assertTrue(all(call.args[1] == 'answerCallbackQuery' for call in self.api.call_args_list))
        self.assertEqual(sources.load_config(self.path)['sources'], [])

    def test_unknown_chat_and_malformed_button_do_not_change_selection(self):
        chats.open_menu(self.bot)
        for data in ('chats:set:999:1:0', 'chats:set:1:2:0', 'chats:page:nope'):
            chats.handle_callback(self.bot, self.callback(data))
        self.assertEqual(sources.load_config(self.path)['sources'], [])

    def test_selected_chat_missing_from_catalog_can_be_removed(self):
        config = {**self.config, 'sources': [{'id': 999, 'title': 'Unavailable', 'channel': False}]}
        sources.save_config(self.path, config)
        chats.open_menu(self.bot, 'Unavailable')
        self.assertIn('✅ Unavailable', str(self.api.call_args.args[2]['reply_markup']))
        chats.handle_callback(self.bot, self.callback('chats:set:999:0:0'))
        self.assertEqual(sources.load_config(self.path)['sources'], [])

    def test_refresh_requests_new_catalog_without_opening_telegram_session(self):
        chats.open_menu(self.bot)
        previous = sources.refresh_token(self.path)
        chats.handle_callback(self.bot, self.callback('chats:refresh'))
        self.assertNotEqual(sources.refresh_token(self.path), previous)

    def test_missing_catalog_gives_help_and_preserves_selected_chats(self):
        (self.path / 'dialogs.json').unlink()
        chats.open_menu(self.bot)
        self.assertIn('ещё готовит список', self.api.call_args.args[2]['text'])

    def test_concurrent_selection_keeps_both_changes(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda chat_id: sources.set_selected(self.path, chat_id, True), (2, 3)))
        self.assertEqual({item['id'] for item in sources.load_config(self.path)['sources']}, {2, 3})

    def test_callback_updates_are_polled_and_checkpointed(self):
        chats.open_menu(self.bot)
        update = {'update_id': 25, 'callback_query': self.callback('chats:set:2:1:0')}
        with patch.object(commands, 'bot_request', return_value=[update]) as api:
            commands.poll_once(self.bot)
        self.assertIn('callback_query', api.call_args.args[2]['allowed_updates'])
        self.assertIn('inline_query', api.call_args.args[2]['allowed_updates'])
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT value FROM offsets WHERE bot_id = 123').fetchone()[0], 26)

    def test_edit_already_applied_is_safe_to_retry(self):
        error = HTTPError('https://api.telegram.org/bot123:secret', 400, 'Bad Request', {},
                          BytesIO(b'{"description":"Bad Request: message is not modified"}'))
        with patch.object(delivery, 'urlopen', side_effect=error):
            result = delivery.bot_request('123:secret', 'editMessageText', {'message_id': 50})
        self.assertEqual(result['message_id'], 50)

    def test_expired_callback_acknowledgment_does_not_block_update_processing(self):
        error = HTTPError('https://api.telegram.org/bot123:secret', 400, 'Bad Request', {}, BytesIO(
            b'{"description":"Bad Request: query is too old and response timeout expired or query ID is invalid"}'))
        with patch.object(delivery, 'urlopen', side_effect=error):
            self.assertFalse(delivery.bot_request('123:secret', 'answerCallbackQuery', {'callback_query_id': 'old'}))

    def test_expired_inline_query_is_checkpointed_so_polling_can_continue(self):
        error = HTTPError('https://api.telegram.org/bot123:secret', 400, 'Bad Request', {}, BytesIO(
            b'{"description":"Bad Request: query is too old and response timeout expired or query ID is invalid"}'))
        update = {**self.inline_query(), 'update_id': 42}
        with patch.object(commands, 'bot_request', return_value=[update]), \
                patch.object(delivery, 'urlopen', side_effect=error):
            commands.poll_once(self.bot)
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT value FROM offsets WHERE bot_id = 123').fetchone()[0], 43)


class CollectorSourcesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        override = patch.object(collector, 'DATA', self.path)
        override.start()
        self.addCleanup(override.stop)
        self.sources = [{'id': number, 'title': f'Chat {number}', 'channel': False, 'username': None}
                        for number in (1, 2)]
        sources.save_config(self.path, {'api_id': 123, 'api_hash': 'private-key',
                            'since': '2026-11-01T00:00:00+00:00', 'sources': [self.sources[0]]})
        sources.write_json(self.path / 'dialogs.json', {'chats': self.sources, 'updated_at': '2026-11-17'})

    async def test_running_collector_picks_up_changes_and_can_wait_with_zero_chats(self):
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
                                 is_user_authorized=AsyncMock(return_value=True))
        collected = []

        async def collect(client, db, source, since):
            collected.append(source['id'])
            if source['id'] == 1:
                sources.set_selected(self.path, 1, False)
                sources.set_selected(self.path, 2, True)
            else:
                sources.set_selected(self.path, 2, False)
            return 0

        class StopTest(Exception):
            pass

        with patch('telethon.TelegramClient', return_value=client), \
                patch.object(collector, 'collect_source', side_effect=collect), \
                patch.object(sources, 'refresh_catalog', new=AsyncMock()) as refresh, \
                patch.object(collector.asyncio, 'sleep', new=AsyncMock(side_effect=[None, None, StopTest()])), \
                patch('sys.stdout', new=StringIO()):
            with self.assertRaises(StopTest):
                await collector.run(60, False)
        self.assertEqual(collected, [1, 2])
        refresh.assert_awaited_once_with(client, self.path)
        client.connect.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        self.assertEqual(sources.load_config(self.path)['sources'], [])

    async def test_catalog_contains_dialogs_without_credentials(self):
        from telethon.tl.types import User

        async def dialogs():
            yield SimpleNamespace(name='Test', entity=User(id=5, first_name='Test'), is_channel=False)

        await sources.refresh_catalog(SimpleNamespace(iter_dialogs=dialogs), self.path)
        result = sources.catalog(self.path)
        self.assertEqual(result['chats'][0]['id'], 5)
        self.assertNotIn('private-key', json.dumps(result))
        self.assertIsNotNone(result['updated_at'])

    async def test_interrupted_catalog_refresh_keeps_previous_list(self):
        async def dialogs():
            raise ConnectionError('offline')
            yield

        with self.assertRaises(ConnectionError):
            await sources.refresh_catalog(SimpleNamespace(iter_dialogs=dialogs), self.path)
        self.assertEqual(sources.catalog(self.path)['chats'], self.sources)


if __name__ == '__main__':
    unittest.main()
