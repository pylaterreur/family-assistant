"""The thread an automation's "new conversation" wake starts off its owner's Home.

With threads on, a wake whose destination is ``new`` does not open an unlinked
conversation: each firing starts a thread of the owner's Home, through the same
creation path as every other thread, and its turn runs there. From then on it
is an ordinary thread -- it can be finished, echoed and reopened.

The thread opens with nothing copied into it. The wake's own prompt is the
turn's first row, written by the callback handler as machine-authored data, as
every wake prompt is, so it is never stored as the user's words.

See docs/design/one-thread-chat-ux.md, "Deliberate simplifications".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from family_assistant.threads.creation import NewThread, create_thread, title_from_text
from family_assistant.threads.echo import announce_home_change
from family_assistant.threads.home import ensure_web_home, home_owner_id

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.config_models import AppConfig
    from family_assistant.processing.types import ProcessingServiceConfig
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import Database
    from family_assistant.threads.echo import HomeActivityPublisher

logger = logging.getLogger(__name__)

WAKE_THREAD_FALLBACK_TITLE = "Automation"
"""The title of a wake thread whose automation has no name and no message."""


def wake_thread_title(
    *, automation_name: str | None, message: str | None, when: datetime
) -> str:
    """A wake thread's title: what fired, and the day it fired.

    The automation's name says what it is; a wake with none -- a one-off
    callback, or a script's hand-off -- is named by the first line of its
    message instead. The date tells one firing's card from the next.
    """
    base = (
        title_from_text(automation_name or "")
        or title_from_text(message or "")
        or WAKE_THREAD_FALLBACK_TITLE
    )
    return f"{base} · {when.day} {when:%b}"


async def ensure_wake_thread(
    db: Database,
    app_config: AppConfig,
    *,
    conversation_id: str,
    owner_user_id: str,
    profile: ProcessingServiceConfig,
    title: str,
    title_taint: TaintMetadata,
    turn_id: str,
    now: datetime,
    publisher: HomeActivityPublisher | None,
) -> None:
    """Record ``conversation_id`` as a thread of the owner's web Home.

    ``conversation_id`` is derived from the task, so a retry finds the thread
    its first attempt created rather than starting a second. ``profile`` is the
    profile the wake runs on, which the thread is then pinned to: its history
    is stamped with that profile, so later turns must run there to see it.
    ``turn_id`` is the wake's turn, the one that starts the thread.
    """
    if await db.conversation_threads.get_thread(conversation_id) is not None:
        return
    home = await ensure_web_home(db, app_config, user_id=owner_user_id, now=now)
    try:
        await create_thread(
            db,
            thread=NewThread(
                conversation_id=conversation_id,
                home_conversation_id=home,
                interface_type="web",
                owner_user_id=home_owner_id(app_config, owner_user_id),
                user_id=owner_user_id,
                profile=profile,
                title=title,
                title_taint=title_taint,
            ),
            seed=None,
            turn_id=turn_id,
            now=now,
        )
    except IntegrityError:
        # An overlapping attempt of the same task created it first.
        if await db.conversation_threads.get_thread(conversation_id) is None:
            raise
        return
    logger.info(
        "Wake started thread %s of Home %s on profile %s",
        conversation_id,
        home,
        profile.id,
    )
    await announce_home_change(
        publisher,
        home_conversation_id=home,
        user_id=owner_user_id,
        reason="thread_started",
    )
