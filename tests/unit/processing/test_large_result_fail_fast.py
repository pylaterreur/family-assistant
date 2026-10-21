"""Unit tests for large tool-result handling in AttachmentProcessor."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing.attachments import AttachmentProcessor
from family_assistant.processing.tool_execution import ToolExecutor
from family_assistant.processing.types import ProcessingServiceConfig
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import (
    AttachmentMetadata,
    AttachmentRegistry,
)
from family_assistant.storage.database import Database
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.utils.clock import SystemClock

from .test_tool_execution_safety_confirmation import MinimalToolsProvider

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import DatabaseExecutor
    from family_assistant.tools.infrastructure import ToolsProvider


class _FailingStorageAttachmentRegistry(AttachmentRegistry):
    """Registry whose tool-attachment storage fails, recording each attempt."""

    def __init__(self, storage_path: str, db_engine: AsyncEngine) -> None:
        super().__init__(storage_path=storage_path, db_engine=db_engine, config=None)
        self.store_attempts: list[str] = []

    async def store_and_register_tool_attachment(
        self,
        file_content: bytes,
        filename: str,
        content_type: str,
        tool_name: str,
        description: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        owner_user_id: str | None = None,
        # ast-grep-ignore: no-dict-any - mirrors AttachmentRegistry's free-form metadata
        metadata: dict[str, Any] | None = None,
        db_context: DatabaseExecutor | None = None,
        *,
        taint_state: TurnTaintState,
    ) -> AttachmentMetadata:
        self.store_attempts.append(tool_name)
        raise RuntimeError("disk full")


class _TaintingToolsProvider:
    """Tools provider whose tool records result taint and returns an attachment."""

    def __init__(self, taint_metadata: TaintMetadata) -> None:
        self.taint_metadata = taint_metadata

    async def get_tool_definitions(self) -> list:
        return []

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> ToolResult:
        assert call_id is not None
        context.tool_result_taint_metadata[call_id] = self.taint_metadata
        return ToolResult(
            text="small result",
            attachments=[
                ToolAttachment(
                    content=b"tainted attachment",
                    mime_type="text/plain",
                    description="external text",
                )
            ],
        )

    async def close(self) -> None:
        pass


def _create_processor(
    attachment_registry: AttachmentRegistry | None,
    threshold_kb: int = 1,
) -> AttachmentProcessor:
    app_config = AppConfig()
    app_config.attachment_config.large_tool_result_threshold_kb = threshold_kb
    return AttachmentProcessor(
        attachment_registry=attachment_registry,
        app_config=app_config,
        clock=SystemClock(),
    )


@pytest.mark.asyncio
async def test_handle_large_result_without_registry_raises(
    db_engine: AsyncEngine,
) -> None:
    """Large content should fail fast when storage is unavailable."""
    processor = _create_processor(attachment_registry=None)
    oversized_content = "A" * 4096

    with pytest.raises(
        RuntimeError,
        match="Tool result exceeded large-result threshold but attachment storage is unavailable",
    ):
        await processor.handle_large_result(
            db_context=Database(engine=db_engine),
            content=oversized_content,
            tool_name="test_tool",
            conversation_id="conv_1",
            call_id="call_1",
        )


@pytest.mark.asyncio
async def test_handle_large_result_storage_failure_raises(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Attachment storage failures should propagate."""
    registry = _FailingStorageAttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine
    )
    processor = _create_processor(attachment_registry=registry)
    oversized_content = "B" * 4096

    with pytest.raises(RuntimeError, match="disk full"):
        await processor.handle_large_result(
            db_context=Database(engine=db_engine),
            content=oversized_content,
            tool_name="test_tool",
            conversation_id="conv_2",
            call_id="call_2",
        )

    assert registry.store_attempts == ["test_tool"]


