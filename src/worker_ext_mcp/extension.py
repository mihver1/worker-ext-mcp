"""Worker MCP extension entrypoint."""

from __future__ import annotations

import logging
import os
from typing import Any

from worker_core.extensions import Extension, ExtensionContext

from worker_ext_mcp.runtime import McpRuntimeManager

logger = logging.getLogger("worker.ext.mcp")


class McpExtension(Extension):
    """Expose configured MCP servers as first-class Worker tools."""

    name = "mcp"
    version = "0.1.0"

    def __init__(self) -> None:
        self._runtime = McpRuntimeManager()

    async def on_load(self) -> None:
        context = self.context or ExtensionContext(
            project_dir=os.getcwd(),
            runtime="local",
        )
        await self._runtime.load(context)
        if self._runtime.servers:
            logger.info("Loaded %d MCP server(s)", len(self._runtime.servers))

    async def on_unload(self) -> None:
        await self._runtime.close()

    def get_tools(self) -> list[Any]:
        return self._runtime.tools

    def get_commands(self) -> dict[str, Any]:
        return {
            "mcp": self._cmd_mcp,
        }

    async def _cmd_mcp(self, arg: str) -> str | None:
        action = arg.strip().lower()
        if action == "reload":
            await self._runtime.reload()
        return self._runtime.status_text()
