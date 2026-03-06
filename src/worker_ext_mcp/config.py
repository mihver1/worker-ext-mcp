"""Configuration loading for the Worker MCP extension."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class McpAuthConfig(BaseModel):
    """Remote authentication options for MCP servers."""

    type: Literal["none", "bearer", "basic"] = "none"
    token: str = ""
    token_env: str = ""
    username: str = ""
    username_env: str = ""
    password: str = ""
    password_env: str = ""


class McpServerConfig(BaseModel):
    """Merged per-server configuration."""

    transport: Literal["stdio", "streamable_http", "sse"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    encoding: str = "utf-8"
    encoding_error_handler: Literal["strict", "ignore", "replace"] = "strict"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = 30.0
    sse_read_timeout: float = 300.0
    enabled: bool = True
    tool_prefix: str = ""
    include_tools: bool = True
    include_prompts: bool = True
    include_resources: bool = True
    roots: list[str] = Field(default_factory=list)
    auth: McpAuthConfig = Field(default_factory=McpAuthConfig)

    @model_validator(mode="after")
    def validate_transport_requirements(self) -> McpServerConfig:
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport in {"streamable_http", "sse"} and not self.url:
            raise ValueError(f"{self.transport} transport requires 'url'")
        return self


@dataclass(slots=True)
class LoadedMcpConfig:
    """Resolved extension configuration and the files it came from."""

    servers: dict[str, McpServerConfig]
    sources: list[Path]


def load_mcp_config(project_dir: str) -> LoadedMcpConfig:
    """Load MCP config from common client locations with Worker override precedence."""
    project_path = Path(project_dir or os.getcwd()).resolve()
    candidates = [
        Path.home() / ".config" / "worker" / "mcp.json",
        project_path / ".cursor" / "mcp.json",
        project_path / ".vscode" / "mcp.json",
        project_path / ".mcp.json",
        project_path / ".worker" / "mcp.json",
    ]

    merged: dict[str, dict[str, Any]] = {}
    sources: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        raw_data = json.loads(path.read_text(encoding="utf-8"))
        raw_servers = raw_data.get("mcpServers") or raw_data.get("servers") or {}
        if not isinstance(raw_servers, dict):
            continue
        for server_name, server_value in raw_servers.items():
            if not isinstance(server_value, dict):
                continue
            resolved = _resolve_server_dict(server_value, base_dir=path.parent)
            current = merged.setdefault(server_name, {})
            _deep_merge(current, resolved)
        sources.append(path)

    servers = {
        name: McpServerConfig.model_validate(server_config)
        for name, server_config in merged.items()
    }
    return LoadedMcpConfig(servers=servers, sources=sources)


def _resolve_server_dict(raw_server: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    resolved = _expand_value(raw_server)
    if not isinstance(resolved, dict):
        return {}

    transport = resolved.get("transport")
    if isinstance(transport, str):
        normalized_transport = transport.replace("-", "_").strip().lower()
        if normalized_transport in {"stdio", "streamable_http", "sse"}:
            resolved["transport"] = normalized_transport

    command = resolved.get("command")
    if isinstance(command, str):
        resolved["command"] = os.path.expanduser(command)

    cwd = resolved.get("cwd")
    if isinstance(cwd, str) and cwd:
        resolved["cwd"] = str(_resolve_path(base_dir, cwd))

    url = resolved.get("url")
    if isinstance(url, str):
        resolved["url"] = url.strip()

    roots = resolved.get("roots")
    if isinstance(roots, list):
        resolved["roots"] = [
            str(_resolve_path(base_dir, item))
            for item in roots
            if isinstance(item, str)
        ]

    return resolved


def _expand_value(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_string(value)
    if isinstance(value, list):
        return [_expand_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_value(item) for key, item in value.items()}
    return value


def _expand_string(value: str) -> str:
    expanded = os.path.expanduser(value)
    return _ENV_PATTERN.sub(lambda match: os.environ.get(match.group(1), ""), expanded)


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
