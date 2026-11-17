import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import socket
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from telegram_mcp.bot import commands
from telegram_mcp.bot import delivery
from telegram_mcp.events import EVENT_NAME, MESSAGE_EVENT_NAME, EventError, queue, storage, subscriptions, webhooks
from telegram_mcp import mcp_server


class EventsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        override = patch.object(storage, 'TELEGRAM_DATA', self.path)
        override.start()
        self.addCleanup(override.stop)
        self.config = {'token': '123:secret', 'bot_id': 123, 'chat_id': 456}
        (self.path / 'bot.json').write_text(json.dumps(self.config))
        self.params = {'name': EVENT_NAME, 'arguments': {}, 'delivery': {
            'mode': 'webhook', 'url': 'https://callback.example/subscription',
            'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()}}

    @staticmethod
    def echo_challenge(url, secret, subscription, event_id, body):
        challenge = json.loads(body)['challenge']
        return 200, json.dumps({'challenge': challenge}).encode()

    def subscribe(self, name=EVENT_NAME):
        with patch.object(webhooks, 'post_signed', side_effect=self.echo_challenge):
            return subscriptions.handle_event_method('events/subscribe', {**self.params, 'name': name})

    def update(self, number=1, **message_fields):
        return {'update_id': number, 'message': {
            'chat': {'id': 456, 'type': 'private'}, 'from': {'id': 456},
            'text': '/summary', 'date': int(time.time()), 'message_id': 100 + number, **message_fields}}

    def test_subscription_verified_persistent_and_idempotent(self):
        first = self.subscribe()
        self.params['delivery']['secret'] = 'whsec_' + base64.b64encode(b'y' * 32).decode()
        second = self.subscribe()
        self.assertEqual(first['id'], second['id'])
        with storage.database() as db:
            rows = db.execute('SELECT * FROM subscriptions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['secret'], self.params['delivery']['secret'])
        self.assertEqual((self.path / 'events.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_failed_callback_verification_does_not_save_subscription(self):
        with patch.object(webhooks, 'post_signed', return_value=(200, b'{"challenge":"wrong"}')):
            with self.assertRaises(EventError):
                subscriptions.handle_event_method('events/subscribe', self.params)
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 0)

    def test_rejects_invalid_secret_unknown_event_and_filters(self):
        for change in ({'name': 'unknown'}, {'arguments': {'chat': 99}}, {'ttlMs': -1}):
            with self.subTest(change=change), self.assertRaises(EventError):
                subscriptions.handle_event_method('events/subscribe', {**self.params, **change})
        with self.assertRaises(EventError):
            webhooks.secret_bytes('whsec_dGlueQ==')

    def test_only_one_active_subscription_allowed(self):
        self.subscribe()
        self.params['delivery']['url'] = 'https://callback.example/other'
        with self.assertRaises(EventError):
            self.subscribe()

    def test_unsubscribe_and_expiration_prevent_new_requests(self):
        self.subscribe()
        subscriptions.handle_event_method('events/unsubscribe', self.params)
        self.assertEqual(queue.enqueue_summary(1), 'not_subscribed')
        self.subscribe()
        with storage.database() as db:
            db.execute('UPDATE subscriptions SET expires = 0')
        self.assertEqual(queue.enqueue_summary(2), 'not_subscribed')

    def test_only_owner_can_request_summary(self):
        self.subscribe()
        invalid = [self.update(chat={'id': 999, 'type': 'private'}),
                   self.update(sender={'id': 999}),
                   self.update(chat={'id': 456, 'type': 'group'}),
                   self.update(date=int(time.time()) - 600), self.update(text='/unknown')]
        invalid[1]['message']['from'] = {'id': 999}
        with patch.object(commands, 'bot_request') as api:
            for update in invalid:
                commands.process_update(self.config, update)
            api.assert_not_called()
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

    def test_summary_repeated_update_queues_only_one_event(self):
        self.subscribe()
        with patch.object(commands, 'bot_request', return_value={'message_id': 1}) as api:
            commands.process_update(self.config, self.update())
            commands.process_update(self.config, self.update())
            self.assertEqual(api.call_count, 1)
        with storage.database() as db:
            body = db.execute('SELECT body FROM requests').fetchone()[0]
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 1)
        self.assertEqual(json.loads(body)['name'], 'summary.requested')
        self.assertNotIn('secret', body)
        self.assertNotIn('456', body)

    def test_command_without_subscription_explains_setup_and_does_not_queue(self):
        with patch.object(commands, 'bot_request') as api:
            commands.process_update(self.config, self.update())
        self.assertIn('Нет активной подписки', api.call_args.args[2]['text'])
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

    def test_delivery_retry_preserves_id_and_body_then_stops_after_success(self):
        self.subscribe()
        queue.enqueue_summary(42)
        with patch.object(webhooks, 'post_signed', return_value=(503, b'')) as first:
            queue.deliver_pending()
        with storage.database() as db:
            row = db.execute('SELECT * FROM deliveries').fetchone()
            self.assertEqual(row['attempts'], 1)
            self.assertGreater(row['next_try'], time.time())
            db.execute('UPDATE deliveries SET next_try = 0')
        with patch.object(webhooks, 'post_signed', return_value=(200, b'')) as second:
            queue.deliver_pending()
            queue.deliver_pending()
            self.assertEqual(second.call_count, 1)
        self.assertEqual(first.call_args.args[3:], second.call_args.args[3:])

    def test_permanent_failure_and_unsubscribe_stop_delivery(self):
        self.subscribe()
        queue.enqueue_summary(1)
        with patch.object(webhooks, 'post_signed', return_value=(410, b'')) as api:
            self.assertEqual(len(queue.deliver_pending()), 1)
            queue.deliver_pending()
            self.assertEqual(api.call_count, 1)
        self.assertEqual(queue.enqueue_summary(2), 'not_subscribed')
        self.subscribe()
        queue.enqueue_summary(3)
        subscriptions.handle_event_method('events/unsubscribe', self.params)
        with patch.object(webhooks, 'post_signed') as api:
            queue.deliver_pending()
            api.assert_not_called()

    def test_retry_limit_bounds_failed_delivery(self):
        self.subscribe()
        queue.enqueue_summary(1)
        with patch.object(webhooks, 'post_signed', side_effect=EventError('offline')) as api:
            for _ in range(10):
                with storage.database() as db:
                    db.execute('UPDATE deliveries SET next_try = 0')
                queue.deliver_pending()
            self.assertEqual(api.call_count, queue.MAX_ATTEMPTS)
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT state FROM deliveries').fetchone()[0], 'failed')

    def test_changed_bot_recipient_cannot_receive_old_events(self):
        self.subscribe()
        queue.enqueue_summary(1)
        self.config['chat_id'] = 999
        (self.path / 'bot.json').write_text(json.dumps(self.config))
        with patch.object(webhooks, 'post_signed') as api:
            queue.deliver_pending()
            api.assert_not_called()
        self.assertEqual(queue.enqueue_summary(2), 'not_subscribed')

    def test_polling_offset_saved_and_used_after_restart(self):
        self.subscribe()
        with patch.object(commands, 'bot_request', side_effect=[[self.update(42)], {'message_id': 1}, []]) as api:
            commands.poll_once(self.config)
            commands.poll_once(self.config)
        self.assertEqual(api.call_args.args[2]['offset'], 43)

    def test_callback_rejects_private_mixed_and_multicast_addresses(self):
        for url in ('http://example.com', 'https://user:pass@example.com', 'https://example.com:8443'):
            with self.assertRaises(EventError):
                webhooks.callback_address(url)
        for addresses in (['127.0.0.1'], ['93.184.216.34', '10.0.0.1'], ['224.0.0.1'], ['::1']):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443)) for address in addresses]
            with patch.object(webhooks.socket, 'getaddrinfo', return_value=records):
                with self.assertRaises(EventError):
                    webhooks.callback_address('https://callback.example')

    def test_webhook_signature_matches_standard_webhooks(self):
        connection = Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{}'
        with patch.object(webhooks, 'callback_address', return_value=(
                webhooks.urlsplit('https://callback.example/path?q=1'), '93.184.216.34')), \
                patch.object(webhooks, 'PinnedHTTPSConnection', return_value=connection) as factory, \
                patch.object(webhooks.time, 'time', return_value=12345):
            webhooks.post_signed(self.params['delivery']['url'], self.params['delivery']['secret'], 'sub', 'event', b'{}')
        signature = base64.b64encode(hmac.digest(b'x' * 32, b'event.12345.{}', 'sha256')).decode()
        self.assertEqual(connection.request.call_args.kwargs['headers']['webhook-signature'], 'v1,' + signature)
        factory.assert_called_once_with('callback.example', '93.184.216.34')
        connection.close.assert_called_once()

    def test_endpoint_secret_stays_stable(self):
        path = storage.endpoint_path()
        self.assertEqual(storage.endpoint_path(), path)
        self.assertGreater(len(path), 32)
        self.assertEqual((self.path / 'events-key').stat().st_mode & 0o777, 0o600)

    def test_old_summary_subscription_survives_schema_upgrade(self):
        path = self.path / 'events.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE subscriptions (id TEXT PRIMARY KEY, owner TEXT NOT NULL, '
                       'url TEXT NOT NULL, secret TEXT NOT NULL, expires REAL NOT NULL)')
            db.execute('INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?)',
                       ('original', '123:456', self.params['delivery']['url'],
                        self.params['delivery']['secret'], time.time() + 3600))
        with storage.database() as db:
            subscription = dict(db.execute('SELECT * FROM subscriptions').fetchone())
        self.assertEqual(subscription['name'], EVENT_NAME)
        self.assertEqual(subscription['id'], 'original')
        self.assertEqual(subscription['secret'], self.params['delivery']['secret'])
        self.assertEqual(queue.enqueue_summary(1), 'queued')
        self.assertEqual(queue.enqueue_message(2, 'Вопрос', 102), 'not_subscribed')

    def test_events_route_to_their_own_subscription(self):
        summary = self.subscribe()
        question = self.subscribe(MESSAGE_EVENT_NAME)
        self.assertNotEqual(summary['id'], question['id'])
        queue.enqueue_summary(1)
        queue.enqueue_message(2, 'Что выбрать?', 102)
        with storage.database() as db:
            rows = db.execute('SELECT d.subscription_id, r.body FROM deliveries d '
                              'JOIN requests r USING(event_id)').fetchall()
        routed = {json.loads(row['body'])['name']: row['subscription_id'] for row in rows}
        self.assertEqual(routed, {EVENT_NAME: summary['id'], MESSAGE_EVENT_NAME: question['id']})
        subscriptions.handle_event_method('events/unsubscribe', {**self.params, 'name': MESSAGE_EVENT_NAME})
        self.assertEqual(queue.enqueue_message(3, 'Ещё вопрос', 103), 'not_subscribed')
        self.assertEqual(queue.enqueue_summary(4), 'queued')

    def test_question_and_bot_quote_are_saved_once_without_reading_new_messages(self):
        self.subscribe(MESSAGE_EVENT_NAME)
        update = self.update(text='Где можно без опыта?', reply_to_message={
            'message_id': 50, 'text': 'Нашёл три вакансии.', 'from': {'id': 123}})
        with patch.object(commands, 'bot_request') as api, \
                patch.object(mcp_server.storage, 'get_messages') as get_messages:
            commands.process_update(self.config, update)
            commands.process_update(self.config, update)
            self.assertEqual(api.call_count, 1)
            get_messages.assert_not_called()
        with storage.database() as db:
            requests = db.execute('SELECT body FROM requests').fetchall()
        self.assertEqual(len(requests), 1)
        request = json.loads(requests[0]['body'])
        self.assertEqual(request['name'], MESSAGE_EVENT_NAME)
        self.assertEqual(request['data']['text'], 'Где можно без опыта?')
        self.assertEqual(request['data']['reply_to'], {'message_id': 50, 'text': 'Нашёл три вакансии.'})
        self.assertEqual(queue.request_message_id(request['eventId']), 101)

    def test_foreign_users_media_and_unknown_commands_do_not_create_questions(self):
        self.subscribe(MESSAGE_EVENT_NAME)
        updates = [self.update(text='Вопрос', **{'from': {'id': 999}}),
                   self.update(text='Вопрос', chat={'id': 999, 'type': 'private'}),
                   self.update(text='Вопрос', chat={'id': 456, 'type': 'group'}),
                   self.update(text='Вопрос', date=int(time.time()) - 600),
                   self.update(text='', photo=[{'file_id': 'photo'}]),
                   self.update(text='/unknown')]
        with patch.object(commands, 'bot_request') as api:
            for update in updates:
                commands.process_update(self.config, update)
            api.assert_not_called()
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

    def test_question_without_subscription_explains_which_event_is_missing(self):
        self.subscribe()
        with patch.object(commands, 'bot_request') as api:
            commands.process_update(self.config, self.update(text='Привет'))
        self.assertIn(MESSAGE_EVENT_NAME, api.call_args.args[2]['text'])
        with storage.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

    def test_reply_only_accepts_question_of_current_owner(self):
        self.subscribe()
        self.subscribe(MESSAGE_EVENT_NAME)
        queue.enqueue_summary(1)
        queue.enqueue_message(2, 'Вопрос', 102)
        with storage.database() as db:
            rows = db.execute('SELECT event_id, body FROM requests').fetchall()
        ids = {json.loads(row['body'])['name']: row['event_id'] for row in rows}
        self.assertEqual(queue.request_message_id(ids[MESSAGE_EVENT_NAME]), 102)
        for request_id in ('missing', ids[EVENT_NAME]):
            with self.assertRaises(ValueError):
                queue.request_message_id(request_id)
        (self.path / 'bot.json').write_text(json.dumps({**self.config, 'chat_id': 999}))
        with self.assertRaises(ValueError):
            queue.request_message_id(ids[MESSAGE_EVENT_NAME])

    def test_asgi_initialize_events_and_regular_tool_calls(self):
        async def direct(function, *args, **kwargs):
            return function(*args)

        async def worker(*args):
            return None

        async def check():
            with patch('telegram_mcp.mcp_server.asyncio.to_thread', side_effect=worker), \
                    patch('telegram_mcp.events.http.anyio.to_thread.run_sync', side_effect=direct):
                app = mcp_server.create_app()
                path = storage.endpoint_path()
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url='http://localhost:8765') as client:
                        headers = {'Accept': 'application/json, text/event-stream'}

                        async def rpc(method, params=None, url=path):
                            return await client.post(url, headers=headers, json={
                                'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})

                        init = {'protocolVersion': '2025-03-26', 'capabilities': {},
                                'clientInfo': {'name': 'test', 'version': '1'}}
                        response = await rpc('initialize', init)
                        self.assertIn('events', response.json()['result']['capabilities'])
                        catalog = await rpc('events/list')
                        self.assertEqual(catalog.json()['result']['events'][0]['name'], EVENT_NAME)
                        self.assertEqual((await rpc('events/list', url='/mcp')).status_code, 403)
                        self.assertEqual((await rpc('events/list', url='/mcp/wrong')).status_code, 404)
                        with patch.object(webhooks, 'post_signed', side_effect=self.echo_challenge):
                            subscribed = await rpc('events/subscribe', self.params)
                        self.assertIn('id', subscribed.json()['result'])
                        with patch.object(commands, 'bot_request', return_value={'message_id': 1}):
                            commands.process_update(self.config, self.update(100))
                        with patch.object(webhooks, 'post_signed', return_value=(200, b'')) as sent:
                            queue.deliver_pending()
                        self.assertEqual(json.loads(sent.call_args.args[-1])['name'], EVENT_NAME)
                        cancelled = await rpc('events/unsubscribe', self.params)
                        self.assertEqual(cancelled.json()['result'], {})
                        self.assertEqual(queue.enqueue_summary(101), 'not_subscribed')
                        with patch.object(webhooks, 'post_signed', side_effect=self.echo_challenge):
                            question_sub = await rpc('events/subscribe', {**self.params, 'name': MESSAGE_EVENT_NAME})
                        self.assertIn('id', question_sub.json()['result'])
                        with patch.object(commands, 'bot_request'):
                            commands.process_update(self.config, self.update(102, text='Объясни выжимку'))
                        with patch.object(webhooks, 'post_signed', return_value=(200, b'')) as sent:
                            queue.deliver_pending()
                        request = json.loads(sent.call_args.args[-1])
                        self.assertEqual(request['data']['text'], 'Объясни выжимку')
                        with patch.object(delivery, 'TELEGRAM_DATA', self.path), \
                                patch.object(delivery, 'bot_request', return_value={'message_id': 999}) as telegram:
                            reply = await rpc('tools/call', {'name': 'send_telegram_reply', 'arguments': {
                                'request_id': request['eventId'], 'text': 'Вот пояснение.'}})
                        self.assertFalse(reply.json()['result'].get('isError', False))
                        self.assertEqual(telegram.call_args.args[2]['chat_id'], 456)
                        self.assertEqual(telegram.call_args.args[2]['reply_parameters']['message_id'], 202)
                        tools = await rpc('tools/list')
                        names = [tool['name'] for tool in tools.json()['result']['tools']]
                        self.assertIn('get_analysis_instructions', names)
                        self.assertIn('send_telegram_summary', names)
                        hello = await rpc('tools/call', {'name': 'hello_from_my_pc', 'arguments': {}})
                        self.assertEqual(hello.status_code, 200)
                        public = await rpc('initialize', init, url='/mcp')
                        self.assertNotIn('events', public.json()['result']['capabilities'])
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
