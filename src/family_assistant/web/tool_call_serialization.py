"""The one shape a tool call takes on its way to a client."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from family_assistant.tools.display import describe_tool_call

if TYPE_CHECKING:
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.web.models import ToolCallResponseItem


def serialize_tool_call(tool_call: ToolCallItem) -> ToolCallResponseItem:
    """A tool call as the live stream and the history API send it.

    Every path that hands a tool call to a client goes through here, so each
    carries the same ``display`` phrases.
    """
    arguments = tool_call.function.arguments
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {
        "id": tool_call.id,
        "type": tool_call.type,
        "function": {"name": tool_call.function.name, "arguments": arguments},
        "display": describe_tool_call(tool_call.function.name, arguments),
    }
