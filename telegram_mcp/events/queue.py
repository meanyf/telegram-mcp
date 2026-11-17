"""Очередь запросов /summary, доставка событий и повторные попытки."""

from datetime import datetime, timezone
import json
import logging
import secrets
import time

from . import EVENT_NAME, MESSAGE_EVENT_NAME, EventError, webhooks
from .storage import database, owner_id

MAX_ATTEMPTS = 5

def enqueue_summary(update_id):
    return enqueue_request(update_id, EVENT_NAME, {})


def enqueue_message(update_id, text, message_id, reply_to=None):
    data = {'text': text, 'message_id': message_id}
    if reply_to:
        data['reply_to'] = reply_to
    return enqueue_request(update_id, MESSAGE_EVENT_NAME, data)


def enqueue_request(update_id, name, data):
    owner = owner_id()
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM requests WHERE owner = ? AND update_id = ?', (owner, update_id)).fetchone():
            return 'duplicate'
        subscribers = db.execute('SELECT id FROM subscriptions WHERE owner = ? AND name = ? AND expires > ?',
                                 (owner, name, time.time())).fetchall()
        if not subscribers:
            return 'not_subscribed'
        prefix = 'summary_' if name == EVENT_NAME else 'message_'
        event_id = prefix + secrets.token_hex(16)
        body = json.dumps({'eventId': event_id, 'name': name,
                           'timestamp': datetime.now(timezone.utc).isoformat(),
                           'data': {**data, 'request_id': event_id}, 'cursor': None})
        db.execute('INSERT INTO requests VALUES (?, ?, ?, ?)', (event_id, owner, update_id, body))
        for subscription in subscribers:
            db.execute('INSERT INTO deliveries(event_id, subscription_id) VALUES (?, ?)',
                       (event_id, subscription['id']))
    return 'queued'


def request_message_id(request_id):
    """Проверить, что ответ относится к вопросу текущего владельца бота."""
    with database() as db:
        row = db.execute('SELECT body FROM requests WHERE event_id = ? AND owner = ?',
                         (request_id, owner_id())).fetchone()
    if row:
        request = json.loads(row['body'])
        if request['name'] == MESSAGE_EVENT_NAME:
            return request['data']['message_id']
    raise ValueError('Запрос вопроса не найден для настроенного получателя.')

def deliver_pending():
    owner = owner_id()
    failures = []
    with database() as db:
        rows = db.execute('''SELECT d.*, r.body, s.url, s.secret, s.expires FROM deliveries d
            JOIN requests r ON r.event_id = d.event_id
            JOIN subscriptions s ON s.id = d.subscription_id
            WHERE d.state = 'pending' AND d.next_try <= ? AND s.owner = ? LIMIT 10''',
                          (time.time(), owner)).fetchall()
    for row in rows:
        state, status = 'pending', None
        if row['expires'] <= time.time():
            state = 'expired'
        else:
            try:
                status, _ = webhooks.post_signed(row['url'], row['secret'], row['subscription_id'],
                                        row['event_id'], row['body'].encode())
                if 200 <= status < 300:
                    state = 'delivered'
                elif status in (410, 413) or (400 <= status < 500 and status != 429):
                    state = 'failed'
            except EventError:
                pass
        attempts = row['attempts'] + 1
        if state == 'pending' and attempts >= MAX_ATTEMPTS:
            state = 'failed'
        with database() as db:
            db.execute('UPDATE deliveries SET state = ?, attempts = ?, next_try = ? '
                       "WHERE event_id = ? AND subscription_id = ? AND state = 'pending'",
                       (state, attempts, time.time() + min(5 * 2 ** attempts, 300),
                        row['event_id'], row['subscription_id']))
            if status == 410:
                db.execute('DELETE FROM subscriptions WHERE id = ?', (row['subscription_id'],))
        if state in ('failed', 'expired'):
            failures.append(row['event_id'])
            # No callback URL, signing secret or Telegram message text in logs.
            logging.getLogger(__name__).warning('MCP event %s: %s', row['event_id'], state)
    return failures
