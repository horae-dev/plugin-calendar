# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it: with
only the declared secret in its environment, and no network in the test (the URL's host is
not allowed, so nothing is fetched)."""

import sys

import anyio
from horae_sdk.testing import Replayed, read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from conftest import TOKEN
from horae_calendar import COMMAND, NAME

# Derived from the plugin's code, so renaming the plugin needs no edit here.
TOOL = f"{NAME}.events"
CALENDARS = [{"name": "Personal", "index": 1}]


async def serve_and_call() -> tuple[set[str], Replayed, Replayed]:
    # `COMMAND` without its interpreter: run the module with the test's own Python.
    server = StdioServerParameters(
        command=sys.executable,
        args=COMMAND[1:],
        env={"CALENDAR_ICS_URLS": f"https://ics.example.org/{TOKEN}/basic.ics\n"},
    )
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        refused = read_result(await client.call_tool(TOOL, {"calendars": CALENDARS}))
        bad = read_result(await client.call_tool(TOOL, {"calendars": CALENDARS, "days": 99}))
    return tools, refused, bad


def test_serves_over_stdio() -> None:
    tools, refused, bad = anyio.run(serve_and_call)
    assert tools == {TOOL}
    assert refused.error == "calendar failed: Personal: host_not_allowed"
    assert bad.error == "days must be a whole number from 1 to 62"
