"""Совместимость MCP Events с SDK 1.x: расширение того же HTTP endpoint."""

import json
import secrets

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import EventError, subscriptions


class EventsMiddleware:
    def __init__(self, app, private_path):
        self.app = app
        self.private_path = private_path

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope['path'].rstrip('/')
        private = secrets.compare_digest(path.encode(), self.private_path.encode())
        if path.startswith('/mcp/') and not private:
            return await JSONResponse({'error': 'Not found'}, status_code=404)(scope, receive, send)
        if path != '/mcp' and not private:
            return await self.app(scope, receive, send)
        scope = dict(scope)
        scope['path'] = '/mcp'
        scope['raw_path'] = b'/mcp'
        if scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                return await JSONResponse({'error': 'Request too large'}, status_code=413)(scope, receive, send)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            payload = None
        method = payload.get('method', '') if isinstance(payload, dict) else ''
        if isinstance(method, str) and method.startswith('events/'):
            if not private:
                return await JSONResponse({'error': 'Use the private MCP URL'}, status_code=403)(scope, receive, send)
            request_id = payload.get('id')
            if request_id is None or payload.get('jsonrpc') != '2.0':
                result = {'error': {'code': -32600, 'message': 'Invalid request'}}
            else:
                try:
                    result = {'result': await anyio.to_thread.run_sync(
                        subscriptions.handle_event_method, method, payload.get('params', {}))}
                except EventError as error:
                    result = {'error': {'code': error.code, 'message': str(error)}}
                    if error.reason:
                        result['error']['data'] = {'reason': error.reason}
            return await JSONResponse({'jsonrpc': '2.0', 'id': request_id, **result})(scope, receive, send)

        received = False

        async def replay():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        if private and method == 'initialize':
            messages = []

            async def capture(message):
                messages.append(message)

            await self.app(scope, replay, capture)
            start = next((message for message in messages if message['type'] == 'http.response.start'), None)
            if start and start['status'] == 200:
                raw = b''.join(message.get('body', b'') for message in messages
                               if message['type'] == 'http.response.body')
                try:
                    initialized = json.loads(raw)
                    initialized['result']['capabilities']['events'] = {}
                    return await JSONResponse(initialized)(scope, receive, send)
                except (ValueError, KeyError, TypeError):
                    pass
            for message in messages:
                await send(message)
            return
        await self.app(scope, replay, send)
