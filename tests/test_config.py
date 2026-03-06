from __future__ import annotations

import json

from worker_ext_mcp.config import load_mcp_config


def test_load_mcp_config_merges_sources_and_resolves_paths(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".cursor").mkdir()
    (project_dir / ".worker").mkdir()
    tools_dir = project_dir / ".cursor" / "tools"
    tools_dir.mkdir(parents=True)

    monkeypatch.setenv("MCP_TEST_HEADER", "Bearer demo")

    (project_dir / ".cursor" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "transport": "stdio",
                        "command": "python3",
                        "cwd": "./tools",
                        "headers": {"Authorization": "${MCP_TEST_HEADER}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (project_dir / ".worker" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "args": ["server.py"],
                        "tool_prefix": "demo__",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = load_mcp_config(str(project_dir))

    assert len(loaded.sources) == 2
    demo = loaded.servers["demo"]
    assert demo.command == "python3"
    assert demo.args == ["server.py"]
    assert demo.cwd == str(tools_dir.resolve())
    assert demo.headers["Authorization"] == "Bearer demo"
    assert demo.tool_prefix == "demo__"
