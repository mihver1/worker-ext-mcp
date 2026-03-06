from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from worker_core.extensions import ExtensionContext

from worker_ext_mcp.extension import McpExtension


def _server_script() -> str:
    return str(Path(__file__).parent / "fixtures" / "dummy_mcp_server.py")


def _write_config(project_dir: Path, config: dict) -> None:
    worker_dir = project_dir / ".worker"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "mcp.json").write_text(json.dumps(config), encoding="utf-8")


async def _load_extension(project_dir: Path) -> McpExtension:
    ext = McpExtension()
    ext.bind_context(ExtensionContext(project_dir=str(project_dir), runtime="local"))
    await ext.on_load()
    return ext


async def _wait_for_port(port: int, *, host: str = "127.0.0.1", timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for {host}:{port}") from exc
            await asyncio.sleep(0.1)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_extension_loads_stdio_mcp_server(tmp_path):
    _write_config(
        tmp_path,
        {
            "mcpServers": {
                "dummy": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [_server_script(), "stdio"],
                    "tool_prefix": "dummy__",
                }
            }
        },
    )

    ext = await _load_extension(tmp_path)
    try:
        tools = {tool.name: tool for tool in ext.get_tools()}
        assert "dummy__echo" in tools
        assert "dummy__prompt_get" in tools
        assert "dummy__resource_read" in tools

        schema = tools["dummy__echo"].definition().input_schema
        assert schema["properties"]["payload"]["properties"]["text"]["type"] == "string"

        echo_result = await tools["dummy__echo"].execute(
            payload={"text": "hello", "tags": ["x"]},
            repeat=2,
        )
        assert "hello:x | hello:x" in echo_result

        prompt_result = await tools["dummy__prompt_get"].execute(
            prompt="welcome",
            arguments={"name": "Maks"},
        )
        assert "Welcome Maks" in prompt_result

        resource_result = await tools["dummy__resource_read"].execute(uri="memo://greeting")
        assert "hello from resource" in resource_result
    finally:
        await ext.on_unload()


@pytest.mark.asyncio
async def test_extension_loads_streamable_http_mcp_server(tmp_path):
    port = _free_tcp_port()
    env = dict(os.environ)
    env["DUMMY_MCP_PORT"] = str(port)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        _server_script(),
        "streamable_http",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await _wait_for_port(port)
        _write_config(
            tmp_path,
            {
                "mcpServers": {
                    "remote": {
                        "transport": "streamable_http",
                        "url": f"http://127.0.0.1:{port}/mcp",
                        "tool_prefix": "remote__",
                    }
                }
            },
        )

        ext = await _load_extension(tmp_path)
        try:
            tools = {tool.name: tool for tool in ext.get_tools()}
            assert "remote__echo" in tools
            result = await tools["remote__echo"].execute(
                payload={"text": "http", "tags": ["ok"]},
                repeat=1,
            )
            assert "http:ok" in result
        finally:
            await ext.on_unload()
    finally:
        proc.terminate()
        await proc.wait()
