"""Проверка адресов и HTTPS-доставка с подписью Standard Webhooks."""

import base64
import hmac
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

from . import EventError

def secret_bytes(secret):
    try:
        if not isinstance(secret, str) or not secret.startswith('whsec_'):
            raise ValueError
        value = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(value) <= 64:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise EventError('Некорректный ключ подписи callback.') from None

def callback_address(url):
    """Проверяем все DNS-адреса; соединение ниже закрепляется за проверенным IP."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global
                                or ipaddress.ip_address(item[4][0]).is_multicast for item in addresses):
            raise ValueError
        return parsed, addresses[0][4][0]
    except (ValueError, OSError, TypeError):
        raise EventError('Callback должен иметь публичный HTTPS-адрес.', -32015, 'invalid_url') from None

class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise

def post_signed(url, secret, subscription_id, event_id, body):
    parsed, address = callback_address(url)
    timestamp = str(int(time.time()))
    signed = f'{event_id}.{timestamp}.'.encode() + body
    signature = base64.b64encode(hmac.digest(secret_bytes(secret), signed, 'sha256')).decode()
    connection = PinnedHTTPSConnection(parsed.hostname, address)
    try:
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        connection.request('POST', path, body, headers={
            'Content-Type': 'application/json', 'webhook-id': event_id,
            'webhook-timestamp': timestamp, 'webhook-signature': 'v1,' + signature,
            'X-MCP-Subscription-Id': subscription_id,
        })
        response = connection.getresponse()
        # No redirects; response bodies and callback URLs can contain secrets.
        return response.status, response.read(65537)
    except (OSError, http.client.HTTPException):
        raise EventError('Не удалось связаться с callback.', -32015, 'delivery_failed') from None
    finally:
        connection.close()
