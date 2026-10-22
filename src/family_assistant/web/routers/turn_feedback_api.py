"""Thumbs up/down feedback on an assistant turn.

A rating is keyed by turn and person, so it follows a turn moved into a
thread, and analysis joins it to ``message_history`` by ``turn_id`` to see
which profile, model tier and model served the rated turn.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from family_assistant.storage.database import Database
from family_assistant.storage.repositories.turn_feedback import TurnRating
from family_assistant.web.dependencies import get_current_user, get_db
from family_assistant.web.routers.chat_api import ensure_caller_owns_conversation

turn_feedback_api_router = APIRouter()

MAX_FEEDBACK_COMMENT_CHARS = 2000


class TurnFeedbackRequest(BaseModel):
    rating: TurnRating
    comment: str | None = Field(
        None,
        max_length=MAX_FEEDBACK_COMMENT_CHARS,
        description="Optional free text, typically what was wrong with the reply",
    )


class TurnFeedbackResponse(BaseModel):
    turn_id: str
    rating: TurnRating
    comment: str | None
    updated_at: datetime


async def _ensure_rateable_turn(
    request: Request,
    current_user: dict,
    db: Database,
    conversation_id: str,
    turn_id: str,
) -> str:
    user_id = await ensure_caller_owns_conversation(
        request, current_user, conversation_id
    )
    if not await db.message_history.turn_has_reply_in_conversation(
        conversation_id=conversation_id, turn_id=turn_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return user_id


@turn_feedback_api_router.put(
    "/v1/chat/conversations/{conversation_id}/turns/{turn_id}/feedback"
)
async def set_turn_feedback(
    conversation_id: str,
    turn_id: str,
    payload: TurnFeedbackRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Database, Depends(get_db)],
) -> TurnFeedbackResponse:
    """Rate a turn the caller owns, replacing any earlier rating of theirs."""
    user_id = await _ensure_rateable_turn(
        request, current_user, db, conversation_id, turn_id
    )
    comment = payload.comment.strip() if payload.comment else None
    feedback = await db.turn_feedback.set(
        turn_id=turn_id,
        user_id=user_id,
        rating=payload.rating,
        comment=comment or None,
    )
    return TurnFeedbackResponse(
        turn_id=feedback.turn_id,
        rating=feedback.rating,
        comment=feedback.comment,
        updated_at=feedback.updated_at,
    )


@turn_feedback_api_router.delete(
    "/v1/chat/conversations/{conversation_id}/turns/{turn_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def clear_turn_feedback(
    conversation_id: str,
    turn_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Database, Depends(get_db)],
) -> Response:
    """Withdraw the caller's rating of a turn. Idempotent."""
    user_id = await _ensure_rateable_turn(
        request, current_user, db, conversation_id, turn_id
    )
    await db.turn_feedback.clear(turn_id=turn_id, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
