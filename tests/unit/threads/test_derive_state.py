"""A thread's state, derived from what is known about it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from family_assistant.storage.repositories.conversation_threads import ThreadClose
from family_assistant.threads.listing import ThreadState, derive_state

pytestmark = pytest.mark.no_db

FAILED_AT = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
CLOSING_TURN = "turn-closing"
MINUTE = timedelta(minutes=1)


def _close(
    outcome: str = "done", *, by: str = "assistant", at: datetime = CLOSED_AT
) -> ThreadClose:
    return ThreadClose(
        outcome="done" if outcome == "done" else "failed",
        closed_by="assistant" if by == "assistant" else "user",
        closed_at=at,
        turn_id=CLOSING_TURN if by == "assistant" else None,
        echo_message_id=None,
    )


@pytest.mark.parametrize(
    ("run_status", "work_in_progress", "latest_visible_at", "expected"),
    [
        ("queued", False, None, "working"),
        ("running", False, None, "working"),
        ("awaiting_children", False, None, "working"),
        ("completed", True, FAILED_AT, "working"),
        ("completed", False, FAILED_AT, "waiting"),
        (None, False, FAILED_AT, "waiting"),
        ("failed", False, None, "failed"),
        ("failed", False, FAILED_AT - MINUTE, "failed"),
        ("failed", False, FAILED_AT + MINUTE, "waiting"),
        ("failed", True, None, "working"),
    ],
)
def test_derive_state_without_a_close(
    run_status: str | None,
    work_in_progress: bool,
    latest_visible_at: datetime | None,
    expected: ThreadState,
) -> None:
    derived = derive_state(
        run_status=run_status,
        run_completed_at=FAILED_AT if run_status == "failed" else None,
        work_in_progress=work_in_progress,
        latest_visible_at=latest_visible_at,
    )
    assert (derived.state, derived.close) == (expected, None)


@pytest.mark.parametrize(
    ("outcome", "by", "latest_visible_at", "latest_turn", "expected"),
    [
        # The final message the closing turn writes after the call is the
        # outcome, not something new.
        ("done", "assistant", CLOSED_AT + MINUTE, CLOSING_TURN, "done"),
        ("failed", "assistant", CLOSED_AT + MINUTE, CLOSING_TURN, "failed"),
        ("done", "user", CLOSED_AT - MINUTE, "turn-earlier", "done"),
        (
            "done",
            "user",
            None,
            None,
            "done",
        ),
        # Anything visible in a later turn reopens the thread.
        ("done", "assistant", CLOSED_AT + MINUTE, "turn-later", "waiting"),
        ("failed", "assistant", CLOSED_AT + MINUTE, "turn-later", "waiting"),
        ("done", "user", CLOSED_AT + MINUTE, "turn-later", "waiting"),
    ],
)
def test_a_close_holds_until_something_arrives_after_it(
    outcome: str,
    by: str,
    latest_visible_at: datetime | None,
    latest_turn: str | None,
    expected: ThreadState,
) -> None:
    close = _close(outcome, by=by)

    derived = derive_state(
        run_status="completed",
        run_completed_at=FAILED_AT,
        work_in_progress=False,
        latest_visible_at=latest_visible_at,
        latest_visible_turn_id=latest_turn,
        close=close,
    )

    assert derived.state == expected
    assert derived.close == (close if expected in {"done", "failed"} else None)


def test_a_closed_thread_with_work_in_progress_is_working() -> None:
    derived = derive_state(
        run_status="completed",
        run_completed_at=FAILED_AT,
        work_in_progress=True,
        latest_visible_at=CLOSED_AT - MINUTE,
        close=_close(),
    )

    assert derived.state == "working"


def test_a_close_after_a_failed_run_wins() -> None:
    derived = derive_state(
        run_status="failed",
        run_completed_at=FAILED_AT,
        work_in_progress=False,
        latest_visible_at=CLOSED_AT + MINUTE,
        latest_visible_turn_id=CLOSING_TURN,
        close=_close("done", at=FAILED_AT + MINUTE),
    )

    assert derived.state == "done"


def test_a_turn_whose_error_is_the_latest_thing_is_failed() -> None:
    derived = derive_state(
        run_status="completed",
        run_completed_at=FAILED_AT,
        work_in_progress=False,
        latest_visible_at=FAILED_AT + MINUTE,
        latest_visible_is_error=True,
    )

    assert derived.state == "failed"


def test_an_error_in_a_later_turn_fails_a_closed_thread() -> None:
    derived = derive_state(
        run_status="completed",
        run_completed_at=FAILED_AT,
        work_in_progress=False,
        latest_visible_at=CLOSED_AT + MINUTE,
        latest_visible_turn_id="turn-later",
        latest_visible_is_error=True,
        close=_close(),
    )

    assert (derived.state, derived.close) == ("failed", None)


def test_an_error_in_the_closing_turn_leaves_the_thread_closed() -> None:
    derived = derive_state(
        run_status="completed",
        run_completed_at=FAILED_AT,
        work_in_progress=False,
        latest_visible_at=CLOSED_AT + MINUTE,
        latest_visible_turn_id=CLOSING_TURN,
        latest_visible_is_error=True,
        close=_close(),
    )

    assert derived.state == "done"


def test_a_run_failing_after_its_turn_closed_the_thread_is_failed() -> None:
    derived = derive_state(
        run_status="failed",
        run_completed_at=CLOSED_AT + MINUTE,
        work_in_progress=False,
        latest_visible_at=CLOSED_AT,
        latest_visible_turn_id=CLOSING_TURN,
        close=_close("done"),
    )

    assert (derived.state, derived.close) == ("failed", None)
