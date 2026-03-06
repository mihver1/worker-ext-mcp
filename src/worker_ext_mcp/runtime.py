"""Async MCP runtime manager for Worker."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from worker_ai.models import (
    Message,
    ReasoningDelta,
    Role,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolDef,
    ToolResult,
)
from worker_ai.tool_schema import normalize_json_schema
from worker_core.execution import get_current_tool_execution_context
from worker_core.extensions import ExtensionContext
from worker_core.tools import Tool

from worker_ext_mcp.config import LoadedMcpConfig, McpServerConfig, load_mcp_config
from worker_ext_mcp.formatting import (
    format_call_tool_result,
    format_prompt_result,
    format_prompts_listing,
    format_read_resource_result,
    format_resources_listing,
)

logger = logging.getLogger("worker.ext.mcp")


@dataclass(slots=True)
class McpServerRuntime:
    """Connected MCP server and its discovered catalog."""

    name: str
    config: McpServerConfig
    exit_stack: AsyncExitStack
    session: ClientSession
    source_label: str
    endpoint_label: str
    tools: list[types.Tool] = field(default_factory=list)
    prompts: list[types.Prompt] = field(default_factory=list)
    resources: list[types.Resource] = field(default_factory=list)
    resource_templates: list[types.ResourceTemplate] = field(default_factory=list)


class McpCallableTool(Tool):
    """Simple callable-backed Worker tool that preserves raw JSON Schema."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        self.name = name
        self.description = description
        self._input_schema = input_schema
        self._handler = handler

    async def execute(self, **kwargs: Any) -> str:
        result = self._handler(**kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return str(result)

    def definition(self) -> ToolDef:
        return ToolDef(
            name=self.name,
            description=self.description,
            parameters=[],
            input_schema=self._input_schema,
        )


class McpRuntimeManager:
    """Connection and catalog manager for configured MCP servers."""

    def __init__(self) -> None:
        self.context: ExtensionContext | None = None
        self.config: LoadedMcpConfig = LoadedMcpConfig(servers={}, sources=[])
        self.servers: dict[str, McpServerRuntime] = {}
        self.tools: list[Tool] = []
        self.errors: dict[str, str] = {}

    async def load(self, context: ExtensionContext) -> None:
        """Load configuration and connect all enabled MCP servers."""
        self.context = context
        self.errors = {}
        self.config = load_mcp_config(context.project_dir or os.getcwd())
        self.servers = {}
        self.tools = []
        for server_name, server_config in self.config.servers.items():
            if not server_config.enabled:
                continue
            try:
                runtime = await self._connect_server(server_name, server_config)
            except Exception as exc:  # noqa: BLE001
                self.errors[server_name] = str(exc)
                logger.exception("Failed to connect MCP server %s", server_name)
                continue
            self.servers[server_name] = runtime
        self._rebuild_tools()

    async def reload(self) -> None:
        """Close existing sessions and load everything again."""
        if self.context is None:
            raise RuntimeError("MCP runtime has no extension context")
        await self.close()
        await self.load(self.context)

    async def close(self) -> None:
        """Close all active MCP sessions."""
        for runtime in self.servers.values():
            with suppress(Exception):
                await runtime.exit_stack.aclose()
        self.servers = {}
        self.tools = []

    def status_text(self) -> str:
        """Render a short status summary."""
        lines: list[str] = []
        if self.config.sources:
            lines.append("Config sources:")
            lines.extend(f"- {path}" for path in self.config.sources)
        else:
            lines.append("No MCP config found.")

        if self.servers:
            if lines:
                lines.append("")
            lines.append("Connected servers:")
            for runtime in self.servers.values():
                lines.append(
                    f"- {runtime.name} [{runtime.config.transport}] "
                    f"tools={len(runtime.tools)} prompts={len(runtime.prompts)} "
                    f"resources={len(runtime.resources)} "
                    f"templates={len(runtime.resource_templates)}"
                )

        if self.errors:
            if lines:
                lines.append("")
            lines.append("Connection errors:")
            for name, message in self.errors.items():
                lines.append(f"- {name}: {message}")

        return "\n".join(lines).strip()

    async def _connect_server(
        self,
        server_name: str,
        server_config: McpServerConfig,
    ) -> McpServerRuntime:
        stack = AsyncExitStack()
        read_stream: Any
        write_stream: Any
        endpoint_label = server_config.transport

        headers, auth = self._resolve_remote_auth(server_config)
        if server_config.transport == "stdio":
            env = dict(os.environ)
            env.update(server_config.env)
            params = StdioServerParameters(
                command=server_config.command,
                args=server_config.args,
                env=env,
                cwd=server_config.cwd,
                encoding=server_config.encoding,
                encoding_error_handler=server_config.encoding_error_handler,
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params)
            )
            source_label = server_config.command
        elif server_config.transport == "streamable_http":
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=headers or None,
                    timeout=httpx.Timeout(
                        server_config.timeout,
                        read=server_config.sse_read_timeout,
                    ),
                    auth=auth,
                )
            )
            read_stream, write_stream, get_endpoint = await stack.enter_async_context(
                streamable_http_client(
                    server_config.url,
                    http_client=http_client,
                )
            )
            source_label = server_config.url
            endpoint_label = get_endpoint() or server_config.url
        else:
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(
                    server_config.url,
                    headers=headers or None,
                    timeout=server_config.timeout,
                    sse_read_timeout=server_config.sse_read_timeout,
                    auth=auth,
                )
            )
            source_label = server_config.url
            endpoint_label = server_config.url

        session = await stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                sampling_callback=self._sampling_callback,
                elicitation_callback=self._elicitation_callback,
                list_roots_callback=self._list_roots_callback,
            )
        )
        await session.initialize()

        runtime = McpServerRuntime(
            name=server_name,
            config=server_config,
            exit_stack=stack,
            session=session,
            source_label=source_label,
            endpoint_label=endpoint_label,
        )
        await self._refresh_catalog(runtime)
        return runtime

    async def _refresh_catalog(self, runtime: McpServerRuntime) -> None:
        runtime.tools = (
            await self._safe_collect_paginated(runtime.session.list_tools, "tools")
            if runtime.config.include_tools
            else []
        )
        runtime.prompts = (
            await self._safe_collect_paginated(runtime.session.list_prompts, "prompts")
            if runtime.config.include_prompts
            else []
        )
        runtime.resources = (
            await self._safe_collect_paginated(runtime.session.list_resources, "resources")
            if runtime.config.include_resources
            else []
        )
        runtime.resource_templates = (
            await self._safe_collect_paginated(
                runtime.session.list_resource_templates,
                "resourceTemplates",
            )
            if runtime.config.include_resources
            else []
        )

    async def _collect_paginated(self, fetch: Callable[..., Any], attribute: str) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        while True:
            result = await fetch(cursor=cursor)
            items.extend(getattr(result, attribute, []))
            cursor = getattr(result, "nextCursor", None)
            if not cursor:
                break
        return items

    async def _safe_collect_paginated(self, fetch: Callable[..., Any], attribute: str) -> list[Any]:
        try:
            return await self._collect_paginated(fetch, attribute)
        except Exception:
            return []

    def _rebuild_tools(self) -> None:
        tools: list[Tool] = []
        for runtime in self.servers.values():
            prefix = self._tool_prefix(runtime)
            if runtime.config.include_tools:
                for tool in runtime.tools:
                    tool_name = f"{prefix}{_sanitize_name(tool.name)}"
                    description = tool.description or tool.title or f"MCP tool {tool.name}"
                    input_schema = normalize_json_schema(
                        tool.inputSchema or {"type": "object", "properties": {}}
                    )

                    async def _handler(
                        _runtime: McpServerRuntime = runtime,
                        _tool_name: str = tool.name,
                        **kwargs: Any,
                    ) -> str:
                        result = await _runtime.session.call_tool(_tool_name, kwargs or None)
                        return format_call_tool_result(result)

                    tools.append(
                        McpCallableTool(
                            name=tool_name,
                            description=f"[{runtime.name}] {description}",
                            input_schema=input_schema,
                            handler=_handler,
                        )
                    )

            if runtime.config.include_prompts:
                tools.append(
                    McpCallableTool(
                        name=f"{prefix}prompt_list",
                        description=f"[{runtime.name}] List available MCP prompts",
                        input_schema={"type": "object", "properties": {}},
                        handler=lambda _runtime=runtime: format_prompts_listing(_runtime.prompts),
                    )
                )
                tools.append(
                    McpCallableTool(
                        name=f"{prefix}prompt_get",
                        description=f"[{runtime.name}] Resolve an MCP prompt by name",
                        input_schema=_prompt_schema(runtime.prompts),
                        handler=self._make_get_prompt_handler(runtime),
                    )
                )

            if runtime.config.include_resources:
                tools.append(
                    McpCallableTool(
                        name=f"{prefix}resource_list",
                        description=f"[{runtime.name}] List MCP resources and templates",
                        input_schema={"type": "object", "properties": {}},
                        handler=lambda _runtime=runtime: format_resources_listing(
                            _runtime.resources,
                            _runtime.resource_templates,
                        ),
                    )
                )
                tools.append(
                    McpCallableTool(
                        name=f"{prefix}resource_read",
                        description=f"[{runtime.name}] Read an MCP resource URI",
                        input_schema=_resource_schema(
                            runtime.resources,
                            runtime.resource_templates,
                        ),
                        handler=self._make_read_resource_handler(runtime),
                    )
                )

        self.tools = tools

    def _make_get_prompt_handler(self, runtime: McpServerRuntime) -> Callable[..., Any]:
        async def _handler(prompt: str, arguments: dict[str, str] | None = None) -> str:
            result = await runtime.session.get_prompt(prompt, arguments or None)
            return format_prompt_result(result)

        return _handler

    def _make_read_resource_handler(self, runtime: McpServerRuntime) -> Callable[..., Any]:
        async def _handler(uri: str) -> str:
            result = await runtime.session.read_resource(uri)
            return format_read_resource_result(result)

        return _handler

    async def _sampling_callback(
        self,
        _request_context: Any,
        params: types.CreateMessageRequestParams,
    ) -> types.CreateMessageResult:
        execution_context = get_current_tool_execution_context()
        if execution_context is None:
            return types.CreateMessageResult(
                role="assistant",
                content=types.TextContent(
                    type="text",
                    text="Sampling unavailable: no active Worker session.",
                ),
                model="worker-mcp",
                stopReason="endTurn",
            )

        session = execution_context.session
        messages = _sampling_messages_to_worker(params)
        text_chunks: list[str] = []
        tool_use_requested = False

        async for event in session.provider.stream_chat(
            session.model,
            messages,
            tools=_mcp_tools_to_worker_tool_defs(params.tools),
            temperature=(
                params.temperature
                if params.temperature is not None
                else session.temperature
            ),
            max_tokens=params.maxTokens,
            thinking_level=session.thinking_level,
        ):
            if isinstance(event, TextDelta):
                text_chunks.append(event.content)
            elif isinstance(event, ToolCallDelta):
                tool_use_requested = True
            elif isinstance(event, ReasoningDelta):
                continue

        text = "".join(text_chunks).strip()
        if not text and tool_use_requested:
            text = "Model requested tool use during sampling."
        if not text:
            text = "(empty sampling response)"

        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=text),
            model=session.model,
            stopReason="toolUse" if tool_use_requested else "endTurn",
        )

    async def _list_roots_callback(self, _request_context: Any) -> types.ListRootsResult:
        roots: list[types.Root] = []
        project_dir = self.context.project_dir if self.context is not None else os.getcwd()
        all_roots = [project_dir]
        for runtime in self.servers.values():
            all_roots.extend(runtime.config.roots)
        seen: set[str] = set()
        for root in all_roots:
            resolved = str(Path(root).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            roots.append(types.Root(uri=Path(resolved).as_uri(), name=Path(resolved).name))
        return types.ListRootsResult(roots=roots)

    async def _elicitation_callback(
        self,
        _request_context: Any,
        _params: Any,
    ) -> types.ElicitResult:
        logger.warning(
            "MCP elicitation requested but interactive elicitation is not implemented."
        )
        return types.ElicitResult(action="cancel")

    def _resolve_remote_auth(
        self,
        server_config: McpServerConfig,
    ) -> tuple[dict[str, str], httpx.Auth | None]:
        headers = dict(server_config.headers)
        auth = server_config.auth
        if auth.type == "bearer":
            token = auth.token or os.environ.get(auth.token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return headers, None
        if auth.type == "basic":
            username = auth.username or os.environ.get(auth.username_env, "")
            password = auth.password or os.environ.get(auth.password_env, "")
            return headers, httpx.BasicAuth(username, password)
        return headers, None

    def _tool_prefix(self, runtime: McpServerRuntime) -> str:
        configured = runtime.config.tool_prefix.strip()
        if configured:
            return configured
        return f"mcp__{_sanitize_name(runtime.name)}__"


def _prompt_schema(prompts: list[types.Prompt]) -> dict[str, Any]:
    names = [prompt.name for prompt in prompts]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "MCP prompt name",
            },
            "arguments": {
                "type": "object",
                "description": "Prompt arguments as string key/value pairs",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["prompt"],
    }
    if names:
        schema["properties"]["prompt"]["enum"] = names
    return schema


def _resource_schema(
    resources: list[types.Resource],
    resource_templates: list[types.ResourceTemplate],
) -> dict[str, Any]:
    uris = [str(resource.uri) for resource in resources]
    template_descriptions = [template.uriTemplate for template in resource_templates]
    description = "MCP resource URI to read"
    if template_descriptions:
        description += f"; templates: {', '.join(template_descriptions)}"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "description": description,
            }
        },
        "required": ["uri"],
    }
    if uris:
        schema["properties"]["uri"]["enum"] = uris
    return schema


