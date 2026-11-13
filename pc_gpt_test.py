"""Тестовый MCP-сервер: возвращает только фразу, время и случайный код."""

import secrets
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP


server = FastMCP(
    "My PC Test",
    host="127.0.0.1",
    port=8765,
    stateless_http=True,
    json_response=True,
)


@server.tool(annotations={"readOnlyHint": True})
def hello_from_my_pc() -> dict:
    """Получить тестовое сообщение с моего компьютера."""
    return {
        "message": "Привет с моего компьютера!",
        "proof": secrets.token_hex(8),
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    server.run(transport="streamable-http")
