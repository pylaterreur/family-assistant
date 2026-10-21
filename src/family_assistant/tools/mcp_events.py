"""Discovery of the events MCP servers offer, for building event automations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from family_assistant.events.mcp_client import (
    MCPEventsError,
    MCPEventsNotSupportedError,
)
from family_assistant.events.mcp_source import MCPEventSource
from family_assistant.storage.events import EventSourceType
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

MCP_EVENTS_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "list_mcp_events",
            "description": (
                "List the event types connected MCP servers (for example the Tuit "
                "task tracker) offer to automations, as seen with the current "
                "user's credential: each event's name, description, the arguments "
                "a subscription takes (JSON Schema) and the payload it delivers. "
                "Use it before creating an automation with event_source 'mcp'. "
                "Such an automation's event_filter names the subscription with "
                "'server_id', 'event' and one 'arguments.<name>' key per "
                "argument; any other key filters the delivered events, e.g. "
                "'data.category'. Only events offered for polling can be used."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "Only this MCP server. Omit to list every server.",
                    },
                },
                "required": [],
            },
        },
    },
]


async def list_mcp_events_tool(
    exec_context: ToolExecutionContext, server_id: str | None = None
) -> ToolResult:
    """Describe the event types each reachable MCP server offers."""
    source = (exec_context.event_sources or {}).get(EventSourceType.mcp.value)
    if not isinstance(source, MCPEventSource):
        message = "MCP event automations are not enabled on this deployment"
        return ToolResult(text=f"Error: {message}", data={"error": message})

    user_id = exec_context.user_id
    server_ids = [server_id] if server_id else source.event_servers(user_id)
    servers: list[dict[str, object]] = []
    for candidate in server_ids:
        try:
            event_types = await source.list_event_types(candidate, user_id)
        except MCPEventsNotSupportedError as exc:
            if server_id:
                return ToolResult(text=f"Error: {exc}", data={"error": str(exc)})
            logger.debug("MCP server %s offers no events: %s", candidate, exc)
            continue
        except (MCPEventsError, ValueError) as exc:
            if server_id:
                return ToolResult(text=f"Error: {exc}", data={"error": str(exc)})
            servers.append({"server_id": candidate, "error": str(exc)})
            continue
        servers.append({
            "server_id": candidate,
            "events": [
                {
                    "name": event_type.name,
                    "description": event_type.description,
                    "pollable": event_type.supports_poll,
                    "arguments_schema": event_type.input_schema,
                    "payload_schema": event_type.payload_schema,
                }
                for event_type in event_types
            ],
        })
    if not servers:
        # ast-grep-ignore: toolresult-text-literal-with-data - the data is empty; the text says so
        return ToolResult(
            text="No connected MCP server offers events to this user.",
            data={"servers": []},
        )
    return ToolResult(data={"servers": servers})