def _sanitize_name(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    return sanitized or "server"


def _mcp_tools_to_worker_tool_defs(tools: list[types.Tool] | None) -> list[ToolDef] | None:
    if not tools:
        return None
    return [
        ToolDef(
            name=tool.name,
            description=tool.description or tool.title or tool.name,
            parameters=[],
            input_schema=normalize_json_schema(
                tool.inputSchema or {"type": "object", "properties": {}}
            ),
        )
        for tool in tools
    ]


def _sampling_messages_to_worker(params: types.CreateMessageRequestParams) -> list[Message]:
    messages: list[Message] = []
    if params.systemPrompt:
        messages.append(Message(role=Role.SYSTEM, content=params.systemPrompt))
    for message in params.messages:
        messages.extend(_sampling_message_to_worker(message))
    return messages


def _sampling_message_to_worker(message: types.SamplingMessage) -> list[Message]:
    blocks = message.content if isinstance(message.content, list) else [message.content]
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    tool_messages: list[Message] = []

    for block in blocks:
        block_type = getattr(block, "type", "")
        if isinstance(block, types.TextContent):
            text_parts.append(block.text)
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            )
        elif block_type == "tool_result":
            tool_messages.append(
                Message(
                    role=Role.TOOL,
                    tool_result=ToolResult(
                        tool_call_id=block.toolUseId,
                        content=_format_tool_result_content(block),
                        is_error=bool(block.isError),
                    ),
                )
            )
        else:
            text_parts.append(str(block))

    converted: list[Message] = []
    text = "\n".join(part for part in text_parts if part).strip()
    if message.role == "assistant":
        converted.append(
            Message(
                role=Role.ASSISTANT,
                content=text,
                tool_calls=tool_calls or None,
            )
        )
    else:
        if text:
            converted.append(Message(role=Role.USER, content=text))
        converted.extend(tool_messages)
        if not text and not tool_messages:
            converted.append(Message(role=Role.USER, content=""))
    return converted


def _format_tool_result_content(block: Any) -> str:
    rendered: list[str] = []
    for item in getattr(block, "content", []) or []:
        if isinstance(item, types.TextContent):
            rendered.append(item.text)
        else:
            rendered.append(str(item))
    if getattr(block, "structuredContent", None):
        rendered.append(json.dumps(block.structuredContent, ensure_ascii=False, indent=2))
    return "\n".join(rendered).strip() or "(empty tool result)"
