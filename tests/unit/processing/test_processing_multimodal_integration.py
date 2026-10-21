"""
Integration tests for processing.py multimodal tool results handling.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMStreamEvent, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolAttachment, ToolResult, ToolReturnType
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

# ast-grep-ignore: no-dict-any - tool arguments from external LLM tool call
ToolHandler = Callable[[dict[str, Any]], ToolReturnType]


class _ScriptedToolsProvider:
    """Tools provider that runs a per-tool handler and records each call."""

    def __init__(self) -> None:
        self.handlers: dict[str, ToolHandler] = {}
        self.executed_tool_names: list[str] = []

    def on(self, name: str, handler: ToolHandler) -> None:
        self.handlers[name] = handler

    def returns(self, name: str, result: ToolReturnType) -> None:
        self.handlers[name] = lambda _arguments: result

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return []

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool arguments from external LLM tool call
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> ToolReturnType:
        self.executed_tool_names.append(name)
        return self.handlers[name](arguments)

    async def close(self) -> None:
        pass


# ast-grep-ignore: no-dict-any - tool arguments from external LLM tool call
def _attach_to_response(arguments: dict[str, Any]) -> str:
    attachment_ids = arguments["attachment_ids"]
    return json.dumps({
        "status": "attachments_queued",
        "attachment_ids": attachment_ids,
        "count": len(attachment_ids),
        "message": "Explicitly controlling attachments",
    })


def _tool_call(call_id: str, name: str, arguments: str) -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments=arguments),
    )


async def _store_attachment(
    registry: AttachmentRegistry, db_context: Database, description: str
) -> str:
    metadata = await registry.store_and_register_tool_attachment(
        file_content=description.encode(),
        filename=f"{description}.png",
        content_type="image/png",
        tool_name="attach_source",
        description=description,
        db_context=db_context,
        taint_state=TurnTaintState.empty(),
    )
    return metadata.attachment_id


class TestProcessingServiceMultimodal:
    """Test ProcessingService handling of multimodal tool results"""

    @pytest.fixture
    def tools_provider(self) -> _ScriptedToolsProvider:
        return _ScriptedToolsProvider()

    @pytest.fixture
    def db_context(self, db_engine: AsyncEngine) -> Database:
        return Database(engine=db_engine)

    @pytest.fixture
    def attachment_registry(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> AttachmentRegistry:
        return AttachmentRegistry(
            storage_path=str(tmp_path / "attachments"),
            db_engine=db_engine,
            config=None,
        )

    @pytest.fixture
    def processing_service(
        self, tools_provider: _ScriptedToolsProvider
    ) -> ProcessingService:
        """Create a ProcessingService for testing"""

        config = ProcessingServiceConfig(
            id="test_profile",
            prompts={"system": "Test prompt"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        )

        service = ProcessingService(
            llm_client=RuleBasedMockLLMClient(rules=[]),
            tools_provider=tools_provider,
            service_config=config,
            context_providers=[],
            server_url=None,
            app_config=AppConfig(),
        )
        return service

    @pytest.mark.asyncio
    async def test_execute_single_tool_string_result(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Test _execute_single_tool with traditional string result"""
        tool_call = _tool_call("test_call_123", "test_tool", '{"arg1": "value1"}')

        tools_provider.returns("test_tool", "Simple string result")

        result = await processing_service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_123",
            user_name="test_user",
            turn_id="turn_123",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Should return ToolExecutionResult dataclass
        event = result.stream_event
        tool_message = result.llm_message
        # Check event
        assert isinstance(event, LLMStreamEvent)
        assert event.type == "tool_result"
        assert event.tool_call_id == "test_call_123"
        assert event.tool_result == "Simple string result"

        # Check tool message
        assert tool_message.role == "tool"
        assert tool_message.tool_call_id == "test_call_123"
        assert tool_message.content == "Simple string result"
        assert tool_message.error_traceback is None
        assert tool_message.transient_attachments is None
        assert result.explicit_attachment_ids is None

    @pytest.mark.asyncio
    async def test_execute_single_tool_result_with_attachment(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Test _execute_single_tool with ToolResult containing attachment"""
        tool_call = _tool_call(
            "test_call_456", "image_generator", '{"prompt": "sunset"}'
        )

        # Create ToolResult with attachment
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake png data",
            description="Generated sunset image",
        )
        tool_result = ToolResult(
            text="Successfully generated sunset image", attachments=[attachment]
        )

        tools_provider.returns("image_generator", tool_result)

        result = await processing_service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_456",
            user_name="test_user",
            turn_id="turn_456",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message

        # Check event
        assert isinstance(event, LLMStreamEvent)
        assert event.type == "tool_result"
        assert event.tool_call_id == "test_call_456"
        assert event.tool_result == "Successfully generated sunset image"

        # Check tool message (should have _attachments for provider processing)
        assert tool_message.role == "tool"
        assert tool_message.tool_call_id == "test_call_456"
        assert tool_message.content == "Successfully generated sunset image"
        assert tool_message.error_traceback is None
        assert tool_message.transient_attachments is not None
        assert tool_message.transient_attachments == [attachment]

        # Should also have attachments metadata for history
        assert tool_message.attachments is not None
        assert len(tool_message.attachments) == 1
        attachment_meta = tool_message.attachments[0]
        assert attachment_meta["type"] == "tool_result"
        assert attachment_meta["mime_type"] == "image/png"
        assert attachment_meta["description"] == "Generated sunset image"

    @pytest.mark.asyncio
    async def test_execute_single_tool_result_without_attachment(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Test _execute_single_tool with ToolResult without attachment"""
        tool_call = _tool_call("test_call_789", "text_processor", '{"text": "hello"}')

        # Create ToolResult without attachment
        tool_result = ToolResult(text="Text processed successfully")

        tools_provider.returns("text_processor", tool_result)

        result = await processing_service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_789",
            user_name="test_user",
            turn_id="turn_789",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message

        # Should behave similar to string result when no attachment
        assert event.tool_result == "Text processed successfully"
        assert tool_message.content == "Text processed successfully"
        assert tool_message.transient_attachments is None
        assert tool_message.attachments is None

    @pytest.mark.asyncio
    async def test_execute_single_tool_invalid_json_args(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Test _execute_single_tool with invalid JSON arguments"""
        tool_call = _tool_call("test_call_error", "test_tool", "{invalid json")

        result = await processing_service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_error",
            user_name="test_user",
            turn_id="turn_error",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message

        # Should return error
        assert event.type == "tool_result"
        assert event.tool_result is not None
        assert "error" in event.tool_result.lower()
        assert "invalid arguments" in tool_message.content.lower()
        assert tool_message.error_traceback is not None
        assert tool_message.taint_metadata is not None
        assert tool_message.taint_metadata.get("max_tier") == "trusted_internal"
        assert tools_provider.executed_tool_names == []

    @pytest.mark.asyncio
    async def test_execute_single_tool_invalid_json_args_does_not_log_payload(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tool_call = _tool_call(
            "test_call_error_secret",
            "test_tool",
            '{"api_key": "SECRET-123"',  # Missing closing brace
        )
        secret_fragment = "SECRET-123"

        caplog.set_level(logging.ERROR)
        result = await processing_service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_error",
            user_name="test_user",
            turn_id="turn_error",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        tool_message = result.llm_message
        all_logs = "\n".join(record.getMessage() for record in caplog.records)

        assert secret_fragment not in all_logs
        assert secret_fragment not in (tool_message.error_traceback or "")
        assert tools_provider.executed_tool_names == []

    @pytest.mark.asyncio
    async def test_execute_single_tool_non_object_json_args(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Tool arguments must deserialize to a JSON object, not a list/scalar."""
        tool_call = _tool_call(
            "test_call_non_object_args", "test_tool", '["not", "an", "object"]'
        )

        result = await processing_service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_non_object_args",
            user_name="test_user",
            turn_id="turn_non_object_args",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message

        assert event.type == "tool_result"
        assert event.tool_result is not None
        assert "invalid arguments" in event.tool_result.lower()
        assert "Expected JSON object" in (tool_message.error_traceback or "")
        assert tool_message.taint_metadata is not None
        assert tools_provider.executed_tool_names == []

    @pytest.mark.asyncio
    async def test_execute_single_tool_execution_error(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Test _execute_single_tool when tool execution fails"""
        tool_call = _tool_call(
            "test_call_exception", "failing_tool", '{"arg": "value"}'
        )

        # ast-grep-ignore: no-dict-any - tool arguments from external LLM tool call
        def _fail(_arguments: dict[str, Any]) -> ToolReturnType:
            raise RuntimeError("Tool execution failed")

        tools_provider.on("failing_tool", _fail)

        result = await processing_service.tool_executor.execute(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_exception",
            user_name="test_user",
            turn_id="turn_exception",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message

        # Should handle error gracefully
        assert event.type == "tool_result"
        assert event.tool_result is not None
        assert "error" in event.tool_result.lower()
        assert "tool execution failed" in tool_message.content.lower()
        assert tool_message.error_traceback is not None

    async def test_automatic_attachment_queuing(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
        db_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """Test that tool result attachments are automatically queued for display"""
        tool_call = _tool_call(
            "test_call_auto", "mock_camera_snapshot", '{"entity_id": "camera.test"}'
        )

        # Create ToolResult with attachment
        attachment = ToolAttachment(
            mime_type="image/jpeg",
            content=b"fake jpeg data",
            description="Test camera image",
        )
        tool_result = ToolResult(text="Captured camera image", attachments=[attachment])

        attachment_registry = AttachmentRegistry(
            storage_path=str(tmp_path / "attachments"),
            db_engine=db_engine,
            config=None,
        )
        processing_service.tool_executor.attachment_registry = attachment_registry
        tools_provider.returns("mock_camera_snapshot", tool_result)

        # Execute single tool
        result = await processing_service.tool_executor.execute(
            tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Verify that the attachment ID is returned for auto-queuing
        assert result.auto_attachment_ids is not None
        assert len(result.auto_attachment_ids) == 1
        stored_attachment_id = result.auto_attachment_ids[0]

        # Verify the attachment was actually stored and registered, not just
        # that some mocked method was called with the right arguments.
        db_context = Database(engine=db_engine)
        stored_metadata = await attachment_registry.get_attachment(
            db_context, stored_attachment_id, acting_user_id=None
        )
        assert stored_metadata is not None
        assert stored_metadata.mime_type == "image/jpeg"
        assert stored_metadata.source_id == "mock_camera_snapshot"
        stored_content = await attachment_registry.get_attachment_content(
            db_context, stored_attachment_id, acting_user_id=None
        )
        assert stored_content == b"fake jpeg data"

        # Verify that the attachment ID is injected into the LLM message content
        llm_message = result.llm_message
        assert f"[Attachment ID(s): {stored_attachment_id}]" in llm_message.content

        # Verify that the ToolAttachment object has the attachment_id populated
        assert (
            llm_message.transient_attachments is not None
            and llm_message.transient_attachments[0].attachment_id
            == stored_attachment_id
        )

    async def test_no_auto_attachment_for_string_results(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Test that string tool results don't generate auto-attachment IDs"""
        tool_call = _tool_call("test_call_string", "simple_tool", '{"text": "hello"}')

        tools_provider.returns("simple_tool", "Simple text result")

        # Execute single tool
        result = await processing_service.tool_executor.execute(
            tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Verify no attachment ID is returned
        assert not result.auto_attachment_ids or len(result.auto_attachment_ids) == 0

    async def test_no_auto_attachment_without_attachment_registry(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
    ) -> None:
        """Test that ToolResult attachments don't auto-queue without attachment registry"""
        tool_call = _tool_call("test_call_no_service", "image_tool", "{}")

        # Create ToolResult with attachment
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake png data",
            description="Test image",
        )
        tool_result = ToolResult(text="Generated image", attachments=[attachment])

        # No attachment registry configured
        processing_service.tool_executor.attachment_registry = None

        tools_provider.returns("image_tool", tool_result)

        # Execute single tool
        result = await processing_service.tool_executor.execute(
            tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Should not have auto-attachment ID without attachment registry
        assert not result.auto_attachment_ids or len(result.auto_attachment_ids) == 0

    async def test_attach_to_response_overrides_auto_attachments(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that LLM calling attach_to_response replaces auto-queued attachments"""
        # This test simulates the full tool loop behavior where:
        # 1. First tool generates attachment -> auto-queued
        # 2. LLM calls attach_to_response -> replaces auto-queued with explicit list
        processing_service.tool_executor.attachment_registry = attachment_registry
        explicit_id = await _store_attachment(
            attachment_registry, db_context, "Explicit attachment"
        )

        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake png data",
            description="Generated sunset image",
        )
        tools_provider.returns(
            "generate_image",
            ToolResult(text="Generated image", attachments=[attachment]),
        )
        tools_provider.on("attach_to_response", _attach_to_response)

        # Execute first tool (image generation) - should auto-queue
        first_result = await processing_service.tool_executor.execute(
            _tool_call("call_image_gen", "generate_image", '{"prompt": "sunset"}'),
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        assert first_result.auto_attachment_ids is not None
        assert len(first_result.auto_attachment_ids) == 1
        generated_id = first_result.auto_attachment_ids[0]
        assert generated_id != explicit_id
        generated = await attachment_registry.get_attachment(
            db_context, generated_id, acting_user_id=None
        )
        assert generated is not None
        assert generated.source_id == "generate_image"

        second_result = await processing_service.tool_executor.execute(
            _tool_call(
                "call_attach_response",
                "attach_to_response",
                json.dumps({"attachment_ids": [explicit_id]}),
            ),
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # attach_to_response doesn't generate auto-attachments
        assert (
            not second_result.auto_attachment_ids
            or len(second_result.auto_attachment_ids) == 0
        )
        assert second_result.explicit_attachment_ids == [explicit_id]
        # But it should return the JSON response for the processing loop to handle
        assert second_result.stream_event.tool_result is not None
        assert "attachments_queued" in second_result.stream_event.tool_result

        # The processing loop merges results into a pending attachment list in
        # order; the explicit call must replace the auto-queued attachment
        # rather than append to it.
        pending_attachment_ids: list[str] = []
        first_result.apply_attachment_updates(pending_attachment_ids)
        second_result.apply_attachment_updates(pending_attachment_ids)
        assert pending_attachment_ids == [explicit_id]

    async def test_multiple_attach_to_response_calls_behavior(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test behavior when attach_to_response is called multiple times"""
        # This tests the edge case where LLM calls attach_to_response multiple times
        # The last call should win (replace previous explicit attachments)
        processing_service.tool_executor.attachment_registry = attachment_registry
        first_id = await _store_attachment(
            attachment_registry, db_context, "first_attachment"
        )
        second_a = await _store_attachment(
            attachment_registry, db_context, "second_attachment_a"
        )
        second_b = await _store_attachment(
            attachment_registry, db_context, "second_attachment_b"
        )
        tools_provider.on("attach_to_response", _attach_to_response)

        first_result = await processing_service.tool_executor.execute(
            _tool_call(
                "call_attach_1",
                "attach_to_response",
                json.dumps({"attachment_ids": [first_id]}),
            ),
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        second_result = await processing_service.tool_executor.execute(
            _tool_call(
                "call_attach_2",
                "attach_to_response",
                json.dumps({"attachment_ids": [second_a, second_b]}),
            ),
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Both should return proper JSON responses
        assert first_result.stream_event.tool_result is not None
        assert first_id in first_result.stream_event.tool_result
        assert first_result.explicit_attachment_ids == [first_id]
        assert second_result.stream_event.tool_result is not None
        assert second_a in second_result.stream_event.tool_result
        assert second_b in second_result.stream_event.tool_result
        assert second_result.explicit_attachment_ids == [second_a, second_b]

        # Neither generates auto-attachments (attach_to_response is explicit control)
        assert (
            not first_result.auto_attachment_ids
            or len(first_result.auto_attachment_ids) == 0
        )
        assert (
            not second_result.auto_attachment_ids
            or len(second_result.auto_attachment_ids) == 0
        )

        # The second explicit call must fully replace the first, not append.
        pending_attachment_ids: list[str] = []
        first_result.apply_attachment_updates(pending_attachment_ids)
        second_result.apply_attachment_updates(pending_attachment_ids)
        assert pending_attachment_ids == [second_a, second_b]

    async def test_new_attachments_after_attach_to_response(
        self,
        processing_service: ProcessingService,
        tools_provider: _ScriptedToolsProvider,
        db_context: Database,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test behavior when new tool attachments appear after attach_to_response is called"""
        # This tests the scenario:
        # 1. LLM calls attach_to_response (explicit control)
        # 2. Later tool generates new attachment
        # Question: Should the new attachment be auto-queued or ignored?
        # Answer: It should be auto-queued! Each attachment is independent.
        processing_service.tool_executor.attachment_registry = attachment_registry
        explicit_id = await _store_attachment(
            attachment_registry, db_context, "Explicit attachment"
        )
        tools_provider.on("attach_to_response", _attach_to_response)
        tools_provider.returns(
            "generate_new_image",
            ToolResult(
                text="Generated new image",
                attachments=[
                    ToolAttachment(
                        mime_type="image/jpeg",
                        content=b"new image data",
                        description="New generated image",
                    )
                ],
            ),
        )

        attach_result = await processing_service.tool_executor.execute(
            _tool_call(
                "call_explicit",
                "attach_to_response",
                json.dumps({"attachment_ids": [explicit_id]}),
            ),
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        new_tool_result = await processing_service.tool_executor.execute(
            _tool_call("call_new_image", "generate_new_image", "{}"),
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Attach call should not generate auto-attachment
        assert (
            not attach_result.auto_attachment_ids
            or len(attach_result.auto_attachment_ids) == 0
        )
        assert attach_result.explicit_attachment_ids == [explicit_id]

        # But new tool should still auto-queue its attachment
        # (Each tool execution is independent)
        assert new_tool_result.auto_attachment_ids is not None
        assert len(new_tool_result.auto_attachment_ids) == 1
        new_id = new_tool_result.auto_attachment_ids[0]
        new_attachment = await attachment_registry.get_attachment(
            db_context, new_id, acting_user_id=None
        )
        assert new_attachment is not None
        assert new_attachment.source_id == "generate_new_image"

        # A later tool's auto-queued attachment is appended to the explicit
        # selection rather than being ignored or replacing it.
        pending_attachment_ids: list[str] = []
        attach_result.apply_attachment_updates(pending_attachment_ids)
        new_tool_result.apply_attachment_updates(pending_attachment_ids)
        assert pending_attachment_ids == [explicit_id, new_id]
