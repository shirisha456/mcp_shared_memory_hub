"""The tool manifest as a committed contract.

Tool names, descriptions and schemas are the *entire* interface a model sees.
They are not documentation: the description is the prompt that decides whether
the model stores a credential, whether it passes ``supersedes`` instead of
recording a contradicting fact, and whether it treats a conflict as recoverable.

Change one and system behaviour changes with no logic change and no other test
failing. So the manifest is snapshotted to ``manifest.json`` and committed. Any
drift fails here and shows up in review as a diff, which is where a change to
the model's instructions belongs.

Regenerate deliberately after an intentional change:

    MEMHUB_UPDATE_MANIFEST=1 pytest tests/protocol/test_manifest_snapshot.py

Then read the diff before committing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import AsyncEngine

from memhub.mcp.server import build_server
from memhub.persistence.engine import create_session_factory

pytestmark = pytest.mark.integration

SNAPSHOT = Path(__file__).parent / "manifest.json"


@pytest.fixture
def server(engine: AsyncEngine) -> MCPServer:
    return build_server(create_session_factory(engine), name="memhub")


async def capture_manifest(server: MCPServer) -> dict[str, Any]:
    """Everything a client can observe about the interface before calling it."""
    async with Client(server) as client:
        tools = await client.list_tools()
        resources = await client.list_resource_templates()
        static_resources = await client.list_resources()
        instructions = client.instructions

    return {
        "instructions": instructions,
        "tools": [
            {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
                "input_properties": sorted((tool.input_schema or {}).get("properties", {})),
                "required": sorted((tool.input_schema or {}).get("required", [])),
                "has_output_schema": bool(tool.output_schema),
            }
            for tool in sorted(tools.tools, key=lambda t: t.name)
        ],
        "resource_templates": sorted(
            str(template.uri_template) for template in resources.resource_templates
        ),
        "resources": sorted(str(resource.uri) for resource in static_resources.resources),
    }


async def test_manifest_matches_the_committed_snapshot(server: MCPServer) -> None:
    current = await capture_manifest(server)

    if os.environ.get("MEMHUB_UPDATE_MANIFEST") == "1":
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"snapshot rewritten at {SNAPSHOT} - review the diff before committing")

    if not SNAPSHOT.is_file():
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.fail(f"no snapshot existed; wrote one at {SNAPSHOT}. Review and commit it.")

    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == expected, (
        "The MCP interface changed.\n\n"
        "Tool names, descriptions and schemas are the prompt that steers the "
        "model - a wording change alters behaviour without altering logic. If "
        "this change is intended, regenerate the snapshot and read the diff:\n\n"
        "    MEMHUB_UPDATE_MANIFEST=1 pytest tests/protocol/test_manifest_snapshot.py\n"
    )


async def test_no_tool_is_missing_a_description(server: MCPServer) -> None:
    """An undescribed tool is one the model will use wrongly or not at all."""
    manifest = await capture_manifest(server)
    for tool in manifest["tools"]:
        assert tool["description"], f"{tool['name']} has no description"
        assert tool["title"], f"{tool['name']} has no title"
        assert len(tool["description"]) > 120, (
            f"{tool['name']} has a {len(tool['description'])}-character description. "
            "This is the model's only instruction for when and how to use it; a "
            "one-liner is not enough to steer behaviour."
        )


async def test_every_write_tool_names_its_project_argument(server: MCPServer) -> None:
    """Isolation is a boundary, so no tool may operate without a project scope.

    ``project_use`` is the sole exception: resolving the project is the thing it
    does.
    """
    manifest = await capture_manifest(server)
    for tool in manifest["tools"]:
        if tool["name"] == "project_use":
            continue
        assert "project_id" in tool["input_properties"], (
            f"{tool['name']} does not take a project_id - every operation must be "
            "scoped to a project"
        )
        assert "project_id" in tool["required"]
