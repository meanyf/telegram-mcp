from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from telegram_mcp import mcp_server as mcp
from telegram_mcp import storage
from telegram_mcp.collector import open_database


class InstructionsTests(unittest.TestCase):
    def test_timing_records_tool_duration_and_count_without_message_text(self):
        with patch.object(storage, 'get_messages', return_value={'messages': [{'text': 'private message'}]}), \
                self.assertLogs(mcp.logger, level='INFO') as logs:
            mcp.get_telegram_messages(123)
        text = '\n'.join(logs.output)
        self.assertIn('tool=get_telegram_messages stage=started', text)
        self.assertIn('stage=finished', text)
        self.assertIn('duration_seconds=', text)
        self.assertIn('messages=1', text)
        self.assertNotIn('private message', text)

    def test_timing_preserves_failure_without_logging_error_content(self):
        with patch.object(mcp, 'send_summary', side_effect=RuntimeError('private failure details')), \
                self.assertLogs(mcp.logger, level='INFO') as logs:
            with self.assertRaisesRegex(RuntimeError, 'private failure details'):
                mcp.send_telegram_summary('private summary')
        text = '\n'.join(logs.output)
        self.assertIn('stage=failed', text)
        self.assertNotIn('private failure details', text)
        self.assertNotIn('private summary', text)

    def test_returns_full_instructions_and_reads_changes_on_each_call(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'instructions.md'
            text = 'Правила анализа\n' * 100 + 'Конец полного промпта'
            path.write_text(text, encoding='utf-8')
            with patch.object(mcp, 'INSTRUCTIONS_PATH', path):
                self.assertEqual(mcp.get_analysis_instructions(), text)
                path.write_text('Обновлённые правила', encoding='utf-8')
                self.assertEqual(mcp.get_analysis_instructions(), 'Обновлённые правила')


class TelegramMCPTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = Path(directory.name)
        self.now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        clock = patch.object(storage, 'datetime', wraps=datetime)
        self.clock = clock.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock.stop)
        override = patch.object(storage, 'TELEGRAM_DATA', data)
        override.start()
        self.addCleanup(override.stop)
        (data / 'config.json').write_text(json.dumps({
            'api_hash': 'private-test-key',
            'sources': [{'id': 1, 'title': 'Selected'}]}))
        db = open_database(data / 'messages.sqlite3')
        with db:
            for chat_id, message_id in [(1, 10), (1, 20), (1, 30), (2, 1)]:
                db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (chat_id, message_id, 'Chat', (self.now - timedelta(hours=1)).isoformat(), None, 'Text', None, 0))
        db.close()

    def test_only_selected_sources_and_no_credentials_in_list(self):
        result = mcp.list_telegram_chats()
        self.assertEqual(result['chats'][0]['message_count'], 3)
        self.assertEqual(len(result['chats']), 1)
        self.assertNotIn('private-test-key', json.dumps(result))
        with self.assertRaises(ValueError):
            mcp.get_telegram_messages(2)

    def test_first_read_returns_whole_day_then_nothing(self):
        with closing(open_database(storage.TELEGRAM_DATA / 'messages.sqlite3')) as db, db:
            for number in range(31, 131):
                db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (1, number, 'Chat', self.now.isoformat(), None, 'New', None, 0))
        first = mcp.get_telegram_messages(1)
        self.assertEqual(len(first['messages']), 103)
        self.assertEqual(first['window_start_utc'], (self.now - timedelta(hours=24)).isoformat())
        self.assertEqual(mcp.get_telegram_messages(1)['messages'], [])

    def test_new_messages_returned_after_saved_cursor_without_repeating(self):
        mcp.get_telegram_messages(1)
        with closing(open_database(storage.TELEGRAM_DATA / 'messages.sqlite3')) as db, db:
            for number in (40, 50):
                db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (1, number, 'Chat', self.now.isoformat(), None, 'New', None, 0))
        first = mcp.get_telegram_messages(1)
        self.assertEqual([m['message_id'] for m in first['messages']], [40, 50])
        self.assertEqual(mcp.get_telegram_messages(1)['messages'], [])

    def test_24_hour_boundary_uses_actual_time_with_timezone_offsets(self):
        with closing(open_database(storage.TELEGRAM_DATA / 'messages.sqlite3')) as db, db:
            db.execute('DELETE FROM messages')
            dates = [self.now - timedelta(hours=24, seconds=1),
                     (self.now - timedelta(hours=24)).astimezone(timezone(timedelta(hours=3))),
                     self.now, self.now + timedelta(seconds=1)]
            for number, date in enumerate(dates, 1):
                db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (1, number, 'Chat', date.isoformat(), None, 'Text', None, 0))
        result = mcp.get_telegram_messages(1)
        self.assertEqual([m['message_id'] for m in result['messages']], [2, 3])

    def test_messages_older_than_day_are_skipped_after_downtime(self):
        mcp.get_telegram_messages(1)
        with closing(open_database(storage.TELEGRAM_DATA / 'messages.sqlite3')) as db, db:
            for number, age in [(40, 25), (50, 1)]:
                db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (1, number, 'Chat', (self.now - timedelta(hours=age)).isoformat(),
                            None, 'New', None, 0))
        result = mcp.get_telegram_messages(1)
        self.assertEqual([m['message_id'] for m in result['messages']], [50])

    def test_window_moves_forward_on_next_request(self):
        mcp.get_telegram_messages(1)
        with closing(open_database(storage.TELEGRAM_DATA / 'messages.sqlite3')) as db, db:
            db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (1, 40, 'Chat', (self.now + timedelta(hours=1)).isoformat(),
                        None, 'Later', None, 0))
        self.assertEqual(mcp.get_telegram_messages(1)['messages'], [])
        self.clock.now.return_value = self.now + timedelta(hours=2)
        self.assertEqual([m['message_id'] for m in mcp.get_telegram_messages(1)['messages']], [40])

    def test_concurrent_reads_do_not_repeat_delivered_messages(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: mcp.get_telegram_messages(1), range(2)))
        self.assertEqual(sorted(len(result['messages']) for result in results), [0, 3])


if __name__ == '__main__':
    unittest.main()
