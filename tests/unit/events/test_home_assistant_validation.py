"""Tests for Home Assistant event source validation."""

from collections.abc import Iterable, Sequence
from datetime import datetime

import homeassistant_api as ha_api
import pytest

from family_assistant.plugins.home_assistant.client import HomeAssistantClientWrapper
from family_assistant.plugins.home_assistant.events import HomeAssistantSource

KNOWN_ENTITY_IDS = (
    "person.alex_smith",
    "person.taylor_smith",
    "light.living_room",
    "light.bedroom",
    "switch.garage",
    "sensor.temperature",
    "binary_sensor.motion_detected",
)


def _state(entity_id: str, state: str) -> ha_api.State:
    return ha_api.State.model_validate({"entity_id": entity_id, "state": state})


class ScriptedHomeAssistantClient(HomeAssistantClientWrapper):
    """A real wrapper whose state and history reads answer from scripted data.

    The underlying SDK client is real but never contacted: the two reads the
    event source makes are answered here, or fail with the scripted error.
    """

    def __init__(self, entity_ids: Iterable[str]) -> None:
        super().__init__(
            api_url="http://localhost:8123",
            token="test_token",
            client=ha_api.Client(
                api_url="http://localhost:8123/api",
                token="test_token",
            ),
        )
        self.states = tuple(_state(entity_id, "on") for entity_id in entity_ids)
        self.states_error: Exception | None = None
        self.histories: list[ha_api.History] = []
        self.histories_error: Exception | None = None

    async def async_get_states(self) -> tuple[ha_api.State, ...]:
        if self.states_error is not None:
            raise self.states_error
        return self.states

    async def async_get_entity_histories(
        self,
        entity_ids: Sequence[str],
        *,
        start_timestamp: datetime,
        end_timestamp: datetime,
    ) -> list[ha_api.History]:
        if self.histories_error is not None:
            raise self.histories_error
        return [
            history
            for history in self.histories
            if history.states and history.states[0].entity_id in entity_ids
        ]


def _ha_client(entity_ids: Iterable[str]) -> ScriptedHomeAssistantClient:
    return ScriptedHomeAssistantClient(entity_ids)


def _history(entity_id: str, *states: str) -> ha_api.History:
    return ha_api.History(states=tuple(_state(entity_id, s) for s in states))


