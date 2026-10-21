"""A minimal client for the MCP Events extension (SEP-3415) over protocol 2026-07-28.

The pinned ``mcp`` SDK predates the stateless 2026-07-28 revision, and polling
needs nothing it would provide: each request is one self-contained JSON-RPC
POST that carries its protocol version, so there is no session to hold.
"""

from __future__ import annotations

import itertools
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2026-07-28"
EVENTS_EXTENSION = "io.modelcontextprotocol/events"
_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
_MAX_EVENTS_PER_POLL = 50


class MCPEventsError(Exception):
    """An events request failed: transport, HTTP status, or JSON-RPC error."""


class MCPEventsRPCError(MCPEventsError):
    """The server answered, with a JSON-RPC error."""


class MCPEventsNotSupportedError(MCPEventsError):
    """The server does not offer the Events extension or poll delivery."""


@dataclass(frozen=True)
class MCPEventType:
    """One entry of ``events/list``."""

    name: str
    description: str
    delivery: tuple[str, ...]
    # ast-grep-ignore: no-dict-any - JSON Schema is arbitrary JSON
    input_schema: dict[str, Any]
    # ast-grep-ignore: no-dict-any - JSON Schema is arbitrary JSON
    payload_schema: dict[str, Any] | None

    @property
    def supports_poll(self) -> bool:
        return "poll" in self.delivery


@dataclass(frozen=True)
class MCPEventOccurrence:
    """One event delivered by ``events/poll``."""

    event_id: str
    name: str
    timestamp: str | None
    # ast-grep-ignore: no-dict-any - payloads follow each server's payloadSchema
    data: dict[str, Any]


@dataclass(frozen=True)
class MCPPollResult:
    """One page of ``events/poll``."""

    events: list[MCPEventOccurrence]
    cursor: str | None
    truncated: bool
    has_more: bool
    next_poll_ms: int | None


class MCPEventsClient:
    """Speak the Events extension to one server as one principal."""

    def __init__(
        self,
        *,
        url: str,
        token: str | None,
        http_client: httpx.AsyncClient,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._url = url
        self._token = token
        self._http = http_client
        self._timeout = timeout_seconds
        self._ids = itertools.count(1)

    async def _request(
        self,
        method: str,
        # ast-grep-ignore: no-dict-any - JSON-RPC params are protocol-defined JSON
        params: dict[str, Any],
        *,
        name: str | None = None,
        # ast-grep-ignore: no-dict-any - JSON-RPC results are protocol-defined JSON
    ) -> dict[str, Any]:
        request_id = next(self._ids)
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {
                **params,
                "_meta": {_META_PROTOCOL_VERSION: PROTOCOL_VERSION},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name is not None and name.isascii():
            headers["Mcp-Name"] = name
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.post(
                self._url,
                json=body,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise MCPEventsError(f"{method} failed: {exc}") from exc

        message = _response_message(response, request_id)
        if message is None:
            raise MCPEventsError(
                f"{method} failed: HTTP {response.status_code} without a JSON-RPC reply"
            )
        error = message.get("error")
        if isinstance(error, dict):
            raise MCPEventsRPCError(
                f"{method} failed: {error.get('message', 'error')} "
                f"(code {error.get('code')})"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPEventsError(f"{method} returned no result")
        return cast("dict[str, Any]", result)

    async def supports_events(self) -> bool:
        """Whether ``server/discover`` advertises the Events extension.

        A server that answers with a JSON-RPC error does not speak 2026-07-28;
        one that cannot be reached at all raises, so an outage is not reported
        as a missing capability.
        """
        try:
            result = await self._request("server/discover", {})
        except MCPEventsRPCError as exc:
            logger.debug("server/discover refused at %s: %s", self._url, exc)
            return False
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict):
            return False
        extensions = capabilities.get("extensions")
        return (
            isinstance(extensions, dict) and EVENTS_EXTENSION in extensions
        ) or "events" in capabilities

    async def list_events(self) -> list[MCPEventType]:
        """All event types the server offers, following pagination."""
        event_types: list[MCPEventType] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("events/list", params)
            for entry in result.get("events") or []:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    event_types.append(_event_type(entry))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return event_types
            cursor = next_cursor

    async def poll(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - subscription arguments follow the event's inputSchema
        arguments: Mapping[str, Any],
        cursor: str | None,
    ) -> MCPPollResult:
        """One ``events/poll`` page. A ``None`` cursor starts from now."""
        result = await self._request(
            "events/poll",
            {
                "name": name,
                "arguments": dict(arguments),
                "cursor": cursor,
                "maxEvents": _MAX_EVENTS_PER_POLL,
            },
            name=name,
        )
        entries = result.get("events") or []
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict) for entry in entries
        ):
            # Skipping a malformed entry and advancing past it would lose it.
            raise MCPEventsError(f"events/poll for {name} returned malformed events")
        events = [_occurrence(entry) for entry in entries]
        next_cursor = result.get("cursor")
        next_poll_ms = result.get("nextPollMs")
        return MCPPollResult(
            events=events,
            cursor=next_cursor if isinstance(next_cursor, str) else None,
            truncated=result.get("truncated") is True,
            has_more=result.get("hasMore") is True,
            next_poll_ms=next_poll_ms if isinstance(next_poll_ms, int) else None,
        )


def _response_message(
    response: httpx.Response,
    request_id: int,
    # ast-grep-ignore: no-dict-any - JSON-RPC messages are protocol-defined JSON
) -> dict[str, Any] | None:
    """The JSON-RPC reply to ``request_id`` from a JSON or SSE response body."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                message = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        return None
    try:
        message = response.json()
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


# ast-grep-ignore: no-dict-any - an events/list entry is protocol-defined JSON
def _event_type(entry: dict[str, Any]) -> MCPEventType:
    delivery = entry.get("delivery")
    input_schema = entry.get("inputSchema")
    payload_schema = entry.get("payloadSchema")
    return MCPEventType(
        name=entry["name"],
        description=str(entry.get("description") or ""),
        delivery=tuple(str(mode) for mode in delivery)
        if isinstance(delivery, list)
        else (),
        input_schema=input_schema
        if isinstance(input_schema, dict)
        else {"type": "object"},
        payload_schema=payload_schema if isinstance(payload_schema, dict) else None,
    )


# ast-grep-ignore: no-dict-any - a delivered event is protocol-defined JSON
def _occurrence(entry: dict[str, Any]) -> MCPEventOccurrence:
    data = entry.get("data")
    timestamp = entry.get("timestamp")
    return MCPEventOccurrence(
        event_id=str(entry.get("eventId") or ""),
        name=str(entry.get("name") or ""),
        timestamp=timestamp if isinstance(timestamp, str) else None,
        data=data if isinstance(data, dict) else {"value": data},
    )
