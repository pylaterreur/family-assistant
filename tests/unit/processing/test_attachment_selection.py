"""
Unit tests for attachment selection logic in ProcessingService.

Tests the _select_attachments_for_response method and related threshold logic.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.attachments import AttachmentSelectionError
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import (
    AttachmentMetadata,
    AttachmentRegistry,
)
from family_assistant.storage.database import Database
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    MatcherArgs,
    RuleBasedMockLLMClient,
)
from tests.mocks.mock_tools_provider import (  # pylint: disable=no-name-in-module
    MockToolsProvider,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMMessage
    from family_assistant.tools.types import ToolDefinition


class _FailingLLMClient(RuleBasedMockLLMClient):
    """LLM client whose every response fails."""

    def __init__(self, error: Exception) -> None:
        super().__init__(rules=[])
        self.error = error

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        self._record_call(
            "generate_response",
            {"messages": messages, "tools": tools, "tool_choice": tool_choice},
        )
        raise self.error


class _FailingMetadataRegistry(AttachmentRegistry):
    """Registry whose metadata reads fail."""

    async def get_attachment_with_context(
        self,
        attachment_id: str,
        *,
        acting_user_id: str | None,
    ) -> AttachmentMetadata | None:
        raise RuntimeError("Registry error")


def _respond_with(output: LLMOutput) -> RuleBasedMockLLMClient:
    def _always(_kwargs: MatcherArgs) -> bool:
        return True

    return RuleBasedMockLLMClient(rules=[(_always, output)])


def _attach_call(arguments: dict[str, object] | str) -> LLMOutput:
    return LLMOutput(
        content="",
        tool_calls=[
            ToolCallItem(
                id="call_123",
                type="function",
                function=ToolCallFunction(
                    name="attach_to_response", arguments=arguments
                ),
            )
        ],
    )


class TestSelectAttachmentsForResponse:
    """Test the _select_attachments_for_response method."""

    @pytest.fixture
    def app_config(self) -> AppConfig:
        """Create an app config with custom thresholds."""
        config = AppConfig()
        config.attachment_selection_threshold = 3
        config.max_response_attachments = 6
        return config

    @pytest.fixture
    def attachment_registry(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> AttachmentRegistry:
        return AttachmentRegistry(
            storage_path=str(tmp_path), db_engine=db_engine, config=None
        )

    def _make_service(
        self,
        llm_client: RuleBasedMockLLMClient,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry | None,
    ) -> ProcessingService:
        service_config = ProcessingServiceConfig(
            id="test_profile",
            prompts={"system": "Test system prompt"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        )

        service = ProcessingService(
            llm_client=llm_client,
            tools_provider=MockToolsProvider(),
            service_config=service_config,
            context_providers=[],
            server_url=None,
            app_config=app_config,
        )
        service.attachment_processor.attachment_registry = attachment_registry
        return service

    async def _store_attachments(
        self, registry: AttachmentRegistry, db_engine: AsyncEngine, count: int
    ) -> list[str]:
        db = Database(engine=db_engine)
        ids: list[str] = []
        for i in range(1, count + 1):
            metadata = await registry.store_and_register_tool_attachment(
                file_content=f"image {i}".encode(),
                filename=f"att{i}.jpg",
                content_type="image/jpeg",
                tool_name="test_tool",
                description=f"Test attachment {i}",
                db_context=db,
                taint_state=TurnTaintState.empty(),
            )
            ids.append(metadata.attachment_id)
        return ids

    @pytest.mark.asyncio
    async def test_select_attachments_returns_llm_selection(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
        db_engine: AsyncEngine,
    ) -> None:
        """Test that _select_attachments_for_response extracts attachment IDs from LLM tool call."""
        pending_ids = await self._store_attachments(attachment_registry, db_engine, 5)
        selected = [pending_ids[1], pending_ids[3], pending_ids[4]]
        llm_client = _respond_with(_attach_call({"attachment_ids": selected}))
        service = self._make_service(llm_client, app_config, attachment_registry)

        result = await service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Show me the most relevant images",
            acting_user_id=None,
            llm_client=service.llm_client,
        )

        assert result == selected
        calls = llm_client.get_calls()
        assert len(calls) == 1
        prompt = calls[0]["kwargs"]["messages"][0].content
        for i, att_id in enumerate(pending_ids, start=1):
            assert f"- {att_id}: Test attachment {i} (image/jpeg)" in prompt

    @pytest.mark.asyncio
    async def test_select_attachments_with_json_string_arguments(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
        db_engine: AsyncEngine,
    ) -> None:
        """Test that _select_attachments_for_response handles JSON string arguments from LLM."""
        pending_ids = await self._store_attachments(attachment_registry, db_engine, 3)
        selected = [pending_ids[0], pending_ids[2]]
        # LLM returns arguments as JSON string (common with some LLM providers)
        llm_client = _respond_with(
            _attach_call(json.dumps({"attachment_ids": selected}))
        )
        service = self._make_service(llm_client, app_config, attachment_registry)

        result = await service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
            acting_user_id=None,
            llm_client=service.llm_client,
        )

        assert result == selected

    @pytest.mark.asyncio
    async def test_select_attachments_errors_on_no_tool_call(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
        db_engine: AsyncEngine,
    ) -> None:
        """Selection fails explicitly when LLM returns no tool call."""
        pending_ids = await self._store_attachments(attachment_registry, db_engine, 4)
        llm_client = _respond_with(
            LLMOutput(
                content="I would select the most relevant attachments",
                tool_calls=None,
            )
        )
        service = self._make_service(llm_client, app_config, attachment_registry)

        with pytest.raises(AttachmentSelectionError):
            await service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
                llm_client=service.llm_client,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_errors_on_wrong_tool_name(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
        db_engine: AsyncEngine,
    ) -> None:
        """Selection fails explicitly when LLM calls a wrong tool."""
        pending_ids = await self._store_attachments(attachment_registry, db_engine, 4)
        llm_client = _respond_with(
            LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="wrong_tool_name",
                            arguments={"attachment_ids": [pending_ids[0]]},
                        ),
                    )
                ],
            )
        )
        service = self._make_service(llm_client, app_config, attachment_registry)

        with pytest.raises(AttachmentSelectionError):
            await service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
                llm_client=service.llm_client,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_respects_max_limit(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
        db_engine: AsyncEngine,
    ) -> None:
        """Test that selected attachments are truncated to max_response_attachments."""
        # max_response_attachments is 6, so we provide more than that
        pending_ids = await self._store_attachments(attachment_registry, db_engine, 10)
        # LLM selects all 10, but should be truncated to max (6)
        llm_client = _respond_with(_attach_call({"attachment_ids": pending_ids}))
        service = self._make_service(llm_client, app_config, attachment_registry)

        result = await service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
            acting_user_id=None,
            llm_client=service.llm_client,
        )

        assert len(result) == 6
        assert result == pending_ids[:6]

    @pytest.mark.asyncio
    async def test_select_attachments_raises_on_metadata_error(
        self,
        app_config: AppConfig,
        db_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """Unexpected metadata read errors are not silently swallowed."""
        registry = _FailingMetadataRegistry(
            storage_path=str(tmp_path), db_engine=db_engine, config=None
        )
        llm_client = _respond_with(_attach_call({"attachment_ids": ["att1"]}))
        service = self._make_service(llm_client, app_config, registry)

        with pytest.raises(RuntimeError, match="Registry error"):
            await service.attachment_processor.select_for_response(
                pending_attachment_ids=["att1", "att2", "att3"],
                original_query="Select images",
                acting_user_id=None,
                llm_client=service.llm_client,
            )
        assert llm_client.get_calls() == []

    @pytest.mark.asyncio
    async def test_select_attachments_raises_on_llm_error(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
        db_engine: AsyncEngine,
    ) -> None:
        """LLM failures surface as explicit selection errors."""
        pending_ids = await self._store_attachments(attachment_registry, db_engine, 5)
        llm_client = _FailingLLMClient(RuntimeError("LLM error"))
        service = self._make_service(llm_client, app_config, attachment_registry)

        with pytest.raises(
            AttachmentSelectionError,
            match="Failed to run LLM-based attachment selection",
        ):
            await service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
                llm_client=service.llm_client,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_empty_input(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that empty input list is handled gracefully."""
        llm_client = RuleBasedMockLLMClient(rules=[])
        service = self._make_service(llm_client, app_config, attachment_registry)

        result = await service.attachment_processor.select_for_response(
            pending_attachment_ids=[],
            original_query="Select images",
            acting_user_id=None,
            llm_client=service.llm_client,
        )

        assert result == []
        assert llm_client.get_calls() == []

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_select_attachments_no_registry(
        self,
        app_config: AppConfig,
    ) -> None:
        """Test that method handles missing attachment registry gracefully."""
        llm_client = RuleBasedMockLLMClient(rules=[])
        service = self._make_service(llm_client, app_config, None)

        pending_ids = ["att1", "att2", "att3"]

        result = await service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
            acting_user_id=None,
            llm_client=service.llm_client,
        )

        # Should return original list when no registry
        assert result == pending_ids
        assert llm_client.get_calls() == []

    @pytest.mark.asyncio
    async def test_select_attachments_errors_on_malformed_arguments(
        self,
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,
        db_engine: AsyncEngine,
    ) -> None:
        """Test handling of malformed tool arguments from LLM."""
        pending_ids = await self._store_attachments(attachment_registry, db_engine, 4)
        # LLM returns malformed arguments (not a dict, not a list)
        llm_client = _respond_with(_attach_call("invalid_arguments"))
        service = self._make_service(llm_client, app_config, attachment_registry)

        with pytest.raises(AttachmentSelectionError):
            await service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
                llm_client=service.llm_client,
            )
