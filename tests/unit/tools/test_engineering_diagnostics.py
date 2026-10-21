"""Tests for engineer-profile diagnostic tools.

Covers the MCP status / reconnect tools, resolved config / profile config
inspection, and system info. The tools run against real AppConfig models, real
ProcessingService instances and a real MCPToolsProvider; only the connection
state an MCP server reached is scripted, since reaching it needs live servers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import (
    AppConfig,
    DefaultProfileSettings,
    RemoteA2AConfig,
    ServiceProfile,
)
from family_assistant.tools.catalog import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.engineering import (
    get_mcp_server_status,
    get_profile_config,
    get_profile_tool_inventory,
    get_resolved_config,
    get_system_info,
    reconnect_mcp_server,
    resolve_tool_policy,
)
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.mcp import MCPServerStatus, MCPToolsProvider
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.on_demand import OnDemandToolsView
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import MCPServerConfig, ToolExecutionContext
from tests.mocks.idle_processing_service import make_idle_processing_service
from tests.mocks.unconnected_database import unconnected_database

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.database import Database


class ScriptedStateMCPToolsProvider(MCPToolsProvider):
    """A real provider whose servers report scripted connection states.

    Reaching ``connected`` or ``failed`` needs live MCP servers; the diagnostic
    tools only read the status snapshot, so the state each server reached is
    laid over the provider's real snapshot. A reconnect succeeds and leaves the
    server connected.
    """

    def __init__(
        self,
        mcp_server_configs: dict[str, MCPServerConfig],
        states: dict[str, str],
    ) -> None:
        super().__init__(mcp_server_configs)
        self._states = dict(states)
        self.reconnected: list[str] = []

    def get_server_statuses(self) -> dict[str, MCPServerStatus]:
        snapshot = super().get_server_statuses()
        for server_id, state in self._states.items():
            snapshot[server_id]["status"] = state
        return snapshot

    async def reconnect_server(self, server_id: str) -> bool:
        self.reconnected.append(server_id)
        self._states[server_id] = "connected"
        return True


@pytest.fixture
def db() -> Database:
    """A real handle; none of these tools query the database."""
    return unconnected_database()


@pytest.fixture
def exec_context_with_db(db: Database) -> ToolExecutionContext:
    """Minimal execution context with a SQLite database handle."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conv-1",
        user_name="tester",
        turn_id=None,
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


# --- get_system_info ---


