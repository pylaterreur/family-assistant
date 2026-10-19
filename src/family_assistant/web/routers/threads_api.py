"""Home and its threads, for the web and iOS clients.

See docs/design/one-thread-chat-ux.md.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from family_assistant.config_models import AppConfig
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.conversation_threads import (
    ConversationThread,
)
from family_assistant.threads.echo import announce_home_change
from family_assistant.threads.home import (
    ensure_web_home,
    find_home,
    home_owner_id,
    viewer_for,
)
from family_assistant.threads.listing import list_home_threads, summarize_threads
from family_assistant.web.dependencies import get_current_user, get_db
from family_assistant.web.routers.chat_api import (
    ThreadSummaryResponse,
    get_stream_hub,
    thread_summary_response,
    turn_running_in,
)

threads_api_router = APIRouter()


class HomeResponse(BaseModel):
    """The caller's Home conversation."""

    enabled: bool = Field(
        ..., description="Whether threads are enabled. When false there is no Home."
    )
    conversation_id: str | None = Field(
        default=None,
        description="The caller's Home conversation, designated on first request",
    )


class HomeThreadsResponse(BaseModel):
    """The threads of the caller's Home, newest first."""

    home_conversation_id: str | None
    threads: list[ThreadSummaryResponse]


class MarkReadRequest(BaseModel):
    """How far the caller has read a conversation."""

    through_message_id: int | None = Field(
        default=None,
        description=(
            "The newest message the caller has read. Omit to mark everything "
            "read. A position behind the caller's current one is ignored."
        ),
    )


class MarkReadResponse(BaseModel):
    """The caller's unread count once the read is recorded."""

    conversation_id: str
    unread_count: int


def _app_config(request: Request) -> AppConfig:
    return request.app.state.config


def _user_id(current_user: dict) -> str:
    user_id = current_user.get("user_identifier")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user_id


async def _owned_thread(
    db: Database, config: AppConfig, conversation_id: str, user_id: str
) -> ConversationThread:
    """The caller's own thread, or 404 for anything else."""
    if not config.conversation_threads.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    thread = await db.conversation_threads.get_thread(conversation_id)
    if thread is None or thread.owner_user_id != home_owner_id(config, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return thread


@threads_api_router.get("/v1/chat/home")
async def get_home(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> HomeResponse:
    """The caller's web Home, designated the first time it is asked for."""
    config = _app_config(request)
    if not config.conversation_threads.enabled:
        return HomeResponse(enabled=False)
    home = await ensure_web_home(
        db_context, config, user_id=_user_id(current_user), now=datetime.now(UTC)
    )
    return HomeResponse(enabled=True, conversation_id=home)


@threads_api_router.get("/v1/chat/home/threads")
async def get_home_threads(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> HomeThreadsResponse:
    """The threads of the caller's web Home, newest first.

    Only the caller's own Home is ever read: it is looked up from their
    identity, never named by the request.
    """
    config = _app_config(request)
    if not config.conversation_threads.enabled:
        return HomeThreadsResponse(home_conversation_id=None, threads=[])
    user_id = _user_id(current_user)
    home = await find_home(db_context, config, user_id=user_id, interface_type="web")
    if home is None:
        return HomeThreadsResponse(home_conversation_id=None, threads=[])
    threads = await list_home_threads(
        db_context,
        home,
        turn_running=turn_running_in(get_stream_hub(request)),
        viewer=viewer_for(config, user_id),
    )
    return HomeThreadsResponse(
        home_conversation_id=home,
        threads=[thread_summary_response(thread, home) for thread in threads],
    )


@threads_api_router.post("/v1/chat/home/threads/{conversation_id}/done")
async def mark_thread_done(
    conversation_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> ThreadSummaryResponse:
    """Close one of the caller's threads as done.

    Nothing is echoed into Home: the thread's last message may be a question or
    a progress update rather than an outcome, so the card just shows that the
    user closed it. Anything that arrives in the thread later reopens it.
    """
    config = _app_config(request)
    user_id = _user_id(current_user)
    thread = await _owned_thread(db_context, config, conversation_id, user_id)
    await db_context.conversation_threads.close_thread(
        conversation_id,
        outcome="done",
        closed_by="user",
        turn_id=None,
        now=datetime.now(UTC),
    )
    hub = get_stream_hub(request)
    await announce_home_change(
        hub,
        home_conversation_id=thread.home_conversation_id,
        owner_user_id=thread.owner_user_id,
        reason="thread_closed",
    )
    await hub.publish_activity(
        conversation_id, user_id=thread.owner_user_id, reason="thread_closed"
    )
    closed = await db_context.conversation_threads.get_thread(conversation_id)
    assert closed is not None
    [summary] = await summarize_threads(
        db_context,
        [closed],
        turn_running=turn_running_in(hub),
        viewer=viewer_for(config, user_id),
    )
    return thread_summary_response(summary, thread.home_conversation_id)


@threads_api_router.post("/v1/chat/conversations/{conversation_id}/read")
async def mark_conversation_read(
    conversation_id: str,
    payload: MarkReadRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> MarkReadResponse:
    """Record how far the caller has read one of their threads, or their Home.

    The marker is the caller's own: another member of the household reading
    the same conversation keeps their own count. Ordinary conversations have
    no unread count, so they have no marker either.
    """
    config = _app_config(request)
    user_id = _user_id(current_user)
    viewer = viewer_for(config, user_id)
    home = (
        await find_home(db_context, config, user_id=user_id, interface_type="web")
        if config.conversation_threads.enabled
        else None
    )
    if home != conversation_id:
        await _owned_thread(db_context, config, conversation_id, user_id)
    latest = (
        await db_context.message_history.latest_visible_messages([conversation_id])
    ).get(conversation_id)
    if latest is not None:
        # Bounded by what exists, so a position ahead of the conversation
        # cannot mark messages read before they arrive.
        through = (
            latest.internal_id
            if payload.through_message_id is None
            else min(payload.through_message_id, latest.internal_id)
        )
        await db_context.conversation_threads.mark_read(
            conversation_id,
            viewer_user_id=viewer.user_id,
            through_message_id=through,
            now=datetime.now(UTC),
        )
        await get_stream_hub(request).publish_activity(
            conversation_id, user_id=viewer.user_id, reason="read"
        )
    unread = await db_context.conversation_threads.unread_counts(
        [conversation_id], viewer_user_id=viewer.user_id, viewer_keys=viewer.keys
    )
    return MarkReadResponse(
        conversation_id=conversation_id,
        unread_count=unread.get(conversation_id, 0),
    )
