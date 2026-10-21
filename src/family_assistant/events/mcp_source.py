"""Event source for MCP servers that offer the Events extension, by polling.

A listener on this source names what to watch in its match conditions:
``server_id``, ``event`` and the event's subscription arguments as
``arguments.<name>`` keys. Any other key filters the delivered events as on
every other source. Each distinct (server, event, arguments, creator) is one
subscription, polled with the creator's credential for that server, so a
person only ever hears what that server would show them. Subscriptions follow
the enabled listeners: creating, disabling or deleting an automation starts or
stops its subscription at the next reconcile, wherever the change was made.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import jsonschema

from family_assistant.events.mcp_client import (
    MCPEventsClient,
    MCPEventsError,
    MCPEventsNotSupportedError,
    MCPEventType,
)
from family_assistant.events.sources import BaseEventSource, EventSource
from family_assistant.events.validation import ValidationError, ValidationResult
from family_assistant.storage.events import EventSourceType
from family_assistant.tools.mcp_auth import verify_mcp_user_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from family_assistant.config_models import MCPEventSourceConfig, MCPServerConfig
    from family_assistant.events.processor import EventProcessor
    from family_assistant.storage.database import Database
    from family_assistant.storage.types import EventListenerDict, MatchConditions

logger = logging.getLogger(__name__)

SERVER_KEY = "server_id"
EVENT_KEY = "event"
ARGUMENT_PREFIX = "arguments."
_HTTP_TRANSPORTS = frozenset({"streamable_http", "streamablehttp", "http"})

type ArgumentValue = str | int | float | bool


@dataclass(frozen=True)
class MCPSubscription:
    """What one poll loop watches, and as whom."""

    server_id: str
    event_name: str
    arguments: tuple[tuple[str, ArgumentValue], ...]
    user_id: str | None

    @property
    def argument_dict(self) -> dict[str, ArgumentValue]:
        return dict(self.arguments)

    @property
    def key(self) -> str:
        canonical = json.dumps(
            [self.server_id, self.event_name, self.arguments, self.user_id],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def parse_subscription(
    match_conditions: MatchConditions, user_id: str | None
) -> MCPSubscription:
    """Read the subscription a listener's match conditions name."""
    server_id = match_conditions.get(SERVER_KEY)
    event_name = match_conditions.get(EVENT_KEY)
    if not isinstance(server_id, str) or not server_id:
        raise ValueError(f"'{SERVER_KEY}' must name a configured MCP server")
    if not isinstance(event_name, str) or not event_name:
        raise ValueError(f"'{EVENT_KEY}' must name one of the server's event types")
    arguments = tuple(
        sorted(
            (key.removeprefix(ARGUMENT_PREFIX), value)
            for key, value in match_conditions.items()
            if key.startswith(ARGUMENT_PREFIX)
        )
    )
    return MCPSubscription(server_id, event_name, arguments, user_id)


@dataclass(frozen=True)
class _Endpoint:
    url: str
    token: str | None
    verify: Callable[[], Awaitable[None]] | None


@dataclass
class _Running:
    subscription: MCPSubscription
    listener_ids: frozenset[int]
    task: asyncio.Task[None] | None = None
    cursor: str | None = None
    verified: bool = False
    failures: int = 0
    last_error: str | None = None


