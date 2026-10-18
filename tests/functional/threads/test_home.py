"""Which conversation is a user's Home."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from family_assistant.config_models import (
    AppConfig,
    OIDCUserIdentityConfig,
    UserIdentityConfig,
)
from family_assistant.storage.database import Database
from family_assistant.threads.home import ensure_web_home, find_home

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
CONFIG = AppConfig(
    users=[
        UserIdentityConfig(
            id="alice",
            oidc=OIDCUserIdentityConfig(
                emails={"alice@example.com"}, subjects={"oidc-sub-1"}
            ),
        ),
        UserIdentityConfig(
            id="bob", oidc=OIDCUserIdentityConfig(subjects={"oidc-sub-2"})
        ),
    ]
)


@pytest.mark.asyncio
async def test_a_users_web_home_is_stable(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)

    first = await ensure_web_home(db, CONFIG, user_id="alice", now=NOW)
    second = await ensure_web_home(db, CONFIG, user_id="alice", now=NOW)

    assert first == second


@pytest.mark.asyncio
async def test_every_login_of_one_person_reaches_the_same_home(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    by_subject = await ensure_web_home(db, CONFIG, user_id="oidc-sub-1", now=NOW)
    by_email = await ensure_web_home(db, CONFIG, user_id="alice@example.com", now=NOW)

    assert by_subject == by_email


@pytest.mark.asyncio
async def test_different_people_have_different_homes(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)

    alice = await ensure_web_home(db, CONFIG, user_id="alice", now=NOW)
    bob = await ensure_web_home(db, CONFIG, user_id="bob", now=NOW)

    assert alice != bob


@pytest.mark.asyncio
async def test_voice_turns_belong_to_the_web_home(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    home = await ensure_web_home(db, CONFIG, user_id="alice", now=NOW)

    assert await find_home(db, CONFIG, user_id="alice", interface_type="voice") == home


@pytest.mark.asyncio
async def test_a_home_survives_mapping_its_login_to_a_user(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    before = await ensure_web_home(db, AppConfig(), user_id="oidc-sub-9", now=NOW)
    mapped = AppConfig(
        users=[
            UserIdentityConfig(
                id="carol", oidc=OIDCUserIdentityConfig(subjects={"oidc-sub-9"})
            )
        ]
    )

    after = await ensure_web_home(db, mapped, user_id="carol", now=NOW)

    assert after == before
