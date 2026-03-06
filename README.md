# worker-ext-mcp

`worker-ext-mcp` is a Worker extension that connects configured MCP servers and exposes their tools, prompts, and resources inside Worker.

## What it does

- Discovers MCP server definitions from common config files.
- Connects to local and remote MCP servers.
- Registers MCP tools as regular Worker tools.
- Adds helper tools for prompts and resources.
- Provides `/mcp` and `/mcp reload` commands in Worker.

## Supported transports

- `stdio`
- `streamable_http`
- `sse`

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for local development
- A working Worker installation
- At least one MCP server configuration

## Installation

Install the extension into the same Python environment as Worker.

### Install from a local checkout

```bash
worker ext install /absolute/path/to/worker-ext-mcp
```

### Development setup

```bash
uv sync --dev
```

This installs the package together with test and lint dependencies.

## How Worker discovers the extension

The package registers the `mcp` extension through the `worker.extensions` entry point. Once the package is installed in Worker’s environment, Worker loads it automatically on startup.

## Configuration

The extension reads MCP server definitions from these locations, in this order:

1. `~/.config/worker/mcp.json`
2. `.cursor/mcp.json`
3. `.vscode/mcp.json`
4. `.mcp.json`
5. `.worker/mcp.json`

Later files override earlier ones, and server objects are merged by name.

### Example config

Create `.worker/mcp.json` in your project:

```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "python3",
      "args": ["scripts/mcp_filesystem_server.py"],
      "cwd": ".",
      "tool_prefix": "fs__"
    },
    "docs": {
      "transport": "streamable_http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": {
        "X-Client": "worker"
      }
    }
  }
}
```

### Supported server fields

#### Common fields

- `transport`: `stdio`, `streamable_http`, or `sse`
- `enabled`: enable or disable a server
- `tool_prefix`: custom prefix for generated Worker tool names
- `include_tools`: include MCP tools
- `include_prompts`: include prompt helper tools
- `include_resources`: include resource helper tools
- `roots`: extra root directories exposed to the MCP server

#### `stdio` fields

- `command`
- `args`
- `env`
- `cwd`
- `encoding`
- `encoding_error_handler`

#### Remote transport fields

- `url`
- `headers`
- `timeout`
- `sse_read_timeout`
- `auth`

### Authentication

Remote servers support:

- `none`
- `bearer`
- `basic`

Example bearer auth:

```json
{
  "mcpServers": {
    "secured": {
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "auth": {
        "type": "bearer",
        "token_env": "MCP_API_TOKEN"
      }
    }
  }
}
```

### Variable and path resolution

- `~` is expanded in string values.
- `${ENV_VAR}` placeholders are replaced with environment variables.
- Relative `cwd` and `roots` paths are resolved relative to the config file that defines them.

## How to use it

1. Install the extension into Worker’s environment.
2. Add one or more MCP servers to one of the supported `mcp.json` files.
3. Start Worker in your project.
4. Use the generated tools from your Worker session.

### Generated tool names

For every MCP server, the extension generates Worker tools.

If `tool_prefix` is not set, the default prefix is:

```text
mcp__<server_name>__
```

So an MCP tool named `echo` from a server named `demo` becomes:

```text
mcp__demo__echo
```

If you set:

```json
{
  "mcpServers": {
    "demo": {
      "transport": "stdio",
      "command": "python3",
      "args": ["server.py"],
      "tool_prefix": "demo__"
    }
  }
}
```

Then the same tool becomes:

```text
demo__echo
```

### Prompt and resource helper tools

When enabled, each server also gets helper tools:

- `<prefix>prompt_list`
- `<prefix>prompt_get`
- `<prefix>resource_list`
- `<prefix>resource_read`

These let Worker:

- list available MCP prompts
- resolve a prompt by name with arguments
- list resources and resource templates
- read a resource by URI

## Worker commands

The extension registers one Worker command:

- `/mcp` — show config sources, connected servers, and connection errors
- `/mcp reload` — reload configuration and reconnect all MCP servers

## Example workflow

1. Add a server to `.worker/mcp.json`.
2. Start Worker.
3. Run `/mcp` to confirm the server is connected.
4. Ask Worker to use one of the generated MCP tools.
5. Update the config and run `/mcp reload` if needed.

## Development

### Run tests

```bash
uv run pytest
```

### Run lint

```bash
uv run ruff check .
```

## Project structure

```text
src/worker_ext_mcp/
  config.py
  extension.py
  formatting.py
  runtime.py
tests/
  fixtures/
  test_config.py
  test_integration.py
```

## Notes

- Connection failures are collected and shown in `/mcp`.
- Disabled servers are skipped.
- If a server does not support prompts or resources, the related helper tools return empty listings.
