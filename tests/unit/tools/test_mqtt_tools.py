"""Tests for MQTT publish tool."""

from collections.abc import Iterator
from types import TracebackType
from typing import Any, Self, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import aiomqtt
import pytest
from pydantic import SecretStr

from family_assistant.config_models import AppConfig, MQTTConfig
from family_assistant.tools.argument_schema import argument_schema_errors
from family_assistant.tools.catalog import TOOLS_DEFINITION
from family_assistant.tools.mqtt import mqtt_publish_tool
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.idle_processing_service import make_idle_processing_service
from tests.mocks.unconnected_database import unconnected_database


class FakeMqttClient:
    """Stands in for ``aiomqtt.Client``, recording connections and publishes."""

    instances: list["FakeMqttClient"] = []
    connect_error: BaseException | None = None

    def __init__(
        self,
        *,
        hostname: str,
        port: int,
        username: str | None,
        password: str | None,
    ) -> None:
        self.connection = {
            "hostname": hostname,
            "port": port,
            "username": username,
            "password": password,
        }
        self.published: list[tuple[str, bytes, bool]] = []
        FakeMqttClient.instances.append(self)

    async def __aenter__(self) -> Self:
        if FakeMqttClient.connect_error is not None:
            raise FakeMqttClient.connect_error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def publish(self, topic: str, *, payload: bytes, retain: bool) -> None:
        self.published.append((topic, payload, retain))


@pytest.fixture
def fake_mqtt() -> Iterator[type[FakeMqttClient]]:
    FakeMqttClient.instances = []
    FakeMqttClient.connect_error = None
    with patch("family_assistant.tools.mqtt.aiomqtt.Client", new=FakeMqttClient):
        yield FakeMqttClient


def _make_exec_context(mqtt_config: MQTTConfig) -> ToolExecutionContext:
    return ToolExecutionContext(
        conversation_id="test-conv",
        user_name="test-user",
        interface_type="test",
        timezone=ZoneInfo("UTC"),
        turn_id=None,
        db_context=unconnected_database(),
        processing_service=make_idle_processing_service(
            app_config=AppConfig(mqtt_config=mqtt_config)
        ),
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.fixture
def exec_context() -> ToolExecutionContext:
    return _make_exec_context(
        MQTTConfig(
            broker_host="mqtt.local",
            broker_port=1883,
            username="testuser",
            password=SecretStr("testpass"),
        )
    )


@pytest.fixture
def exec_context_no_mqtt() -> ToolExecutionContext:
    return _make_exec_context(MQTTConfig())


def test_mqtt_publish_advertised_argument_schema() -> None:
    definitions = [
        tool for tool in TOOLS_DEFINITION if tool["function"]["name"] == "mqtt_publish"
    ]
    assert len(definitions) == 1
    params = cast("dict[str, Any]", definitions[0]["function"]["parameters"])

    assert (
        argument_schema_errors(
            params, {"topic": "home/status", "payload": {"online": True}}
        )
        == []
    )
    assert (
        argument_schema_errors(
            params, {"topic": "home/status", "payload": "ON", "retain": False}
        )
        == []
    )
    assert argument_schema_errors(params, {"payload": "ON"})
    assert argument_schema_errors(params, {"topic": "home/status"})
    assert argument_schema_errors(params, {"topic": 42, "payload": "ON"})
    assert argument_schema_errors(
        params, {"topic": "home/status", "payload": "ON", "retain": "false"}
    )


@pytest.mark.asyncio
async def test_mqtt_publish(
    exec_context: ToolExecutionContext, fake_mqtt: type[FakeMqttClient]
) -> None:
    result = await mqtt_publish_tool(
        exec_context=exec_context,
        topic="family/eink/display",
        payload={"temperature": 22, "tasks": ["groceries"]},
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["topic"] == "family/eink/display"
    assert data["retain"] is True
    assert data["payload_size"] > 0

    assert len(fake_mqtt.instances) == 1
    client = fake_mqtt.instances[0]
    assert client.connection == {
        "hostname": "mqtt.local",
        "port": 1883,
        "username": "testuser",
        "password": "testpass",
    }
    assert client.published == [
        (
            "family/eink/display",
            b'{"temperature": 22, "tasks": ["groceries"]}',
            True,
        )
    ]


@pytest.mark.asyncio
async def test_mqtt_publish_no_retain(
    exec_context: ToolExecutionContext, fake_mqtt: type[FakeMqttClient]
) -> None:
    result = await mqtt_publish_tool(
        exec_context=exec_context,
        topic="test/topic",
        payload={"key": "value"},
        retain=False,
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["retain"] is False

    [(_, _, retain)] = fake_mqtt.instances[0].published
    assert retain is False


@pytest.mark.asyncio
async def test_mqtt_publish_string_payload(
    exec_context: ToolExecutionContext, fake_mqtt: type[FakeMqttClient]
) -> None:
    result = await mqtt_publish_tool(
        exec_context=exec_context,
        topic="home/switch/command",
        payload="ON",
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["payload_size"] == 2  # "ON" without JSON quotes

    [(_, payload, _)] = fake_mqtt.instances[0].published
    assert payload == b"ON"


@pytest.mark.asyncio
async def test_mqtt_publish_list_payload(
    exec_context: ToolExecutionContext, fake_mqtt: type[FakeMqttClient]
) -> None:
    await mqtt_publish_tool(
        exec_context=exec_context,
        topic="home/items",
        payload=["milk", "eggs"],
    )

    [(_, payload, _)] = fake_mqtt.instances[0].published
    assert payload == b'["milk", "eggs"]'


@pytest.mark.asyncio
async def test_mqtt_publish_not_configured(
    exec_context_no_mqtt: ToolExecutionContext,
) -> None:
    with pytest.raises(ValueError, match="MQTT broker not configured"):
        await mqtt_publish_tool(
            exec_context=exec_context_no_mqtt,
            topic="test/topic",
            payload={"key": "value"},
        )


@pytest.mark.asyncio
async def test_mqtt_publish_no_processing_service() -> None:
    context = ToolExecutionContext(
        conversation_id="test-conv",
        user_name="test-user",
        interface_type="test",
        timezone=ZoneInfo("UTC"),
        turn_id=None,
        db_context=unconnected_database(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
    )
    with pytest.raises(ValueError, match="MQTT broker not configured"):
        await mqtt_publish_tool(
            exec_context=context,
            topic="test/topic",
            payload={"key": "value"},
        )


@pytest.mark.asyncio
async def test_mqtt_publish_broker_connection_error(
    exec_context: ToolExecutionContext, fake_mqtt: type[FakeMqttClient]
) -> None:
    fake_mqtt.connect_error = aiomqtt.MqttError("Connection refused")

    with pytest.raises(ValueError, match="MQTT error publishing to"):
        await mqtt_publish_tool(
            exec_context=exec_context,
            topic="test/topic",
            payload={"key": "value"},
        )


@pytest.mark.asyncio
async def test_mqtt_publish_timeout(
    exec_context: ToolExecutionContext, fake_mqtt: type[FakeMqttClient]
) -> None:
    fake_mqtt.connect_error = TimeoutError()

    with pytest.raises(ValueError, match="Timed out connecting to MQTT broker"):
        await mqtt_publish_tool(
            exec_context=exec_context,
            topic="test/topic",
            payload={"key": "value"},
        )