class MCPEventSource(BaseEventSource, EventSource):
    """Poll MCP servers' ``events/poll`` for the subscriptions listeners name."""

    def __init__(
        self,
        *,
        servers: Mapping[str, MCPServerConfig],
        database: Callable[[], Database],
        config: MCPEventSourceConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._servers = dict(servers)
        self._database = database
        self._config = config
        self._http = http_client
        self._owns_http = http_client is None
        self.processor: EventProcessor | None = None
        self._running: dict[str, _Running] = {}
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reported_invalid: set[int] = set()

    @property
    def source_id(self) -> str:
        return EventSourceType.mcp.value

    async def start(self, processor: EventProcessor) -> None:
        self.processor = processor
        if self._http is None:
            self._http = httpx.AsyncClient()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        logger.info("Started MCP event source")

    async def stop(self) -> None:
        tasks = [self._reconcile_task] + [
            running.task for running in self._running.values()
        ]
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._running.clear()
        self._reconcile_task = None
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
        self.processor = None
        logger.info("Stopped MCP event source")

    # --- Subscriptions -------------------------------------------------

    def subscriptions_for(
        self, listeners: Iterable[EventListenerDict]
    ) -> dict[MCPSubscription, frozenset[int]]:
        """Group listeners by the subscription each one names."""
        grouped: dict[MCPSubscription, set[int]] = {}
        for listener in listeners:
            try:
                subscription = parse_subscription(
                    listener["match_conditions"], listener.get("created_by_user_id")
                )
            except ValueError as exc:
                if listener["id"] not in self._reported_invalid:
                    self._reported_invalid.add(listener["id"])
                    logger.error(
                        "MCP event listener %s (%s) cannot be polled: %s",
                        listener["id"],
                        listener["name"],
                        exc,
                    )
                continue
            grouped.setdefault(subscription, set()).add(listener["id"])
        return {sub: frozenset(ids) for sub, ids in grouped.items()}

    async def reconcile(self, *, start_polling: bool = True) -> None:
        """Make the running subscriptions match the enabled listeners."""
        db = self._database()
        listeners = await db.events.get_enabled_listeners_for_source(self.source_id)
        wanted = {
            sub.key: (sub, ids)
            for sub, ids in self.subscriptions_for(listeners).items()
        }
        await db.mcp_event_cursors.delete_except(wanted.keys())
        for key in list(self._running):
            if key not in wanted:
                running = self._running.pop(key)
                if running.task is not None:
                    running.task.cancel()
                logger.info(
                    "Stopped polling MCP event %s/%s",
                    running.subscription.server_id,
                    running.subscription.event_name,
                )
        for key, (subscription, listener_ids) in wanted.items():
            running = self._running.get(key)
            if running is None:
                running = _Running(subscription, listener_ids)
                self._running[key] = running
                logger.info(
                    "Polling MCP event %s/%s for listeners %s",
                    subscription.server_id,
                    subscription.event_name,
                    sorted(listener_ids),
                )
            running.listener_ids = listener_ids
            if start_polling and (running.task is None or running.task.done()):
                running.task = asyncio.create_task(self._poll_loop(running))

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                logger.exception("Failed to reconcile MCP event subscriptions")
            await asyncio.sleep(self._config.reconcile_interval_seconds)

    async def _poll_loop(self, running: _Running) -> None:
        while True:
            try:
                delay = await self.poll_once(running)
                running.failures = 0
                running.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                running.failures += 1
                running.last_error = str(exc)
                running.verified = False
                delay = min(
                    self._config.min_poll_interval_seconds * 2**running.failures,
                    self._config.max_error_backoff_seconds,
                )
                logger.warning(
                    "Polling MCP event %s/%s failed (attempt %s, retrying in %.0fs): %s",
                    running.subscription.server_id,
                    running.subscription.event_name,
                    running.failures,
                    delay,
                    exc,
                )
            await asyncio.sleep(delay)

    async def poll_once(self, running: _Running) -> float:
        """Poll one page, deliver it, and say how long to wait before the next.

        The cursor is stored only after the page's events were processed, so
        an event is delivered at least once: a crash between the two replays
        the page.
        """
        subscription = running.subscription
        client = await self._client(running)
        db = self._database()
        if running.cursor is None:
            running.cursor = await db.mcp_event_cursors.get(subscription.key)
        result = await client.poll(
            subscription.event_name, subscription.argument_dict, running.cursor
        )
        if result.truncated:
            logger.warning(
                "MCP server %s reports missed %s events; some were not delivered",
                subscription.server_id,
                subscription.event_name,
            )
        for occurrence in result.events:
            if self.processor is None:
                raise RuntimeError("MCP event source is not started")
            await self.processor.process_event(
                self.source_id,
                {
                    SERVER_KEY: subscription.server_id,
                    EVENT_KEY: subscription.event_name,
                    "arguments": subscription.argument_dict,
                    "event_id": occurrence.event_id,
                    "timestamp": occurrence.timestamp,
                    "data": occurrence.data,
                },
                listener_ids=running.listener_ids,
            )
        if result.cursor is not None and result.cursor != running.cursor:
            await db.mcp_event_cursors.set(
                subscription_key=subscription.key,
                server_id=subscription.server_id,
                event_name=subscription.event_name,
                cursor=result.cursor,
            )
        running.cursor = result.cursor
        if result.has_more:
            return 0.0
        suggested = (
            result.next_poll_ms / 1000
            if result.next_poll_ms is not None
            else self._config.default_poll_interval_seconds
        )
        return max(suggested, self._config.min_poll_interval_seconds)

    async def _client(self, running: _Running) -> MCPEventsClient:
        endpoint = self._endpoint(
            running.subscription.server_id, running.subscription.user_id
        )
        if endpoint.verify is not None and not running.verified:
            await endpoint.verify()
            running.verified = True
        return self._make_client(endpoint)

    # --- Servers and credentials --------------------------------------

    def _make_client(self, endpoint: _Endpoint) -> MCPEventsClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
            self._owns_http = True
        return MCPEventsClient(
            url=endpoint.url,
            token=endpoint.token,
            http_client=self._http,
            timeout_seconds=self._config.request_timeout_seconds,
        )

    def _endpoint(self, server_id: str, user_id: str | None) -> _Endpoint:
        """Where to poll ``server_id`` and with which credential for ``user_id``."""
        server = self._servers.get(server_id)
        if server is None:
            raise MCPEventsError(f"No MCP server named {server_id!r} is configured")
        url = (server.model_extra or {}).get("url")
        if not is_http_mcp_server(server) or not isinstance(url, str):
            raise MCPEventsNotSupportedError(
                f"MCP server {server_id!r} is not a Streamable HTTP server; "
                "events are only polled over HTTP"
            )
        if server.user_auth is None:
            return _Endpoint(url, _resolve_token(server), None)
        if user_id is None:
            raise MCPEventsError(
                f"MCP server {server_id!r} needs the automation's creator, and it has none"
            )
        credential = server.user_auth.users.get(user_id)
        if credential is None:
            raise MCPEventsError(
                f"There is no {server_id} credential configured for this user"
            )
        token = os.getenv(credential.token_env)
        check = server.user_auth.identity_check

        async def verify() -> None:
            await verify_mcp_user_identity(url, token, credential, check)

        return _Endpoint(url, token, verify)

    def event_servers(self, user_id: str | None) -> list[str]:
        """HTTP servers this user has a credential for, which may offer events."""
        server_ids = []
        for server_id in sorted(self._servers):
            try:
                self._endpoint(server_id, user_id)
            except MCPEventsError:
                continue
            server_ids.append(server_id)
        return server_ids

    async def list_event_types(
        self, server_id: str, user_id: str | None
    ) -> list[MCPEventType]:
        """The server's event types, as seen with this user's credential."""
        endpoint = self._endpoint(server_id, user_id)
        if endpoint.verify is not None:
            await endpoint.verify()
        client = self._make_client(endpoint)
        if not await client.supports_events():
            raise MCPEventsNotSupportedError(
                f"MCP server {server_id!r} does not offer the Events extension"
            )
        return await client.list_events()

    async def validate_listener(
        self, match_conditions: MatchConditions, user_id: str | None
    ) -> ValidationResult:
        """Check, against the live server, that a listener can be polled."""
        try:
            subscription = parse_subscription(match_conditions, user_id)
        except ValueError as exc:
            return _invalid(SERVER_KEY, match_conditions.get(SERVER_KEY), str(exc))
        try:
            event_types = await self.list_event_types(subscription.server_id, user_id)
        except (MCPEventsError, ValueError) as exc:
            return _invalid(SERVER_KEY, subscription.server_id, str(exc))
        by_name = {event_type.name: event_type for event_type in event_types}
        event_type = by_name.get(subscription.event_name)
        if event_type is None:
            return ValidationResult(
                valid=False,
                errors=[
                    ValidationError(
                        field=EVENT_KEY,
                        value=subscription.event_name,
                        error=f"{subscription.server_id} has no event type by that name",
                        similar_values=sorted(by_name),
                    )
                ],
            )
        if not event_type.supports_poll:
            return _invalid(
                EVENT_KEY,
                subscription.event_name,
                f"{subscription.server_id} does not offer this event for polling "
                f"(delivery: {', '.join(event_type.delivery) or 'unspecified'})",
            )
        try:
            jsonschema.validate(subscription.argument_dict, event_type.input_schema)
        except jsonschema.ValidationError as exc:
            return _invalid("arguments", subscription.argument_dict, exc.message)
        except jsonschema.SchemaError as exc:
            logger.warning(
                "MCP server %s publishes an invalid inputSchema for %s: %s",
                subscription.server_id,
                subscription.event_name,
                exc.message,
            )
        return ValidationResult(valid=True)


def is_http_mcp_server(server: MCPServerConfig) -> bool:
    """Whether events could be polled from this server at all."""
    extra = server.model_extra or {}
    transport = str(extra.get("transport") or "stdio").lower()
    return transport in _HTTP_TRANSPORTS and isinstance(extra.get("url"), str)


def _resolve_token(server: MCPServerConfig) -> str | None:
    if server.token is None:
        return None
    token = server.token.get_secret_value()
    if token.startswith("$"):
        return os.getenv(token[1:])
    return token


def _invalid(field_name: str, value: object, error: str) -> ValidationResult:
    return ValidationResult(
        valid=False,
        errors=[ValidationError(field=field_name, value=value, error=error)],
    )
