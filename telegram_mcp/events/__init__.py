"""События MCP: подписки, очередь и доставка уведомлений."""

EVENT_NAME = 'summary.requested'


class EventError(Exception):
    def __init__(self, message, code=-32602, reason=None):
        super().__init__(message)
        self.code = code
        self.reason = reason

