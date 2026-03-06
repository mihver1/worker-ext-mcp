"""Dummy MCP server used by integration tests."""

from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel


class EchoPayload(BaseModel):
    text: str
    tags: list[str] = []


mcp = FastMCP(
    "Dummy MCP",
    host="127.0.0.1",
    port=int(os.environ.get("DUMMY_MCP_PORT", "8001")),
    streamable_http_path="/mcp",
)


@mcp.tool()
async def echo(payload: EchoPayload, repeat: int = 1) -> str:
    return " | ".join(f"{payload.text}:{','.join(payload.tags)}" for _ in range(repeat))


@mcp.prompt()
def welcome(name: str) -> str:
    return f"Welcome {name}"


@mcp.resource("memo://greeting", name="greeting")
def greeting() -> str:
    return "hello from resource"


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "streamable_http":
        transport = "streamable-http"
    mcp.run(transport=transport)
