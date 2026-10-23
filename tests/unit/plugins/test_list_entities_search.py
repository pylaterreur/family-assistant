"""list_home_assistant_entities: finding an entity and reading its state in one call."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.home_assistant.tools import (
    find_entities,
    list_home_assistant_entities_tool,
)
from family_assistant.plugins.runtime import ProfilePlugins
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.home_assistant_client import (
    SpeccedHomeAssistant,
    make_home_assistant_client,
    serve_entities,
)

if TYPE_CHECKING:
    from homeassistant_api.utils import JSONType

    from family_assistant.plugins.home_assistant.client import EntityMetadata

pytestmark = pytest.mark.no_db


def _entity(
    entity_id: str,
    name: str,
    *,
    area: str | None = None,
    device: str | None = None,
    device_class: str | None = None,
) -> dict[str, JSONType]:
    return {
        "entity_id": entity_id,
        "name": name,
        "area_name": area,
        "device_id": None,
        "device_name": device,
        "device_class": device_class,
    }


ENTITIES = [
    _entity("sensor.pool_pump_run_time", "Pool pump run time"),
    _entity("switch.pool_heater_switch", "Pool Heater Switch1", area="Pool"),
    _entity(
        "sensor.pool_heater_temp_inflow",
        "Pool Heater Temp inflow",
        area="Pool",
        device="Pool Heater",
        device_class="temperature",
    ),
    _entity(
        "sensor.living_room_temperature",
        "Living Room Temperature",
        area="Living Room",
        device_class="temperature",
    ),
    _entity("light.kitchen", "Kitchen Light", area="Kitchen"),
]
STATES = {"sensor.pool_heater_temp_inflow": ("24.5", "°C")}


def _context(home_assistant: SpeccedHomeAssistant) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="list-entities-test",
        user_name="Tester",
        turn_id=None,
        db_context=Database(
            create_engine_with_sqlite_optimizations("sqlite+aiosqlite:///:memory:")
        ),
        processing_service=None,
        clock=None,
        plugins=ProfilePlugins((HomeAssistantInstance(home_assistant.wrapper),)),
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )


def _served() -> SpeccedHomeAssistant:
    home_assistant = make_home_assistant_client()
    serve_entities(home_assistant.api, ENTITIES, STATES)
    return home_assistant


def _ids(entities: list[EntityMetadata]) -> list[str]:
    return [entity["entity_id"] for entity in entities]


def test_query_ranks_entities_matching_more_words_first() -> None:
    """'temperature' reaches 'Temp' by prefix and by the device class."""
    found = find_entities(
        cast("list[EntityMetadata]", ENTITIES),
        query_words=["pool", "temperature"],
        entity_id_filter=None,
        area_filter=None,
    )

    assert _ids(found)[0] == "sensor.pool_heater_temp_inflow"
    assert "light.kitchen" not in _ids(found)


def test_query_finds_an_entity_with_no_area() -> None:
    """The real pool sensor has no area, so an area filter can never reach it."""
    entities = [
        *ENTITIES,
        _entity(
            "sensor.pool_sensor_temperature",
            "Pool Sensor Temperature",
            device_class="temperature",
        ),
    ]

    found = find_entities(
        cast("list[EntityMetadata]", entities),
        query_words=["pool", "temperature"],
        entity_id_filter=None,
        area_filter=None,
    )

    assert "sensor.pool_sensor_temperature" in _ids(found)[:2]


def test_query_drops_entities_matching_no_word() -> None:
    found = find_entities(
        cast("list[EntityMetadata]", ENTITIES),
        query_words=["garage"],
        entity_id_filter=None,
        area_filter=None,
    )

    assert found == []


def test_short_words_match_only_exactly() -> None:
    """A two-letter prefix would match nearly every entity."""
    found = find_entities(
        cast("list[EntityMetadata]", ENTITIES),
        query_words=["po"],
        entity_id_filter=None,
        area_filter=None,
    )

    assert found == []


def test_filters_and_query_must_all_hold() -> None:
    found = find_entities(
        cast("list[EntityMetadata]", ENTITIES),
        query_words=["temperature"],
        entity_id_filter=None,
        area_filter="living",
    )

    assert _ids(found) == ["sensor.living_room_temperature"]


@pytest.mark.asyncio
async def test_result_carries_the_current_state_and_unit() -> None:
    result = await list_home_assistant_entities_tool(
        _context(_served()), query="pool temperature", max_results=1
    )

    assert result.text is not None
    assert "sensor.pool_heater_temp_inflow" in result.text
    assert ": 24.5 °C" in result.text
    data = cast("dict[str, Any]", result.data)
    assert data["entities"][0]["state"] == "24.5"
    assert data["total_matches"] > 1


@pytest.mark.asyncio
async def test_unfiltered_call_is_refused_without_reading_home_assistant() -> None:
    home_assistant = _served()

    result = await list_home_assistant_entities_tool(_context(home_assistant))

    assert result.text is not None
    assert result.text.startswith("Error: give a query")
    home_assistant.api.async_get_rendered_template.assert_not_called()


@pytest.mark.asyncio
async def test_states_are_read_only_for_the_entities_returned() -> None:
    home_assistant = _served()

    await list_home_assistant_entities_tool(
        _context(home_assistant), query="pool", max_results=2
    )

    state_template = home_assistant.api.async_get_rendered_template.call_args_list[
        -1
    ].kwargs["template"]
    assert state_template.count("sensor.") + state_template.count("switch.") == 2
