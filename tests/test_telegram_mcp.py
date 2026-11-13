from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pc_gpt_test as mcp
from telegram_collect import open_database


class TelegramMCPTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = Path(directory.name)
        override = patch.object(mcp, 'TELEGRAM_DATA', data)
        override.start()
        self.addCleanup(override.stop)
        (data / 'config.json').write_text(json.dumps({
            'api_hash': 'private-test-key',
            'sources': [{'id': 1, 'title': 'Selected'}]}))
        db = open_database(data / 'messages.sqlite3')
        with db:
            for chat_id, message_id in [(1, 10), (1, 20), (1, 30), (2, 1)]:
                db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (chat_id, message_id, 'Chat', '2026-11-13', None, 'Text', None, 0))
        db.close()

    def test_only_selected_sources_and_no_credentials_in_list(self):
        result = mcp.list_telegram_chats()
        self.assertEqual(result['chats'][0]['message_count'], 3)
        self.assertEqual(len(result['chats']), 1)
        self.assertNotIn('private-test-key', json.dumps(result))
        with self.assertRaises(ValueError):
            mcp.get_telegram_messages(2)

    def test_pagination_returns_each_message_once(self):
        page = mcp.get_telegram_messages(1, after_id=0, limit=2)
        self.assertEqual([m['message_id'] for m in page['messages']], [10, 20])
        self.assertTrue(page['has_more'])
        last = mcp.get_telegram_messages(1, after_id=page['next_after_id'], limit=2)
        self.assertEqual([m['message_id'] for m in last['messages']], [30])
        self.assertFalse(last['has_more'])
        empty = mcp.get_telegram_messages(1, after_id=last['next_after_id'])
        self.assertEqual(empty['messages'], [])
        self.assertEqual(empty['next_after_id'], 30)

    def test_invalid_limits_rejected(self):
        for limit in (0, 101):
            with self.assertRaises(ValueError):
                mcp.get_telegram_messages(1, limit=limit)
        with self.assertRaises(ValueError):
            mcp.get_telegram_messages(1, after_id=-1)

    def test_first_read_only_returns_latest_messages_then_nothing(self):
        first = mcp.get_telegram_messages(1, limit=2)
        self.assertEqual([m['message_id'] for m in first['messages']], [20, 30])
        self.assertTrue(first['older_history_skipped'])
        self.assertFalse(first['has_more'])
        self.assertEqual(mcp.get_telegram_messages(1)['messages'], [])

    def test_new_messages_returned_after_saved_cursor_without_repeating(self):
        mcp.get_telegram_messages(1)
        with closing(open_database(mcp.TELEGRAM_DATA / 'messages.sqlite3')) as db, db:
            for number in (40, 50):
                db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (1, number, 'Chat', '2026-11-13', None, 'New', None, 0))
        first = mcp.get_telegram_messages(1, limit=1)
        self.assertEqual([m['message_id'] for m in first['messages']], [40])
        self.assertTrue(first['has_more'])
        second = mcp.get_telegram_messages(1, limit=1)
        self.assertEqual([m['message_id'] for m in second['messages']], [50])
        self.assertFalse(second['has_more'])
        self.assertEqual(mcp.get_telegram_messages(1)['messages'], [])

    def test_history_view_does_not_change_automatic_cursor(self):
        mcp.get_telegram_messages(1, after_id=0, limit=1)
        self.assertEqual(len(mcp.get_telegram_messages(1)['messages']), 3)
        self.assertEqual(len(mcp.get_telegram_messages(1, after_id=0)['messages']), 3)
        self.assertEqual(mcp.get_telegram_messages(1)['messages'], [])

    def test_concurrent_reads_do_not_repeat_delivered_messages(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: mcp.get_telegram_messages(1), range(2)))
        self.assertEqual(sorted(len(result['messages']) for result in results), [0, 3])


if __name__ == '__main__':
    unittest.main()
