"""Configuration shared by the MCP runtime and application config."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field


class MCPUserCredentialConfig(BaseModel):
    """An environment-backed credential for one canonical application user."""

    model_config = ConfigDict(extra="forbid")

    token_env: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    expected_user_id: str = Field(min_length=1)


class MCPIdentityCheckConfig(BaseModel):
    """Verify the effective principal using a same-origin identity endpoint."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="/api/me", pattern=r"^/[^/\\].*$")
    expected_agent: str = Field(min_length=1)
    require_write: bool = True


class MCPUserAuthConfig(BaseModel):
    """Select a bearer credential using the trusted execution-context user."""

    model_config = ConfigDict(extra="forbid")

    call_timeout_seconds: float = Field(default=60, gt=0)
    users: dict[str, MCPUserCredentialConfig] = Field(min_length=1)
    identity_check: MCPIdentityCheckConfig | None = None


async def verify_mcp_user_identity(
    url: str | None,
    token: str | None,
    user: MCPUserCredentialConfig,
    check: MCPIdentityCheckConfig | None,
) -> None:
    """Fail if a user credential is absent or bound to someone else."""
    if not token or not url:
        raise ValueError(
            "MCP user credential is missing; configure its token environment variable"
        )
    if check is None:
        return
    endpoint = urljoin(url, check.path)
    if urlsplit(endpoint).netloc != urlsplit(url).netloc:
        raise ValueError("MCP identity check must use the server origin")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(
            endpoint, headers={"Authorization": f"Bearer {token}"}
        )
    if response.status_code != 200:
        raise ValueError(f"MCP identity check failed (HTTP {response.status_code})")
    payload = response.json()
    identity = payload.get("user") if isinstance(payload, dict) else None
    if (
        not isinstance(identity, dict)
        or identity.get("id") != user.expected_user_id
        or payload.get("agent") != check.expected_agent
        or (check.require_write and payload.get("can_write") is not True)
    ):
        raise ValueError(
            "MCP credential does not match the configured user, agent, or write capability"
        )
