"""Пожелания в боте отделены от постоянных инструкций MCP."""

from pathlib import Path
from tempfile import TemporaryDirectory
import time
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import patch

from telegram_mcp import instructions, mcp_server
from telegram_mcp.bot import commands, prompt_menu
from telegram_mcp.events import storage


class PromptTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.base = self.path / 'base.md'
        self.base.write_text('Вызывай get_telegram_messages и send_telegram_summary.\n')
        self.defaults = self.path / 'defaults.md'
        self.defaults.write_text('Коротко о полезном.\n')
        self.preferences = self.path / 'local/preferences.md'
        for module, name, value in (
                (instructions, 'INSTRUCTIONS_PATH', self.base),
                (mcp_server, 'INSTRUCTIONS_PATH', self.base),
                (instructions, 'DEFAULT_PREFERENCES_PATH', self.defaults),
                (instructions, 'PREFERENCES_PATH', self.preferences),
                (prompt_menu, 'TELEGRAM_DATA', self.preferences.parent),
                (storage, 'TELEGRAM_DATA', self.preferences.parent)):
            override = patch.object(module, name, value)
            override.start()
            self.addCleanup(override.stop)
        override = patch.object(prompt_menu, 'bot_request', side_effect=lambda token, method, params=None:
                                {'username': 'our_test_bot'} if method == 'getMe' else {'message_id': 55})
        self.api = override.start()
        self.addCleanup(override.stop)
        self.config = {'token': 'test-token', 'chat_id': 456, 'bot_id': 123}

    def message(self, text, sender=456, number=80):
        return {'update_id': number, 'message': {
            'text': text, 'message_id': number, 'date': int(time.time()),
            'chat': {'id': 456, 'type': 'private'}, 'from': {'id': sender}}}

    def callback(self, action='prompt:edit', sender=456, message_id=55):
        commands.process_update(self.config, {'callback_query': {
            'id': 'callback', 'data': action, 'from': {'id': sender},
            'message': {'message_id': message_id, 'from': {'id': 123},
                        'chat': {'id': 456, 'type': 'private'}}}})

    def edit(self):
        commands.process_update(self.config, self.message('/prompt'))

    def save_button(self):
        return self.api.call_args.args[2]['reply_markup']['inline_keyboard'][0][0]['callback_data']

    def test_view_shows_preferences_without_technical_instructions(self):
        commands.process_update(self.config, self.message('/prompt'))
        content = '\n'.join(call.args[2].get('text', '') for call in self.api.call_args_list)
        self.assertIn('Коротко о полезном.', content)
        self.assertNotIn('get_telegram_messages', content)
        self.assertIn('Редактировать пожелания', str(self.api.call_args.args[2]))
        keyboard = self.api.call_args.args[2]['reply_markup']['inline_keyboard']
        self.assertIn('url', keyboard[0][0])
        self.assertNotIn('Открыть текст', str(keyboard))

    def test_cancel_from_preferences_screen_returns_to_ai_questions(self):
        self.edit()
        keyboard = self.api.call_args.args[2]['reply_markup']['inline_keyboard']
        self.callback(keyboard[1][0]['callback_data'])
        with patch.object(commands.queue, 'enqueue_message', return_value='queued') as ai, \
                patch.object(commands, 'bot_request'):
            commands.process_update(self.config, self.message('Объясни выжимку'))
        ai.assert_called_once()
        self.assertFalse(self.preferences.exists())

    def test_open_text_prefills_only_preferences_without_command_or_token(self):
        text = 'Python & SQL + стажировки #работа\nКратко 😀'
        instructions.save_preferences(text)
        self.edit()
        url = self.api.call_args.args[2]['reply_markup']['inline_keyboard'][0][0]['url']
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, 't.me')
        self.assertEqual(parsed.path, '/our_test_bot')
        self.assertEqual(parse_qs(parsed.query)['text'][0], text)
        self.assertNotIn('/save_prompt', url)

    def test_changes_require_confirmation_and_mcp_always_keeps_technical_rules(self):
        self.edit()
        with patch.object(commands.queue, 'enqueue_message') as ai:
            commands.process_update(self.config, self.message('Только вакансии Python. Подробно.'))
        ai.assert_not_called()
        self.assertFalse(self.preferences.exists())
        self.assertEqual(instructions.read_preferences(), 'Коротко о полезном.\n')
        self.callback(self.save_button())
        combined = mcp_server.get_analysis_instructions()
        self.assertIn(self.base.read_text().strip(), combined)
        self.assertIn('Только вакансии Python. Подробно.', combined)
        self.assertEqual(self.base.read_text(), 'Вызывай get_telegram_messages и send_telegram_summary.\n')
        self.assertEqual(self.preferences.stat().st_mode & 0o777, 0o600)

    def test_cancel_button_keeps_current_preferences(self):
        self.edit()
        commands.process_update(self.config, self.message('Новый вариант'))
        cancel_button = self.api.call_args.args[2]['reply_markup']['inline_keyboard'][0][1]['callback_data']
        self.callback(cancel_button)
        self.assertFalse(self.preferences.exists())
        self.assertEqual(prompt_menu.session(self.config)['phase'], 'closed')

    def test_cancel_command_exits_editor_and_questions_work_again(self):
        self.edit()
        commands.process_update(self.config, self.message('/cancel'))
        with patch.object(commands.queue, 'enqueue_message', return_value='queued') as ai, \
                patch.object(commands, 'bot_request'):
            commands.process_update(self.config, self.message('Вопрос к ИИ'))
        ai.assert_called_once()
        self.assertFalse(self.preferences.exists())

    def test_new_candidate_invalidates_previous_confirmation(self):
        self.edit()
        commands.process_update(self.config, self.message('Первый вариант', number=80))
        old_button = self.save_button()
        commands.process_update(self.config, self.message('Второй вариант', number=81))
        new_button = self.save_button()
        self.callback(old_button)
        self.assertFalse(self.preferences.exists())
        self.callback(new_button)
        self.assertEqual(instructions.read_preferences(), 'Второй вариант\n')

    def test_repeated_save_does_not_overwrite_later_changes(self):
        self.edit()
        commands.process_update(self.config, self.message('Вариант'))
        button = self.save_button()
        self.callback(button)
        instructions.save_preferences('Позже изменённый вариант')
        self.callback(button)
        self.assertEqual(instructions.read_preferences(), 'Позже изменённый вариант\n')

    def test_foreign_user_cannot_show_edit_or_save_preferences(self):
        commands.process_update(self.config, self.message('/prompt', sender=999))
        self.api.assert_not_called()
        self.callback(sender=999)
        self.assertEqual(self.api.call_args.args[1], 'answerCallbackQuery')
        self.assertFalse(prompt_menu.session(self.config))
        self.edit()
        commands.process_update(self.config, self.message('Чужие пожелания', sender=999))
        self.assertEqual(prompt_menu.session(self.config)['phase'], 'input')
        commands.process_update(self.config, self.message('Мои пожелания'))
        self.callback(self.save_button(), sender=999)
        self.assertFalse(self.preferences.exists())

    def test_normal_command_during_edit_is_not_saved_as_preferences(self):
        self.edit()
        with patch.object(commands.queue, 'enqueue_summary', return_value='queued') as summary, \
                patch.object(commands, 'bot_request'):
            commands.process_update(self.config, self.message('/summary'))
        summary.assert_called_once()
        self.assertFalse(self.preferences.exists())

    def test_legacy_draft_never_replaces_preferences_or_reaches_ai(self):
        with patch.object(commands.queue, 'enqueue_message') as ai:
            commands.process_update(self.config, self.message('/save_prompt oldtoken\nТехнический промпт'))
        ai.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_legacy_full_prompt_is_preserved_but_not_used_as_preferences(self):
        legacy = self.preferences.parent / 'analysis-instructions.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('Старый полный промпт')
        self.assertEqual(instructions.read_preferences(), 'Коротко о полезном.\n')
        self.assertNotIn('Старый полный промпт', instructions.read())
        self.assertEqual(legacy.read_text(), 'Старый полный промпт')

    def test_invalid_preferences_preserve_previous_version(self):
        instructions.save_preferences('Сохранённый текст')
        for text in ('   ', '😀' * 2001):
            with self.assertRaises(ValueError):
                instructions.save_preferences(text)
        self.assertEqual(instructions.read_preferences(), 'Сохранённый текст\n')

    def test_saved_preferences_override_defaults_but_keep_updated_base_rules(self):
        instructions.save_preferences('Интересны стажировки')
        self.defaults.write_text('Другие пожелания по умолчанию')
        self.base.write_text('Обновлённые постоянные инструкции')
        combined = mcp_server.get_analysis_instructions()
        self.assertIn('Обновлённые постоянные инструкции', combined)
        self.assertIn('Интересны стажировки', combined)
        self.assertNotIn('Другие пожелания по умолчанию', combined)
        url = prompt_menu.draft_url('our_test_bot', 'x' * instructions.MAX_LENGTH + '\n')
        self.assertEqual(len(parse_qs(urlparse(url).query)['text'][0]), instructions.MAX_LENGTH)

    def test_failed_atomic_replace_preserves_previous_version(self):
        instructions.save_preferences('Сохранённый текст')
        with patch.object(instructions.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                instructions.save_preferences('Новый текст')
        self.assertEqual(instructions.read_preferences(), 'Сохранённый текст\n')
        self.assertEqual(list(self.preferences.parent.iterdir()), [self.preferences])

    def test_session_survives_reload_and_is_bound_to_bot(self):
        self.edit()
        commands.process_update(self.config, self.message('Новые пожелания'))
        button = self.save_button()
        self.assertEqual(prompt_menu.session(self.config)['phase'], 'confirm')
        self.assertFalse(prompt_menu.session({**self.config, 'bot_id': 999}))
        self.callback(button)
        self.assertEqual(instructions.read_preferences(), 'Новые пожелания\n')
