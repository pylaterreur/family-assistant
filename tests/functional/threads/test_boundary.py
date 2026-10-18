"""The cross-conversation chokepoint and its three rules.

See docs/design/one-thread-chat-ux.md, "Moving content across the boundary".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.config_models import ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.memory.due import select_due_conversations
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.processing.types import ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.threads.boundary import (
    AssistantRequest,
    BoundaryDestination,
    CopiedTurns,
    CrossBoundaryCopyRefusedError,
    copy_across_boundary,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.security.taint import TaintMetadata

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
HOME = "web_conv_home"
THREAD = "web_conv_thread"
UNTRUSTED: TaintMetadata = cast(
    "TaintMetadata", {"max_tier": "unknown_external", "sources": []}
)


def _profile(profile_id: str, *, household: bool) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id=profile_id,
        receives_household_context=household,
    )


def _destination(profile: ProcessingServiceConfig) -> BoundaryDestination:
    return BoundaryDestination(
        interface_type="web",
        conversation_id=THREAD,
        profile=profile,
        user_id="alice",
    )


async def _home_turn(db: Database, *, turn_id: str) -> None:
    """A Home turn whose reply carries untrusted taint, as tool output would."""
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="what is on the calendar?"),
        interface_type="web",
        conversation_id=HOME,
        timestamp=NOW - timedelta(minutes=2),
        turn_id=turn_id,
        processing_profile_id="default_assistant",
        user_id="alice",
    )
    await db.message_history.add_message(
        AssistantMessage(content="Swimming at 4:30.", taint_metadata=UNTRUSTED),
        interface_type="web",
        conversation_id=HOME,
        timestamp=NOW - timedelta(minutes=1),
        turn_id=turn_id,
        processing_profile_id="default_assistant",
    )


async def _thread_rows(db: Database) -> list[dict[str, object]]:
    rows = await db.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == THREAD)
        .order_by(message_history_table.c.internal_id)
    )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_copied_turns_keep_taint_and_take_the_destination_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=CopiedTurns(
            source_interface_type="web",
            source_conversation_id=HOME,
            turn_ids=("home-turn",),
        ),
        turn_id="unused",
        now=NOW,
    )

    home_rows = await db.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == HOME)
        .order_by(message_history_table.c.internal_id)
    )
    thread_rows = await _thread_rows(db)
    assert [
        (row["role"], row["processing_profile_id"], row["taint_metadata_json"])
        for row in thread_rows
    ] == [
        (row["role"], "complex_tasks", row["taint_metadata_json"]) for row in home_rows
    ]


@pytest.mark.asyncio
async def test_copied_turns_are_visible_to_the_destination_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=CopiedTurns(
            source_interface_type="web",
            source_conversation_id=HOME,
            turn_ids=("home-turn",),
        ),
        turn_id="unused",
        now=NOW,
    )

    history = await db.message_history.get_recent(
        interface_type="web",
        conversation_id=THREAD,
        processing_profile_id="complex_tasks",
    )
    assert [message.content for message in history] == [
        "what is on the calendar?",
        "Swimming at 4:30.",
    ]


@pytest.mark.asyncio
async def test_turns_are_refused_by_a_profile_without_household_context(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    with pytest.raises(CrossBoundaryCopyRefusedError):
        await copy_across_boundary(
            db,
            destination=_destination(_profile("coder", household=False)),
            payload=CopiedTurns(
                source_interface_type="web",
                source_conversation_id=HOME,
                turn_ids=("home-turn",),
            ),
            turn_id="unused",
            now=NOW,
        )

    assert await _thread_rows(db) == []


@pytest.mark.asyncio
async def test_an_assistant_request_reaches_a_profile_without_household_context(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    await copy_across_boundary(
        db,
        destination=_destination(_profile("coder", household=False)),
        payload=AssistantRequest(text="Write a CSV parser.", taint=None),
        turn_id="seed-turn",
        now=NOW,
    )

    rows = await _thread_rows(db)
    assert [(row["content"], row["turn_id"]) for row in rows] == [
        ("Write a CSV parser.", "seed-turn")
    ]


@pytest.mark.asyncio
async def test_an_assistant_request_is_not_stored_as_the_users_words(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=AssistantRequest(text="Plan the Japan trip.", taint=UNTRUSTED),
        turn_id="seed-turn",
        now=NOW,
    )

    rows = await _thread_rows(db)
    assert [
        (row["role"], row["is_internal"], row["user_id"], row["taint_metadata_json"])
        for row in rows
    ] == [("user", True, "alice", UNTRUSTED)]


@pytest.mark.asyncio
async def test_an_assistant_request_does_not_make_memory_review_due(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=AssistantRequest(text="Plan the Japan trip.", taint=None),
        turn_id="seed-turn",
        now=NOW - timedelta(hours=2),
    )

    due = await select_due_conversations(
        db,
        now=NOW,
        settings=MemoryReviewSettings(
            idle_window_minutes={"web": 30},
            max_deferral_hours=24,
            contributing_interfaces=frozenset({"web"}),
        ),
        contributing_profiles={"complex_tasks": NOW - timedelta(days=1)},
    )

    assert due == []
