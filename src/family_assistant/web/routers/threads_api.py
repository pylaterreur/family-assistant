"""Home and its threads, for the web and iOS clients.

See docs/design/one-thread-chat-ux.md.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from family_assistant.config_models import AppConfig
from family_assistant.storage.database import Database
from family_assistant.threads.home import ensure_web_home, find_home
from family_assistant.threads.listing import list_home_threads
from family_assistant.web.dependencies import get_current_user, get_db
from family_assistant.web.routers.chat_api import _get_hub

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


class ThreadSummaryResponse(BaseModel):
    """One thread of a Home."""

    conversation_id: str
    title: str
    profile_id: str
    state: Literal["working", "waiting", "failed"] = Field(
        ...,
        description=(
            "working while a turn or the starting run is in progress; failed "
            "when the starting run failed and nothing has been said since; "
            "waiting otherwise"
        ),
    )
    source_tool_call_id: str | None = Field(
        default=None, description="The Home tool call that started the thread, if any"
    )
    seed_request: str | None = Field(
        default=None, description="The request the thread was started with"
    )
    preview: str | None = Field(
        default=None, description="The thread's latest visible message"
    )
    created_at: datetime
    updated_at: datetime


class HomeThreadsResponse(BaseModel):
    """The threads of the caller's Home, newest first."""

    home_conversation_id: str | None
    threads: list[ThreadSummaryResponse]


def _app_config(request: Request) -> AppConfig:
    return request.app.state.config


def _user_id(current_user: dict) -> str:
    user_id = current_user.get("user_identifier")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user_id


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
    home = await find_home(
        db_context, config, user_id=_user_id(current_user), interface_type="web"
    )
    if home is None:
        return HomeThreadsResponse(home_conversation_id=None, threads=[])
    hub = _get_hub(request)
    threads = await list_home_threads(
        db_context,
        home,
        turn_running=lambda conversation_id: any(
            turn.status == "running" for turn in hub.active_turns(conversation_id)
        ),
    )
    return HomeThreadsResponse(
        home_conversation_id=home,
        threads=[
            ThreadSummaryResponse(
                conversation_id=thread.conversation_id,
                title=thread.title,
                profile_id=thread.profile_id,
                state=thread.state,
                source_tool_call_id=thread.source_tool_call_id,
                seed_request=thread.seed_request,
                preview=thread.preview,
                created_at=thread.created_at,
                updated_at=thread.updated_at,
            )
            for thread in threads
        ],
    )
