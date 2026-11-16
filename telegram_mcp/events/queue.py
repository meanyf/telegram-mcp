"""Очередь запросов /summary, доставка событий и повторные попытки."""

from datetime import datetime, timezone
import json
import logging
import secrets
import time

from . import EVENT_NAME, EventError, webhooks
from .storage import database, owner_id

MAX_ATTEMPTS = 5

def enqueue_summary(update_id):
    owner = owner_id()
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM requests WHERE owner = ? AND update_id = ?', (owner, update_id)).fetchone():
            return 'duplicate'
        subscribers = db.execute('SELECT id FROM subscriptions WHERE owner = ? AND expires > ?',
                                 (owner, time.time())).fetchall()
        if not subscribers:
            return 'not_subscribed'
        event_id = 'summary_' + secrets.token_hex(16)
        body = json.dumps({'eventId': event_id, 'name': EVENT_NAME,
                           'timestamp': datetime.now(timezone.utc).isoformat(),
                           'data': {'request_id': event_id}, 'cursor': None})
        db.execute('INSERT INTO requests VALUES (?, ?, ?, ?)', (event_id, owner, update_id, body))
        for subscription in subscribers:
            db.execute('INSERT INTO deliveries(event_id, subscription_id) VALUES (?, ?)',
                       (event_id, subscription['id']))
    return 'queued'

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
