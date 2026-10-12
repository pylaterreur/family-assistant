"""Unit tests for the tool-error record kept during a script run."""

import pytest

from family_assistant.scripting.monty_engine import MontyEngine, ScriptToolErrors
from family_assistant.tools.types import ToolResult


def test_summary_keeps_a_bounded_sample_but_counts_every_error() -> None:
    """A tool failing in a loop yields a short summary with the full count."""
    errors = ScriptToolErrors()
    for i in range(1000):
        errors.record("flaky_tool", f"failure {i} " + "x" * 10_000)

    summary = errors.summary()

    assert errors.count == 1000
    assert summary.startswith("Completed with 1000 tool error(s): flaky_tool: ")
    assert summary.endswith("; ... and 990 more")
    assert len(summary) < 6000


@pytest.mark.parametrize(
    "result",
    [
        ToolResult(data={"status": "error", "error": "boom"}),
        ToolResult(data={"error": "boom"}),
        ToolResult(data={"success": False, "error": "boom"}),
        ToolResult(text="Error: boom"),
        "Error: boom",
    ],
)
def test_failed_tool_results_are_recorded(result: ToolResult | str) -> None:
    """Every shape the tool-outcome classifier counts as failed is recorded."""
    engine = MontyEngine()

    engine._record_tool_error("some_tool", result)

    assert engine.get_tool_errors().count == 1
    assert "boom" in engine.get_tool_errors().summary()


@pytest.mark.parametrize(
    "result",
    [ToolResult(data={"status": "ok"}), ToolResult(text="All good"), "Done"],
)
def test_successful_tool_results_are_not_recorded(result: ToolResult | str) -> None:
    engine = MontyEngine()

    engine._record_tool_error("some_tool", result)

    assert engine.get_tool_errors().count == 0
