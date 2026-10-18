"""What a prepared program can reach, judged before it runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.scripting.invocation import (
    PreparedScriptInvocation,
    ScriptReviewContext,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition

pytestmark = pytest.mark.no_db


def _tool(name: str) -> ToolDefinition:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _invocation(
    source: str, *, bindings: tuple[dict[str, object], ...] = ()
) -> PreparedScriptInvocation:
    return PreparedScriptInvocation(
        review=ScriptReviewContext(
            source=source,
            inputs={},
            tools=(_tool("send_email"), _tool("read_notes"), _tool("add_note")),
            external_functions=("llm", "json_*", "keychute_http_request"),
            script_bindings=bindings,
        ),
        globals={},
    )


def test_named_tools_are_reachable_called_or_not() -> None:
    source = 'notes = read_notes()\nsender = send_email\nllm("summarise")'

    assert _invocation(source).reachable_capabilities() == {
        "read_notes",
        "send_email",
        "llm",
    }


def test_a_tool_named_only_in_a_string_is_not_reachable() -> None:
    assert _invocation('print("send_email")').reachable_capabilities() == set()


def test_dispatch_by_name_reaches_every_tool() -> None:
    reachable = _invocation(
        'tools_execute("add_note", title="x")'
    ).reachable_capabilities()

    assert {"send_email", "read_notes", "add_note"} <= reachable


def test_a_bound_childs_source_counts() -> None:
    invocation = _invocation(
        'execute_script(name="child")',
        bindings=({"name": "child", "script_code": "add_note(title='x')"},),
    )

    assert "add_note" in invocation.reachable_capabilities()


def test_a_source_that_does_not_parse_reaches_everything() -> None:
    reachable = _invocation("def (:\n").reachable_capabilities()

    assert {"send_email", "read_notes", "add_note", "keychute_http_request"} <= (
        reachable
    )
