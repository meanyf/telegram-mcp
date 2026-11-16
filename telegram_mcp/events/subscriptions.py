"""Список событий, создание, продление и отмена подписок ChatGPT."""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import time

from . import EVENT_NAME, EventError, webhooks
from .storage import database, owner_id

def event_catalog():
    return {'events': [{
        'name': EVENT_NAME,
        'description': 'Владелец настроенного Telegram-бота запросил сводку командой /summary.',
        'delivery': ['webhook'],
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        'payloadSchema': {
            'type': 'object', 'properties': {'request_id': {'type': 'string'}},
            'required': ['request_id'], 'additionalProperties': False,
        },
    }]}

def handle_event_method(method, params):
    if method == 'events/list':
        return event_catalog()
    if method not in ('events/subscribe', 'events/unsubscribe'):
        raise EventError('Method not found', -32601)
    if not isinstance(params, dict):
        raise EventError('Некорректные параметры события.')
    delivery = params.get('delivery', {})
    if (params.get('name') != EVENT_NAME or params.get('arguments', {}) != {}
            or not isinstance(delivery, dict) or not isinstance(delivery.get('url'), str)):
        raise EventError('Неизвестное событие или фильтры.')
    owner = owner_id()
    url = delivery['url']
    identity = hashlib.sha256(json.dumps([owner, url, EVENT_NAME, {}]).encode()).hexdigest()
    if method == 'events/unsubscribe':
        with database() as db:
            db.execute('DELETE FROM subscriptions WHERE id = ? AND owner = ?', (identity, owner))
            db.execute("UPDATE deliveries SET state = 'cancelled' WHERE subscription_id = ? AND state = 'pending'",
                       (identity,))
        return {}
    if delivery.get('mode') != 'webhook':
        raise EventError('Поддерживается только доставка webhook.')
    secret = delivery.get('secret')
    webhooks.secret_bytes(secret)
    ttl = params.get('ttlMs', 86400000)
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
        raise EventError('ttlMs должен быть положительным числом или null.')
    expires = time.time() + min((ttl or 86400000) / 1000, 86400)
    with database() as db:
        # One active ChatGPT task prevents concurrent analysis consuming the same cursors.
        other = db.execute('SELECT id FROM subscriptions WHERE owner = ? AND expires > ? AND id != ?',
                           (owner, time.time(), identity)).fetchone()
        if other:
            raise EventError('Уже есть подписка. Отключите прежнее задание в ChatGPT перед созданием нового.')
    challenge = secrets.token_urlsafe(24)
    body = json.dumps({'type': 'verification', 'challenge': challenge}).encode()
    status, response = webhooks.post_signed(url, secret, identity, 'verify_' + secrets.token_hex(16), body)
    try:
        echoed = json.loads(response).get('challenge', '')
        if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
            raise ValueError
    except (ValueError, AttributeError):
        raise EventError('Callback не подтвердил проверочный код.', -32015, 'challenge_failed') from None
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        # Recheck after verification: concurrent registration must not create two tasks.
        if db.execute('SELECT id FROM subscriptions WHERE owner = ? AND expires > ? AND id != ?',
                      (owner, time.time(), identity)).fetchone():
            raise EventError('Подписка уже создана другим запросом.')
        db.execute('INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?) '
                   'ON CONFLICT(id) DO UPDATE SET secret=excluded.secret, expires=excluded.expires',
                   (identity, owner, url, secret, expires))
    return {'id': identity, 'refreshBefore': datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            'cursor': None, 'truncated': False}
