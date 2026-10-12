"""Unit tests for the tool-error record kept during a script run."""

from family_assistant.scripting.monty_engine import ScriptToolErrors


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
