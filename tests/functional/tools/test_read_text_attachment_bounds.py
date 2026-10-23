"""read_text_attachment stays bounded on content that is one long line.

Large tool results are often compact JSON, so the stored attachment is a
single line: without splitting it, a grep returned the whole file and the line
limit bounded nothing, which is how a voice call read 132 KB back in.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools.attachments import (
    DEFAULT_READ_MAX_CHARS,
    LONG_LINE_CHARS,
    read_text_attachment_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

ENTITY_COUNT = 1_000
COMPACT_ENTITIES = json.dumps({
    "entities": [
        {"entity_id": f"sensor.reading_{i}", "name": f"Reading {i}"}
        for i in range(ENTITY_COUNT)
    ]
    + [{"entity_id": "sensor.pool_temperature", "name": "Pool"}]
})


async def _stored(
    db_engine: AsyncEngine, tmp_path: Path, content: str, content_type: str
) -> tuple[ToolExecutionContext, str]:
    storage_path = tmp_path / "attachments"
    storage_path.mkdir()
    registry = AttachmentRegistry(
        storage_path=str(storage_path), db_engine=db_engine, config=None
    )
    metadata = await registry.store_and_register_tool_attachment(
        file_content=content.encode("utf-8"),
        filename="result",
        content_type=content_type,
        tool_name="test",
        description="test",
        conversation_id="conv_1",
        taint_state=TurnTaintState.empty(),
    )
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conv_1",
        user_name="test",
        turn_id=None,
        db_context=Database(engine=db_engine),
        processing_service=None,
        clock=SystemClock(),
        plugins=None,
        event_sources=None,
        attachment_registry=registry,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )
    return context, metadata.attachment_id


@pytest.mark.asyncio
async def test_grep_on_a_single_line_returns_only_the_matching_piece(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    context, attachment_id = await _stored(
        db_engine, tmp_path, COMPACT_ENTITIES, "application/json"
    )

    result = await read_text_attachment_tool(context, attachment_id, grep="pool")

    assert result.text is not None
    assert "sensor.pool_temperature" in result.text
    assert len(result.text) < 2 * LONG_LINE_CHARS + 500


@pytest.mark.asyncio
async def test_a_read_stops_at_max_chars_and_says_where_to_continue(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    context, attachment_id = await _stored(
        db_engine, tmp_path, COMPACT_ENTITIES, "application/json"
    )

    result = await read_text_attachment_tool(context, attachment_id)

    assert result.text is not None
    assert len(result.text) < DEFAULT_READ_MAX_CHARS + 1_000
    assert "Continue with offset=" in result.text


@pytest.mark.asyncio
async def test_a_json_attachment_points_at_jq_query(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    context, attachment_id = await _stored(
        db_engine, tmp_path, COMPACT_ENTITIES, "application/json"
    )

    result = await read_text_attachment_tool(context, attachment_id, limit=1)

    assert result.text is not None
    assert f"jq_query(attachment_id='{attachment_id}'" in result.text


@pytest.mark.asyncio
async def test_a_short_text_file_reads_whole_with_no_notes(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    context, attachment_id = await _stored(
        db_engine, tmp_path, "Line 1\nLine 2\nLine 3", "text/plain"
    )

    result = await read_text_attachment_tool(context, attachment_id)

    assert result.text is not None
    assert result.text.endswith("Line 1\nLine 2\nLine 3")
