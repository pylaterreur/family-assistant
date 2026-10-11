"""MCP tool calls that never get an answer are abandoned after the configured timeout."""

from __future__ import annotations

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.message import SessionMessage

from family_assistant.tools import MCPToolsProvider


@pytest.mark.asyncio
async def test_a_call_the_server_never_answers_times_out_without_a_retry() -> None:
    to_server_send, to_server_receive = anyio.create_memory_object_stream[
        SessionMessage
    ](10)
    to_client_send, to_client_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](10)
    provider = MCPToolsProvider(
        {"server": {"command": "unused"}}, tool_call_timeout_seconds=0.2
    )

    async with (
        to_server_send,
        to_server_receive,
        to_client_send,
        to_client_receive,
        ClientSession(to_client_receive, to_server_send) as session,
    ):
        result = await provider._call_tool_with_reconnect(
            "slow_tool", {}, "server", session
        )

        requests = []
        while True:
            try:
                requests.append(to_server_receive.receive_nowait())
            except anyio.WouldBlock:
                break

    assert "timed out after 0.2 seconds" in result
    assert "may have completed" in result
    assert len(requests) == 1
