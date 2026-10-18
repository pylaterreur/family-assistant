"""Which program's watch covers a running script's calls."""

from __future__ import annotations

import pytest

from family_assistant.scripting.invocation import (
    PreparedScriptInvocation,
    ScriptExecutionScope,
    ScriptReviewContext,
)

pytestmark = pytest.mark.no_db


def _invocation(
    *,
    approved: bool = False,
    bound_child: bool = False,
    watched: frozenset[str] = frozenset(),
) -> PreparedScriptInvocation:
    return PreparedScriptInvocation(
        review=ScriptReviewContext(
            source="pass", inputs={}, tools=(), external_functions=()
        ),
        globals={},
        approved=approved,
        bound_child=bound_child,
        watched_tools=watched,
    )


def test_bound_child_is_covered_by_its_programs_watch() -> None:
    parent = ScriptExecutionScope(
        _invocation(approved=True, watched=frozenset({"forward_email"}))
    )
    # A bound child inherits its parent's approval, with no watch of its own.
    child = ScriptExecutionScope(
        _invocation(approved=True, bound_child=True), parent=parent
    )

    assert child.watched_tools == {"forward_email"}


def test_independently_reviewed_child_has_only_its_own_watch() -> None:
    parent = ScriptExecutionScope(
        _invocation(approved=True, watched=frozenset({"forward_email"}))
    )
    child = ScriptExecutionScope(
        _invocation(approved=True, watched=frozenset({"send_email"})), parent=parent
    )

    assert child.watched_tools == {"send_email"}
