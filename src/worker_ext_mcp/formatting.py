"""Formatting helpers for MCP responses rendered back through Worker tools."""

from __future__ import annotations

import json
from typing import Any

from mcp import types


def format_call_tool_result(result: types.CallToolResult) -> str:
    """Render an MCP tool result into a readable text blob."""
    parts = [_format_content_item(item) for item in result.content]
    if result.structuredContent:
        parts.append(_json_block(result.structuredContent))
    rendered = "\n\n".join(part for part in parts if part).strip() or "(no content)"
    if result.isError:
        return f"Error:\n{rendered}"
    return rendered


def format_prompt_result(result: types.GetPromptResult) -> str:
    """Render a prompt template result."""
    parts: list[str] = []
    if result.description:
        parts.append(result.description)
    for message in result.messages:
        parts.append(f"{message.role}: {_format_content_item(message.content)}")
    return "\n\n".join(part for part in parts if part).strip() or "(empty prompt)"


def format_read_resource_result(result: types.ReadResourceResult) -> str:
    """Render resource contents."""
    rendered: list[str] = []
    for item in result.contents:
        if isinstance(item, types.TextResourceContents):
            header = f"{item.uri}"
            if item.mimeType:
                header += f" ({item.mimeType})"
            rendered.append(f"{header}\n{item.text}")
        elif isinstance(item, types.BlobResourceContents):
            mime = item.mimeType or "application/octet-stream"
            rendered.append(f"{item.uri} ({mime})\n[blob: {len(item.blob)} base64 chars]")
    return "\n\n".join(rendered).strip() or "(empty resource)"


def format_tools_listing(tools: list[types.Tool]) -> str:
    if not tools:
        return "No MCP tools available."
    lines = []
    for tool in tools:
        description = tool.description or tool.title or ""
        lines.append(f"- {tool.name}: {description}".rstrip(": "))
    return "\n".join(lines)


def format_prompts_listing(prompts: list[types.Prompt]) -> str:
    if not prompts:
        return "No MCP prompts available."
    lines = []
    for prompt in prompts:
        args = ", ".join(arg.name for arg in prompt.arguments or [])
        suffix = f" ({args})" if args else ""
        description = prompt.description or prompt.title or ""
        lines.append(f"- {prompt.name}{suffix}: {description}".rstrip(": "))
    return "\n".join(lines)


def format_resources_listing(
    resources: list[types.Resource],
    resource_templates: list[types.ResourceTemplate],
) -> str:
    parts: list[str] = []
    if resources:
        parts.append("Resources:")
        parts.extend(
            f"- {resource.name}: {resource.uri}"
            for resource in resources
        )
    if resource_templates:
        if parts:
            parts.append("")
        parts.append("Resource templates:")
        parts.extend(
            f"- {template.name}: {template.uriTemplate}"
            for template in resource_templates
        )
    return "\n".join(parts).strip() or "No MCP resources available."


def _format_content_item(item: Any) -> str:
    if isinstance(item, types.TextContent):
        return item.text
    if isinstance(item, types.ImageContent):
        return f"[image: {item.mimeType}, {len(item.data)} base64 chars]"
    if isinstance(item, types.AudioContent):
        return f"[audio: {item.mimeType}, {len(item.data)} base64 chars]"
    if isinstance(item, types.ResourceLink):
        return f"[resource] {item.name}: {item.uri}"
    if isinstance(item, types.EmbeddedResource):
        resource = item.resource
        if isinstance(resource, types.TextResourceContents):
            return f"[embedded resource] {resource.uri}\n{resource.text}"
        if isinstance(resource, types.BlobResourceContents):
            mime = resource.mimeType or "application/octet-stream"
            return f"[embedded resource] {resource.uri} ({mime}, {len(resource.blob)} base64 chars)"
    if hasattr(item, "model_dump"):
        return _json_block(item.model_dump(mode="json"))
    return str(item)


def _json_block(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
