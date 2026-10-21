from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request, status

from family_assistant.web.auth import AuthService

if TYPE_CHECKING:
    from authlib.integrations.starlette_client import OAuth


class FakeOidcProvider:
    """Stands in for authlib's registered OIDC client, answering the token exchange."""

    def __init__(self) -> None:
        # ast-grep-ignore: no-dict-any - matches the external OAuth token response
        self.token: dict[str, Any] = {}

    # ast-grep-ignore: no-dict-any - matches the external OAuth token response
    async def authorize_access_token(self, request: Request) -> dict[str, Any]:
        return self.token


class FakeOAuth:
    """Stands in for authlib's ``OAuth`` registry with one OIDC provider."""

    def __init__(self) -> None:
        self.oidc_provider = FakeOidcProvider()


@pytest.fixture
def fake_oauth() -> FakeOAuth:
    return FakeOAuth()


@pytest.fixture
def auth_service(fake_oauth: FakeOAuth) -> AuthService:
    service = AuthService()
    service.oauth = cast("OAuth", fake_oauth)
    return service


def _request(session: dict[str, object] | None = None) -> Request:
    return Request({
        "type": "http",
        "app": FastAPI(),
        "session": session if session is not None else {},
    })


@pytest.mark.asyncio
async def test_handle_auth_callback_no_allowlist(
    auth_service: AuthService, fake_oauth: FakeOAuth
) -> None:
    """Test that any email is allowed when ALLOWED_OIDC_EMAILS is not set."""
    with patch("family_assistant.web.auth.ALLOWED_OIDC_EMAILS", None):
        user_info = {"email": "test@example.com", "sub": "123"}
        fake_oauth.oidc_provider.token = {"userinfo": user_info}
        request = _request({
            "redirect_after_login": "/",
            "api_token_id": 17,
            "session_jwt_exp": 1,
        })

        response = await auth_service.handle_auth_callback(request)

        # Starlette's RedirectResponse defaults to 307
        assert response.status_code in {302, 307}
        assert request.session["user"] == user_info
        assert "api_token_id" not in request.session
        assert "session_jwt_exp" not in request.session


@pytest.mark.asyncio
async def test_handle_auth_callback_with_allowlist_success(
    auth_service: AuthService, fake_oauth: FakeOAuth
) -> None:
    """Test that email in allowlist is allowed."""
    with patch(
        "family_assistant.web.auth.ALLOWED_OIDC_EMAILS",
        "allowed@example.com, second@test.com",
    ):
        user_info = {
            "email": "ALLOWED@example.com",
            "sub": "123",
        }  # Case insensitive check
        fake_oauth.oidc_provider.token = {"userinfo": user_info}
        request = _request({"redirect_after_login": "/"})

        response = await auth_service.handle_auth_callback(request)

        assert response.status_code in {302, 307}
        assert request.session["user"] == user_info


@pytest.mark.asyncio
async def test_handle_auth_callback_with_allowlist_denied(
    auth_service: AuthService, fake_oauth: FakeOAuth
) -> None:
    """Test that email NOT in allowlist is denied."""
    with patch("family_assistant.web.auth.ALLOWED_OIDC_EMAILS", "allowed@example.com"):
        fake_oauth.oidc_provider.token = {
            "userinfo": {"email": "hacker@example.com", "sub": "123"}
        }

        with pytest.raises(HTTPException) as excinfo:
            await auth_service.handle_auth_callback(_request())

        assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
        assert "not in the allowlist" in excinfo.value.detail


@pytest.mark.asyncio
async def test_handle_auth_callback_with_allowlist_no_email(
    auth_service: AuthService, fake_oauth: FakeOAuth
) -> None:
    """Test that missing email is denied when allowlist is active."""
    with patch("family_assistant.web.auth.ALLOWED_OIDC_EMAILS", "allowed@example.com"):
        fake_oauth.oidc_provider.token = {"userinfo": {"sub": "123"}}  # No email

        with pytest.raises(HTTPException) as excinfo:
            await auth_service.handle_auth_callback(_request())

        assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
        assert "No email provided" in excinfo.value.detail


@pytest.mark.asyncio
async def test_logout_clears_token_session_binding(auth_service: AuthService) -> None:
    request = _request({
        "user": {"sub": "app-user", "source": "app_token_session"},
        "api_token_id": 17,
        "session_jwt_exp": 1,
    })

    response = await auth_service.handle_logout(request)

    assert response.status_code in {302, 307}
    assert request.session == {}
    cookies = response.headers.getlist("set-cookie")
    assert len(cookies) == 2
    for cookie, path in zip(cookies, ("/api", "/app"), strict=True):
        assert cookie.startswith('fa_access_token="";')
        assert "Max-Age=0" in cookie
        assert f"Path={path}" in cookie