@pytest.mark.asyncio
async def test_tool_executor_propagates_large_result_registry_unavailable(
    db_engine: AsyncEngine,
) -> None:
    """ToolExecutor should fail fast when large-result conversion cannot persist."""
    tools_provider = MinimalToolsProvider(result="X" * 4096)
    processor = _create_processor(attachment_registry=None, threshold_kb=1)
    executor = ToolExecutor(
        tools_provider=tools_provider,
        config=ProcessingServiceConfig(
            id="test",
            prompts={},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
        ),
        attachment_processor=processor,
        attachment_registry=None,
        clock=SystemClock(),
        credential_resolvers=None,
        api_backend=None,
    )

    with pytest.raises(
        RuntimeError,
        match="Tool result exceeded large-result threshold but attachment storage is unavailable",
    ):
        await executor.execute(
            tool_call_item_obj=ToolCallItem(
                id="call-1",
                type="function",
                function=ToolCallFunction(name="test_tool", arguments="{}"),
            ),
            interface_type="test",
            conversation_id="conv",
            user_name="tester",
            turn_id="turn",
            db_context=Database(engine=db_engine),
            chat_interface=None,
            request_confirmation_callback=None,
        )

    assert tools_provider.executed_tool_names == ["test_tool"]


@pytest.mark.asyncio
async def test_handle_large_result_marks_json_content_type_without_full_parse(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Large JSON-shaped output should be stored with application/json MIME type."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processor = _create_processor(attachment_registry=registry)
    db = Database(engine=db_engine)
    large_json_payload = '{"data": "' + ("X" * 4096) + '"}'

    new_content, attachment_id = await processor.handle_large_result(
        db_context=db,
        content=large_json_payload,
        tool_name="test_tool",
        conversation_id="conv_json",
        call_id="call_json",
    )

    assert attachment_id is not None
    assert "saved as attachment" in new_content
    stored = await registry.get_attachment(db, attachment_id, acting_user_id=None)
    assert stored is not None
    assert stored.mime_type == "application/json"


@pytest.mark.asyncio
async def test_handle_large_result_keeps_text_plain_for_non_json_shape(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Large plain text output should be stored with text/plain MIME type."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processor = _create_processor(attachment_registry=registry)
    db = Database(engine=db_engine)
    large_text_payload = "not-json-" + ("Y" * 4096)

    _, attachment_id = await processor.handle_large_result(
        db_context=db,
        content=large_text_payload,
        tool_name="test_tool",
        conversation_id="conv_text",
        call_id="call_text",
    )

    assert attachment_id is not None
    stored = await registry.get_attachment(db, attachment_id, acting_user_id=None)
    assert stored is not None
    assert stored.mime_type == "text/plain"


@pytest.mark.asyncio
async def test_handle_large_result_persists_taint_metadata(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Auto-converted large results should carry result taint in attachment metadata."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processor = _create_processor(attachment_registry=registry)
    db = Database(engine=db_engine)
    taint_metadata = cast(
        "TaintMetadata",
        {
            "version": "runtime_v2",
            "max_tier": "unknown_external",
            "sources": [],
        },
    )

    _, attachment_id = await processor.handle_large_result(
        db_context=db,
        content="tainted-" + ("Z" * 4096),
        tool_name="test_tool",
        conversation_id="conv_taint",
        call_id="call_taint",
        taint_metadata=taint_metadata,
    )

    assert attachment_id is not None
    stored = await registry.get_attachment(db, attachment_id, acting_user_id=None)
    assert stored is not None
    passed_state = TurnTaintState.from_metadata(stored.metadata.get("taint_metadata"))
    expected_state = TurnTaintState.from_metadata(taint_metadata)
    assert passed_state.max_tier is expected_state.max_tier
    assert passed_state.sources == expected_state.sources


def _make_executor(
    *,
    tools_provider: ToolsProvider,
    processor: AttachmentProcessor,
    attachment_registry: AttachmentRegistry | None,
) -> ToolExecutor:
    return ToolExecutor(
        tools_provider=tools_provider,
        config=ProcessingServiceConfig(
            id="test",
            prompts={},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
        ),
        attachment_processor=processor,
        attachment_registry=attachment_registry,
        clock=SystemClock(),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_tool_result_attachment_registration_persists_taint_metadata(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Explicit ToolResult attachments should carry result taint in registry metadata."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processor = _create_processor(attachment_registry=registry, threshold_kb=100)
    taint_metadata = cast(
        "TaintMetadata",
        {
            "version": "runtime_v2",
            "max_tier": "unknown_external",
            "sources": [],
        },
    )

    executor = _make_executor(
        tools_provider=_TaintingToolsProvider(taint_metadata),
        processor=processor,
        attachment_registry=registry,
    )
    db = Database(engine=db_engine)

    result = await executor.execute(
        tool_call_item_obj=ToolCallItem(
            id="call_explicit",
            type="function",
            function=ToolCallFunction(name="test_tool", arguments="{}"),
        ),
        interface_type="test",
        conversation_id="conv_explicit",
        user_name="tester",
        turn_id="turn",
        db_context=db,
        chat_interface=None,
        request_confirmation_callback=None,
    )

    assert result.auto_attachment_ids is not None
    attachment_id = result.auto_attachment_ids[0]
    assert result.llm_message.taint_metadata == taint_metadata
    assert result.stream_event.metadata == {
        "attachments": [
            {
                "type": "tool_result",
                "mime_type": "text/plain",
                "description": "external text",
                "content_url": f"/api/attachments/{attachment_id}",
                "attachment_id": attachment_id,
            }
        ]
    }
    stored = await registry.get_attachment(db, attachment_id, acting_user_id=None)
    assert stored is not None
    assert stored.mime_type == "text/plain"
    passed_state = TurnTaintState.from_metadata(stored.metadata.get("taint_metadata"))
    expected_state = TurnTaintState.from_metadata(taint_metadata)
    assert passed_state.max_tier is expected_state.max_tier
    assert passed_state.sources == expected_state.sources


@pytest.mark.asyncio
async def test_large_result_inherits_ownership_from_owned_argument_attachment(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A derived large result keeps the owner of an owned input attachment.

    A helper like jq_query run over an owner-scoped Gmail attachment must not
    launder its content into an ownerless attachment.
    """
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processor = _create_processor(attachment_registry=registry, threshold_kb=1)
    db = Database(engine=db_engine)
    owned = await registry.store_and_register_tool_attachment(
        file_content=b"owned gmail bytes",
        filename="owned.txt",
        content_type="text/plain",
        tool_name="gmail_get_attachment",
        owner_user_id="user-a",
        db_context=db,
        taint_state=TurnTaintState.empty(),
    )

    executor = _make_executor(
        tools_provider=MinimalToolsProvider(result="D" * 4096),
        processor=processor,
        attachment_registry=registry,
    )

    result = await executor.execute(
        tool_call_item_obj=ToolCallItem(
            id="call-derived",
            type="function",
            function=ToolCallFunction(
                name="jq_query",
                arguments=json.dumps({
                    "attachment_id": owned.attachment_id,
                    "query": ".",
                }),
            ),
        ),
        interface_type="test",
        conversation_id="conv-derived",
        user_name="tester",
        turn_id="turn",
        db_context=db,
        chat_interface=None,
        user_id="user-a",
        request_confirmation_callback=None,
    )

    assert result.large_result_attachment_ids is not None
    assert len(result.large_result_attachment_ids) == 1
    derived_id = result.large_result_attachment_ids[0]
    derived = await registry.get_attachment(db, derived_id, acting_user_id="user-a")
    assert derived is not None
    assert derived.owner_user_id == "user-a"
    assert await registry.get_attachment(db, derived_id, acting_user_id=None) is None


@pytest.mark.asyncio
async def test_large_result_stays_ownerless_without_owned_argument_attachment(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """An ordinary large result with no owned input attachment stays ownerless."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processor = _create_processor(attachment_registry=registry, threshold_kb=1)
    db = Database(engine=db_engine)

    executor = _make_executor(
        tools_provider=MinimalToolsProvider(result="E" * 4096),
        processor=processor,
        attachment_registry=registry,
    )

    result = await executor.execute(
        tool_call_item_obj=ToolCallItem(
            id="call-plain",
            type="function",
            function=ToolCallFunction(
                name="jq_query", arguments=json.dumps({"query": "."})
            ),
        ),
        interface_type="test",
        conversation_id="conv-derived",
        user_name="tester",
        turn_id="turn",
        db_context=db,
        chat_interface=None,
        user_id="user-a",
        request_confirmation_callback=None,
    )

    assert result.large_result_attachment_ids is not None
    assert len(result.large_result_attachment_ids) == 1
    plain_id = result.large_result_attachment_ids[0]
    plain = await registry.get_attachment(db, plain_id, acting_user_id=None)
    assert plain is not None
    assert plain.owner_user_id is None
