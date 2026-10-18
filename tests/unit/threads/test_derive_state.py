"""A thread's state, derived from what is known about it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from family_assistant.threads.listing import ThreadState, derive_state

pytestmark = pytest.mark.no_db

FAILED_AT = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("run_status", "turn_running", "latest_visible_at", "expected"),
    [
        ("queued", False, None, "working"),
        ("running", False, None, "working"),
        ("awaiting_children", False, None, "working"),
        ("completed", True, FAILED_AT, "working"),
        ("completed", False, FAILED_AT, "waiting"),
        (None, False, FAILED_AT, "waiting"),
        ("failed", False, None, "failed"),
        ("failed", False, FAILED_AT - timedelta(minutes=1), "failed"),
        ("failed", False, FAILED_AT + timedelta(minutes=1), "waiting"),
        ("failed", True, None, "working"),
    ],
)
def test_derive_state(
    run_status: str | None,
    turn_running: bool,
    latest_visible_at: datetime | None,
    expected: ThreadState,
) -> None:
    assert (
        derive_state(
            run_status=run_status,
            run_completed_at=FAILED_AT if run_status == "failed" else None,
            turn_running=turn_running,
            latest_visible_at=latest_visible_at,
        )
        == expected
    )
