"""Tests for the read-only diagnostics token on the errors/diagnostics endpoints.

The ``DIAGNOSTICS_READONLY_TOKEN`` env var lets an external monitor read the
error-log and diagnostics-export endpoints without a full user session or API
token, while every other endpoint stays protected.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm.messages import UserMessage
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import DIAGNOSTICS_READONLY_TOKEN_ENV_VAR
from family_assistant.web.route_auth import DIAGNOSTICS_TOKEN_HEADER

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    import pytest as _pytest
    from sqlalchemy.ext.asyncio import AsyncEngine

READONLY_ENDPOINTS = [
    "/api/errors/",
    "/api/errors/telemetry",
    "/api/diagnostics/export",
    "/api/diagnostics/taint-audit",
]
READONLY_TOKEN = "ro-secret-token-value"


class _FakeAuthService:
    """AuthService stand-in with auth enabled and no valid sessions/tokens."""

    auth_enabled = True
    oauth = None

    async def get_user_from_api_token(
        self,
        auth_header: str,
        request: object,
    ) -> None:
        return None


def _enable_auth() -> object | None:
    original = getattr(fastapi_app.state, "auth_service", None)
    fastapi_app.state.auth_service = _FakeAuthService()
    return original


def _restore_auth(original: object | None) -> None:
    if original is not None:
        fastapi_app.state.auth_service = original
    elif hasattr(fastapi_app.state, "auth_service"):
        del fastapi_app.state.auth_service


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_rejects_unauthenticated_when_no_token_configured(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """With auth enabled and no read-only token configured, access is refused."""
    monkeypatch.delenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, raising=False)
    original = _enable_auth()
    try:
        response = await api_client.get(endpoint)
        assert response.status_code == 401
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_allows_readonly_token_bearer(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A matching read-only token via Authorization: Bearer grants read access."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            endpoint,
            headers={"Authorization": f"Bearer {READONLY_TOKEN}"},
        )
        assert response.status_code == 200
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_allows_readonly_token_x_api_token_header(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A matching read-only token via X-API-Token grants read access."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            endpoint,
            headers={"X-API-Token": READONLY_TOKEN},
        )
        assert response.status_code == 200
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_rejects_wrong_readonly_token(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A non-matching token falls through to normal auth and is refused."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            endpoint,
            headers={"Authorization": "Bearer not-the-right-token"},
        )
        assert response.status_code == 401
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_rejects_non_ascii_token_without_500(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A non-ASCII token falls through to normal auth (401), not a 500.

    secrets.compare_digest raises TypeError on non-ASCII str inputs, so the
    comparison must be done on bytes to avoid leaking a 500.
    """
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        # Send the value as latin-1 bytes (valid on the wire); Starlette decodes
        # headers as latin-1, so the server sees a non-ASCII str token.
        response = await api_client.get(
            endpoint,
            headers={b"Authorization": "Bearer tökén-with-ünïcode".encode("latin-1")},
        )
        assert response.status_code == 401
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
async def test_readonly_token_does_not_unlock_other_endpoints(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """The read-only token only covers diagnostics, not other protected endpoints."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            "/api/debug/profiles",
            headers={"Authorization": f"Bearer {READONLY_TOKEN}"},
        )
        assert response.status_code == 401
    finally:
        _restore_auth(original)


async def _shared_conversation(
    db_engine: AsyncEngine, tmp_path: Path
) -> tuple[str, str, str]:
    """Persist a shared conversation; return (share token, conversation id, attachment id)."""
    db = Database(engine=db_engine)
    conversation_id = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Why did the brief not fire?"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
    )
    attachment_registry = AttachmentRegistry(str(tmp_path), db_engine)
    fastapi_app.state.attachment_registry = attachment_registry
    attachment = await attachment_registry.register_user_attachment(
        db,
        b"trace details",
        "trace.txt",
        "text/plain",
        conversation_id=conversation_id,
        user_id="test_user",
    )
    token = secrets.token_urlsafe(32)
    await db.conversation_shares.rotate(
        conversation_id,
        "test_user",
        hashlib.sha256(token.encode()).hexdigest(),
    )
    return token, conversation_id, attachment.attachment_id


@pytest.mark.asyncio
async def test_readonly_token_header_reads_shared_conversation(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """The diagnostics token in X-Diagnostics-Token reads a shared transcript."""
    token, _, _ = await _shared_conversation(db_engine, tmp_path)
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            f"/api/v1/shared-conversations/{token}/messages",
            headers={DIAGNOSTICS_TOKEN_HEADER: READONLY_TOKEN},
        )
    finally:
        _restore_auth(original)
    assert response.status_code == 200
    assert [m["content"] for m in response.json()["messages"]] == [
        "Why did the brief not fire?"
    ]


@pytest.mark.asyncio
async def test_readonly_token_header_reads_shared_attachment(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """The diagnostics token also fetches an attachment of the shared conversation."""
    token, _, attachment_id = await _shared_conversation(db_engine, tmp_path)
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            f"/api/v1/shared-conversations/{token}/attachments/{attachment_id}",
            headers={DIAGNOSTICS_TOKEN_HEADER: READONLY_TOKEN},
        )
    finally:
        _restore_auth(original)
    assert response.status_code == 200
    assert response.content == b"trace details"


@pytest.mark.asyncio
async def test_shared_conversation_rejects_wrong_readonly_token(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """A share link alone, with a wrong diagnostics token, is refused."""
    token, _, _ = await _shared_conversation(db_engine, tmp_path)
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            f"/api/v1/shared-conversations/{token}/messages",
            headers={DIAGNOSTICS_TOKEN_HEADER: "not-the-right-token"},
        )
    finally:
        _restore_auth(original)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_readonly_token_does_not_read_unshared_conversation(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """Without the share link, the conversation endpoints stay closed to it."""
    _, conversation_id, _ = await _shared_conversation(db_engine, tmp_path)
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            headers={DIAGNOSTICS_TOKEN_HEADER: READONLY_TOKEN},
        )
    finally:
        _restore_auth(original)
    assert response.status_code == 401