class TestHomeAssistantValidation:
    """Test Home Assistant event source validation."""

    @pytest.fixture
    def ha_source(self) -> HomeAssistantSource:
        return HomeAssistantSource(_ha_client(KNOWN_ENTITY_IDS))

    @pytest.mark.asyncio
    async def test_valid_entity_id(self, ha_source: HomeAssistantSource) -> None:
        """Test validation with a valid entity ID that exists."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "person.alex_smith"
        })
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_shortened_person_entity(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test validation catches common mistake of shortened person entity."""
        result = await ha_source.validate_match_conditions({"entity_id": "person.alex"})
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.field == "entity_id"
        assert "not found" in error.error
        assert error.suggestion == "Did you mean 'person.alex_smith'?"
        assert error.similar_values is not None
        assert "person.alex_smith" in error.similar_values

    @pytest.mark.parametrize(
        "entity_id",
        [
            "invalid-entity",
            "invalid_entity",
            "invalid entity",
            ".entity",
            "domain.",
            "domain..entity",
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_entity_format(
        self, ha_source: HomeAssistantSource, entity_id: str
    ) -> None:
        """Test validation catches invalid entity ID format."""
        result = await ha_source.validate_match_conditions({"entity_id": entity_id})
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.field == "entity_id"
        assert error.error == "Invalid entity ID format. Expected: domain.object_id"
        assert error.suggestion is not None
        assert "person.alex_smith" in error.suggestion

    @pytest.mark.asyncio
    async def test_uppercase_entity_rejected_by_api(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test that uppercase entities pass format check but fail API check."""
        # Since regex is permissive, uppercase passes format but fails API
        result = await ha_source.validate_match_conditions({
            "entity_id": "Invalid.Entity"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.field == "entity_id"
        assert "not found" in error.error  # API check, not format check

    @pytest.mark.asyncio
    async def test_non_string_entity_id(self, ha_source: HomeAssistantSource) -> None:
        """Test validation catches non-string entity IDs."""
        test_cases = [
            123,
            12.34,
            True,
            None,
            ["person.alex"],
            {"entity": "person.alex"},
        ]

        for entity_id in test_cases:
            result = await ha_source.validate_match_conditions({"entity_id": entity_id})
            assert result.valid is False
            assert len(result.errors) == 1
            error = result.errors[0]
            assert error.field == "entity_id"
            assert "must be a string" in error.error
            assert type(entity_id).__name__ in error.error

    @pytest.mark.asyncio
    async def test_nonexistent_entity(self, ha_source: HomeAssistantSource) -> None:
        """Test validation catches entity that doesn't exist."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "person.unknown"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.field == "entity_id"
        assert "not found" in error.error
        assert error.suggestion is None
        assert error.similar_values == ["person.alex_smith", "person.taylor_smith"]

    @pytest.mark.asyncio
    async def test_no_entity_id_field(self, ha_source: HomeAssistantSource) -> None:
        """Test validation passes when no entity_id field is present."""
        result = await ha_source.validate_match_conditions({
            "some_other_field": "value",
            "another_field": 123,
        })
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_api_error_becomes_warning(self) -> None:
        """Test that API errors become warnings, not validation failures."""
        client = _ha_client(KNOWN_ENTITY_IDS)
        client.states_error = ConnectionError("API connection failed")
        source = HomeAssistantSource(client)

        result = await source.validate_match_conditions({
            "entity_id": "person.alex_smith"
        })
        # Should still be valid since we can't verify
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 1
        assert "Could not verify entity existence" in result.warnings[0]
        assert "API connection failed" in result.warnings[0]

    @pytest.mark.asyncio
    async def test_multiple_match_conditions(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test validation with multiple match conditions."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "light.living_room",
            "event_type": "state_changed",
            "some_other_condition": "value",
        })
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_taylor_entity_suggestion(
        self, ha_source: HomeAssistantSource
    ) -> None:
        """Test validation provides suggestion for Taylor entity."""
        result = await ha_source.validate_match_conditions({
            "entity_id": "person.taylor"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.suggestion == "Did you mean 'person.taylor_smith'?"

    @pytest.mark.asyncio
    async def test_similar_values_limit(self) -> None:
        """Test that similar values are limited to 5 entities of the same domain."""
        source = HomeAssistantSource(
            _ha_client([
                *KNOWN_ENTITY_IDS,
                *(f"light.room_{i}" for i in range(10)),
            ])
        )

        result = await source.validate_match_conditions({
            "entity_id": "light.nonexistent"
        })
        assert result.valid is False
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.similar_values is not None
        assert len(error.similar_values) == 5
        assert all(value.startswith("light.") for value in error.similar_values)

    @pytest.mark.asyncio
    async def test_state_validation_with_valid_state(self) -> None:
        """Test validation when entity has been in the specified state."""
        client = _ha_client(["person.test"])
        client.histories = [_history("person.test", "home")]
        source = HomeAssistantSource(client)

        result = await source.validate_match_conditions({
            "entity_id": "person.test",
            "new_state.state": "home",
        })

        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_state_validation_with_unknown_state(self) -> None:
        """Test validation when entity has never been in the specified state."""
        client = _ha_client(["person.test"])
        client.histories = [_history("person.test", "home", "away")]
        source = HomeAssistantSource(client)

        result = await source.validate_match_conditions({
            "entity_id": "person.test",
            "new_state.state": "vacation",
        })

        assert result.valid is True  # Still valid, just warnings
        assert len(result.errors) == 0
        assert len(result.warnings) == 2
        assert "never been recorded" in result.warnings[0]
        assert "vacation" in result.warnings[0]
        assert "Most common states for 'person.test':" in result.warnings[1]
        assert "home" in result.warnings[1]
        assert "away" in result.warnings[1]

    @pytest.mark.asyncio
    async def test_state_validation_no_history(self) -> None:
        """Test validation when no history is available for entity."""
        client = _ha_client(["person.test"])
        client.histories = []
        source = HomeAssistantSource(client)

        result = await source.validate_match_conditions({
            "entity_id": "person.test",
            "old_state.state": "home",
        })

        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 1
        assert "No history found" in result.warnings[0]

    @pytest.mark.asyncio
    async def test_state_validation_api_error(self) -> None:
        """Test that history API errors return warnings."""
        client = _ha_client(["person.test"])
        client.histories_error = ConnectionError("History API Error")
        source = HomeAssistantSource(client)

        result = await source.validate_match_conditions({
            "entity_id": "person.test",
            "new_state.state": "home",
        })

        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 1
        assert "Could not verify state history" in result.warnings[0]