@pytest.mark.anyio
async def test_get_system_info_returns_runtime_metadata(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await get_system_info(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "python_version" in data
    assert "platform" in data
    assert data["database_dialect"] == "sqlite"


@pytest.mark.anyio
async def test_get_system_info_reports_the_build_it_is_running(
    exec_context_with_db: ToolExecutionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit is what a history query has to be anchored against.

    The image carries no repository, so without this the profile has no way to
    tell which revision it is diagnosing and "what changed since" degrades to
    whatever is on the default branch today.
    """
    monkeypatch.setenv("GIT_COMMIT", "abc1234")
    monkeypatch.setenv("BUILD_DATE", "2026-09-13T00:00:00Z")

    data = (await get_system_info(exec_context_with_db)).get_data()

    assert isinstance(data, dict)
    assert data["git_commit"] == "abc1234"
    assert data["build_date"] == "2026-09-13T00:00:00Z"


@pytest.mark.anyio
async def test_get_system_info_build_falls_back_when_unstamped(
    exec_context_with_db: ToolExecutionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local run has no build stamp; say so rather than omitting the key."""
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("BUILD_DATE", raising=False)

    data = (await get_system_info(exec_context_with_db)).get_data()

    assert isinstance(data, dict)
    assert data["git_commit"] == "unknown"
    assert data["build_date"] == "unknown"


@pytest.mark.anyio
async def test_get_system_info_handles_missing_engine() -> None:
    """If the db_context exposes no dialect, it falls back to 'unknown'."""
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="c",
        user_name="u",
        turn_id=None,
        db_context=cast("Database", object()),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )
    result = await get_system_info(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["database_dialect"] == "unknown"


# --- MCP status / reconnect ---


def _make_ctx_with_tools_provider(
    tools_provider: MCPToolsProvider, db: Database
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="c",
        user_name="u",
        turn_id=None,
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        tools_provider=tools_provider,
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.anyio
async def test_get_mcp_server_status_with_no_provider(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    """Without a tools_provider wired in, the tool reports a clear error
    rather than silently pretending all servers are healthy."""
    result = await get_mcp_server_status(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["servers"] == {}


@pytest.mark.anyio
async def test_get_mcp_server_status_aggregates_statuses(db: Database) -> None:
    """When an MCPToolsProvider is reachable, the tool dumps its statuses
    and computes a summary count."""
    provider = ScriptedStateMCPToolsProvider(
        {
            "code-execution": {
                "transport": "stdio",
                "command": "code-execution-mcp",
                "args": [],
            },
            "broken": {"transport": "sse", "url": "http://broken.invalid"},
            "starting": {"transport": "stdio", "command": "slow-mcp", "args": []},
        },
        states={"code-execution": "connected", "broken": "failed"},
    )
    ctx = _make_ctx_with_tools_provider(provider, db)

    result = await get_mcp_server_status(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["summary"] == {
        "total": 3,
        "connected": 1,
        "failed": 1,
        "cancelled": 0,
        "pending": 1,
    }
    assert data["servers"]["code-execution"]["command"] == "code-execution-mcp"
    assert data["servers"]["broken"]["url"] == "http://broken.invalid"


@pytest.mark.anyio
async def test_reconnect_mcp_server_unknown_id_returns_error(db: Database) -> None:
    provider = MCPToolsProvider({
        "a": {"transport": "stdio", "command": "a-mcp", "args": []},
        "b": {"transport": "stdio", "command": "b-mcp", "args": []},
    })

    ctx = _make_ctx_with_tools_provider(provider, db)
    result = await reconnect_mcp_server(ctx, server_id="nope")
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["configured_servers"] == ["a", "b"]


@pytest.mark.anyio
async def test_reconnect_mcp_server_success(db: Database) -> None:
    provider = ScriptedStateMCPToolsProvider(
        {
            "code-execution": {
                "transport": "stdio",
                "command": "code-execution-mcp",
                "args": [],
            }
        },
        states={"code-execution": "failed"},
    )

    ctx = _make_ctx_with_tools_provider(provider, db)
    result = await reconnect_mcp_server(ctx, server_id="code-execution")
    data = result.get_data()
    assert isinstance(data, dict)
    assert provider.reconnected == ["code-execution"]
    assert data["success"] is True
    assert data["server_id"] == "code-execution"
    assert data["status"]["status"] == "connected"


# --- get_resolved_config / get_profile_config ---


def _ctx_with_service(
    processing_service: ProcessingService, db: Database
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="c",
        user_name="u",
        turn_id=None,
        db_context=db,
        processing_service=processing_service,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


def _ctx_with_app_config(app_config: AppConfig, db: Database) -> ToolExecutionContext:
    """Build a context whose processing_service.app_config is the given config."""
    return _ctx_with_service(make_idle_processing_service(app_config=app_config), db)


@pytest.mark.anyio
async def test_get_resolved_config_omits_profile_bodies_and_redacts_embedded(
    db: Database,
) -> None:
    """The tool drops profile bodies, lists profile ids, and redacts a
    credential embedded in a value.

    Declared ``SecretStr`` credentials are covered by
    ``tests/unit/test_config_inspection.py``.
    """
    app_config = AppConfig(
        default_service_profile_id="default_assistant",
        database_url="postgresql://fa:hunter2@db.internal/family",
        service_profiles=[
            ServiceProfile(id="engineer", description="Engineer"),
            ServiceProfile(id="default_assistant", description="Default"),
        ],
    )

    ctx = _ctx_with_app_config(app_config, db)
    result = await get_resolved_config(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    cfg = data["config"]
    assert isinstance(cfg, dict)
    assert cfg["database_url"] == "postgresql://fa:[REDACTED]@db.internal/family"
    assert "service_profiles" not in cfg  # bodies omitted
    assert "default_profile_settings" not in cfg
    assert data["profile_ids"] == ["default_assistant", "engineer"]
    assert data["profile_count"] == 2
    assert data["default_service_profile_id"] == "default_assistant"


@pytest.mark.anyio
async def test_get_resolved_config_without_processing_service(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await get_resolved_config(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data


@pytest.mark.anyio
async def test_get_profile_config_lists_when_id_omitted(db: Database) -> None:
    app_config = AppConfig(
        service_profiles=[
            ServiceProfile(id="engineer", description="Engineer"),
            ServiceProfile(id="default_assistant", description="Default"),
        ],
        default_profile_settings=DefaultProfileSettings(slash_commands=["/default"]),
        default_service_profile_id="default_assistant",
    )

    ctx = _ctx_with_app_config(app_config, db)
    result = await get_profile_config(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["profile_ids"] == ["default_assistant", "engineer"]
    assert data["default_service_profile_id"] == "default_assistant"
    settings = data["default_profile_settings"]
    assert settings["slash_commands"] == ["/default"]
    assert settings["operator_tools_policy"] is None


@pytest.mark.anyio
async def test_get_profile_config_returns_specific_profile(db: Database) -> None:
    app_config = AppConfig(
        service_profiles=[
            ServiceProfile(id="default_assistant", description="Default"),
            ServiceProfile(
                id="engineer",
                description="Engineer profile",
                remote_a2a=RemoteA2AConfig(
                    agent_url="https://agent.internal/a2a?token=leak-me"
                ),
            ),
        ]
    )

    ctx = _ctx_with_app_config(app_config, db)
    result = await get_profile_config(ctx, profile_id="engineer")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["id"] == "engineer"
    assert data["description"] == "Engineer profile"
    cfg = data["config"]
    assert isinstance(cfg, dict)
    assert (
        cfg["remote_a2a"]["agent_url"] == "https://agent.internal/a2a?token=[REDACTED]"
    )


@pytest.mark.anyio
async def test_get_profile_config_unknown_id_returns_error(db: Database) -> None:
    app_config = AppConfig(service_profiles=[ServiceProfile(id="default_assistant")])

    ctx = _ctx_with_app_config(app_config, db)
    result = await get_profile_config(ctx, profile_id="does_not_exist")
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["profile_ids"] == ["default_assistant"]


# --- get_profile_tool_inventory ---


def _notes_policy_provider() -> PolicyEnforcingToolsProvider:
    return PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.NOTES]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                    ),
                ],
            )
        ),
    )


def _profile_service(
    tools_provider: PolicyEnforcingToolsProvider,
    on_demand_view: OnDemandToolsView | None = None,
) -> ProcessingService:
    service = make_idle_processing_service(tools_provider=tools_provider)
    service.on_demand_view = on_demand_view
    return service


def _ctx_with_registry(
    registry: dict[str, ProcessingService], db: Database
) -> ToolExecutionContext:
    engineer = make_idle_processing_service(profile_id="engineer")
    engineer.processing_services_registry = registry
    return _ctx_with_service(engineer, db)


@pytest.mark.anyio
async def test_get_profile_tool_inventory_summary_across_profiles(db: Database) -> None:
    provider = _notes_policy_provider()
    all_names = [
        defn["function"]["name"]
        for defn in await provider.get_tool_definitions(can_confirm=True)
    ]
    hidden = all_names[0]
    on_demand_view = OnDemandToolsView(
        wrapped_provider=provider, on_demand_tool_names={hidden}
    )
    ctx = _ctx_with_registry(
        {"notes_profile": _profile_service(provider, on_demand_view)}, db
    )

    result = await get_profile_tool_inventory(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["profile_count"] == 1
    profile = data["profiles"][0]
    assert profile["profile_id"] == "notes_profile"
    # Summary mode omits the per-tool lists.
    assert "tools" not in profile["eager"]
    assert profile["activate_tools_present"] is True
    assert profile["on_demand"]["count"] == 1


@pytest.mark.anyio
async def test_get_profile_tool_inventory_specific_profile_has_tool_lists(
    db: Database,
) -> None:
    provider = _notes_policy_provider()
    ctx = _ctx_with_registry({"notes_profile": _profile_service(provider)}, db)

    result = await get_profile_tool_inventory(ctx, profile_id="notes_profile")
    data = result.get_data()
    assert isinstance(data, dict)
    profile = data["profiles"][0]
    assert isinstance(profile["eager"]["tools"], list)
    assert profile["eager"]["tools"]
    assert profile["on_demand"]["count"] == 0


@pytest.mark.anyio
async def test_get_profile_tool_inventory_unknown_profile_returns_error(
    db: Database,
) -> None:
    ctx = _ctx_with_registry(
        {"notes_profile": _profile_service(_notes_policy_provider())}, db
    )

    result = await get_profile_tool_inventory(ctx, profile_id="missing")
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["profile_ids"] == ["notes_profile"]


@pytest.mark.anyio
async def test_get_profile_tool_inventory_without_registry_returns_error(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await get_profile_tool_inventory(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data


# --- resolve_tool_policy ---


def _diagnostic_policy_provider() -> PolicyEnforcingToolsProvider:
    """Policy provider with allow, confirm, and argument-conditional rules."""
    return PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(names=["add_or_update_note"]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                        description="notes tool allowed",
                    ),
                    PolicyRule(
                        match=ToolMatcher(
                            names=["add_or_update_note"],
                            argument_equals={"title": "secret"},
                        ),
                        decision=ToolPolicyDecision.DENY,
                        priority=20,
                        description="secret titles denied",
                    ),
                    PolicyRule(
                        match=ToolMatcher(names=["read_error_logs"]),
                        decision=ToolPolicyDecision.CONFIRM,
                        priority=10,
                        description="error logs need confirmation",
                    ),
                ],
            )
        ),
    )


def _diagnostic_service() -> ProcessingService:
    return _profile_service(_diagnostic_policy_provider())


@pytest.mark.anyio
async def test_resolve_tool_policy_allowed_tool_reports_matched_rule(
    db: Database,
) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    result = await resolve_tool_policy(ctx, tool_name="add_or_update_note")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["tool_exists"] is True
    assert data["can_confirm"] is True
    assert data["profile_count"] == 1
    entry = data["profiles"][0]
    assert entry["profile_id"] == "diag"
    assert entry["default_decision"] == "deny"
    assert entry["origin"] == "local"
    assert entry["mcp_server_id"] is None
    assert entry["on_demand"] is False
    raw = entry["raw"]
    assert raw["decision"] == "allow"
    rule = raw["matched_rule"]
    assert rule["layer"] == "defaults"
    assert rule["priority"] == 10
    assert rule["effective_priority"] == 10
    assert rule["decision"] == "allow"
    assert rule["description"] == "notes tool allowed"
    assert rule["match"] == {"names": ["add_or_update_note"]}
    assert entry["advertisement"]["decision"] == "allow"
    assert entry["execution"]["decision"] == "allow"
    # Without arguments the caller is warned that argument-conditional rules
    # cannot match.
    assert "argument_equals" in data["note"]


@pytest.mark.anyio
async def test_resolve_tool_policy_default_deny_has_no_matched_rule(
    db: Database,
) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    result = await resolve_tool_policy(ctx, tool_name="search_documents")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["tool_exists"] is True
    entry = data["profiles"][0]
    raw = entry["raw"]
    assert raw["decision"] == "deny"
    assert raw["matched_rule"] is None
    assert "default" in raw["reason"]
    assert entry["execution"]["decision"] == "deny"


@pytest.mark.anyio
async def test_resolve_tool_policy_confirm_gated_tool_depends_on_can_confirm(
    db: Database,
) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    confirmable = await resolve_tool_policy(
        ctx, tool_name="read_error_logs", can_confirm=True
    )
    data = confirmable.get_data()
    assert isinstance(data, dict)
    entry = data["profiles"][0]
    assert entry["raw"]["decision"] == "confirm"
    assert entry["execution"]["decision"] == "confirm"
    assert entry["advertisement"]["decision"] == "confirm"

    unconfirmable = await resolve_tool_policy(
        ctx, tool_name="read_error_logs", can_confirm=False
    )
    data = unconfirmable.get_data()
    assert isinstance(data, dict)
    entry = data["profiles"][0]
    assert entry["raw"]["decision"] == "confirm"
    assert entry["execution"]["decision"] == "deny"
    assert "confirmation required but unavailable" in entry["execution"]["reason"]
    assert entry["advertisement"]["decision"] == "deny"


@pytest.mark.anyio
async def test_resolve_tool_policy_argument_conditional_rule_needs_arguments(
    db: Database,
) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    without_args = await resolve_tool_policy(ctx, tool_name="add_or_update_note")
    data = without_args.get_data()
    assert isinstance(data, dict)
    assert data["profiles"][0]["raw"]["decision"] == "allow"
    assert "note" in data

    with_args = await resolve_tool_policy(
        ctx,
        tool_name="add_or_update_note",
        arguments={"title": "secret"},
    )
    data = with_args.get_data()
    assert isinstance(data, dict)
    assert data["arguments"] == {"title": "secret"}
    assert "note" not in data
    entry = data["profiles"][0]
    assert entry["raw"]["decision"] == "deny"
    assert entry["raw"]["matched_rule"]["description"] == "secret titles denied"
    assert entry["execution"]["decision"] == "deny"


@pytest.mark.anyio
async def test_resolve_tool_policy_unknown_tool_suggests_similar_names(
    db: Database,
) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    result = await resolve_tool_policy(ctx, tool_name="add_or_update_notes")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["tool_exists"] is False
    assert "add_or_update_note" in data["similar_tool_names"]
    assert len(data["similar_tool_names"]) <= 10
    assert data["profiles"][0]["profile_id"] == "diag"
    assert "error" in data["profiles"][0]


@pytest.mark.anyio
async def test_resolve_tool_policy_unknown_profile_returns_error(db: Database) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    result = await resolve_tool_policy(
        ctx, tool_name="add_or_update_note", profile_id="missing"
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["profile_ids"] == ["diag"]


@pytest.mark.anyio
async def test_resolve_tool_policy_without_registry_returns_error(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await resolve_tool_policy(
        exec_context_with_db, tool_name="add_or_update_note"
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data


@pytest.mark.anyio
async def test_resolve_tool_policy_reports_on_demand_tools(db: Database) -> None:
    provider = _diagnostic_policy_provider()
    service = _profile_service(
        provider,
        OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names={"add_or_update_note"},
        ),
    )
    ctx = _ctx_with_registry({"diag": service}, db)

    result = await resolve_tool_policy(ctx, tool_name="add_or_update_note")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["profiles"][0]["on_demand"] is True


@pytest.mark.anyio
async def test_resolve_tool_policy_rejects_non_bool_can_confirm(db: Database) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    result = await resolve_tool_policy(
        ctx,
        tool_name="add_or_update_note",
        can_confirm="true",  # type: ignore[arg-type]  # script kwargs bypass schema coercion
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "can_confirm must be a boolean" in data["error"]


@pytest.mark.anyio
async def test_resolve_tool_policy_rejects_non_dict_arguments(db: Database) -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()}, db)

    result = await resolve_tool_policy(
        ctx,
        tool_name="add_or_update_note",
        arguments=["title"],  # type: ignore[arg-type]  # script kwargs bypass schema coercion
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "arguments must be an object" in data["error"]
