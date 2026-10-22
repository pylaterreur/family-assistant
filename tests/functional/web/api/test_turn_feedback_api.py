"""Functional tests for per-turn thumbs up/down feedback."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.storage.database import Database
from family_assistant.web.dependencies import get_current_user

OWNER = "test_user"


async def _conversation_with_turn(db: Database) -> tuple[str, str]:
    conversation_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="What's for dinner?"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        turn_id=turn_id,
        user_id=OWNER,
    )
    await db.message_history.add_message(
        AssistantMessage(content="Pasta."),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        turn_id=turn_id,
        user_id=OWNER,
        reasoning_info={"model_tier": "deep", "model_tier_source": "default"},
    )
    return conversation_id, turn_id


def _feedback_url(conversation_id: str, turn_id: str) -> str:
    return f"/api/v1/chat/conversations/{conversation_id}/turns/{turn_id}/feedback"


@pytest.mark.asyncio
async def test_rating_round_trips_through_conversation_messages(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(db_engine)
    conversation_id, turn_id = await _conversation_with_turn(db)

    response = await api_test_client.put(
        _feedback_url(conversation_id, turn_id),
        json={"rating": "down", "comment": "  Too short  "},
    )
    assert response.status_code == 200
    assert response.json()["rating"] == "down"
    assert response.json()["comment"] == "Too short"

    messages = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert [msg["feedback"] for msg in messages.json()["messages"]] == [
        "down",
        "down",
    ]

    replaced = await api_test_client.put(
        _feedback_url(conversation_id, turn_id), json={"rating": "up"}
    )
    assert replaced.status_code == 200
    stored = await db.turn_feedback.get(turn_id=turn_id, user_id=OWNER)
    assert stored is not None
    assert stored.rating == "up"
    assert stored.comment is None

    cleared = await api_test_client.delete(_feedback_url(conversation_id, turn_id))
    assert cleared.status_code == 204
    assert await db.turn_feedback.get(turn_id=turn_id, user_id=OWNER) is None
    messages = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert all(msg["feedback"] is None for msg in messages.json()["messages"])


@pytest.mark.asyncio
async def test_rating_requires_a_reply_in_that_conversation(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(db_engine)
    conversation_id, turn_id = await _conversation_with_turn(db)
    other_conversation_id, _ = await _conversation_with_turn(db)

    unknown_turn = await api_test_client.put(
        _feedback_url(conversation_id, str(uuid.uuid4())), json={"rating": "up"}
    )
    assert unknown_turn.status_code == 404
    wrong_conversation = await api_test_client.put(
        _feedback_url(other_conversation_id, turn_id), json={"rating": "up"}
    )
    assert wrong_conversation.status_code == 404
    invalid = await api_test_client.put(
        _feedback_url(conversation_id, turn_id), json={"rating": "meh"}
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_rating_someone_elses_conversation_is_not_found(
    api_test_client: AsyncClient, app_fixture: FastAPI, db_engine: AsyncEngine
) -> None:
    db = Database(db_engine)
    conversation_id, turn_id = await _conversation_with_turn(db)

    async def other_user() -> dict[str, str]:
        return {"user_identifier": "someone_else"}

    app_fixture.dependency_overrides[get_current_user] = other_user
    response = await api_test_client.put(
        _feedback_url(conversation_id, turn_id), json={"rating": "down"}
    )
    assert response.status_code == 404
    assert await db.turn_feedback.get(turn_id=turn_id, user_id="someone_else") is None
