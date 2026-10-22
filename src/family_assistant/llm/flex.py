"""Flex inference: background calls at the discounted price class.

Google, OpenAI and OpenRouter sell the same synchronous request at about half
price as "Flex", in exchange for queueing and the occasional capacity
rejection. Whether a call may wait is a property of the work, not of the model
(see ``call_context.Workload``), so the same client serves both: this wrapper
decides per call, and the provider client's request chokepoint reads the
decision from ``call_context.current_service_tier``.

A background call on a flex-enabled chain entry is attempted at Flex, and if
Flex has no capacity -- rate limited, unavailable, timed out, or past the
maximum wait -- before anything was streamed, it is made again at Standard on
the same model. Only a failure at Standard reaches the chain's own fallback,
which is usually another provider at full price. After a capacity failure the
entry stops attempting Flex for a cool-down, so a capacity crunch costs one
wasted wait per entry rather than one per call.

See docs/design/flex-inference.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from typing import TYPE_CHECKING, TypeVar

from opentelemetry import trace
from pydantic import BaseModel

from family_assistant.llm.base import (
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
    stream_error_to_exception,
)
from family_assistant.llm.call_context import (
    Workload,
    current_workload,
    with_service_tier,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from family_assistant.llm import (
        JsonObject,
        LLMInterface,
        LLMMessage,
        LLMOutput,
        LLMStreamEvent,
        UserMessageDict,
    )
    from family_assistant.llm.messages import UserMessage
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

__all__ = [
    "FLEX_COOLDOWN_SECONDS",
    "FLEX_MAX_WAIT_SECONDS",
    "FLEX_PROVIDERS",
    "FlexClient",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

# Providers whose request API takes `service_tier: flex`. OpenRouter is the
# OpenAI client at another base URL. Anthropic has no Flex: its discount is
# Message Batches, an asynchronous API a tool-calling turn cannot use.
FLEX_PROVIDERS = frozenset({"google", "openai"})

# How long a Flex attempt may wait -- for its first streamed event, or for the
# whole response when not streaming -- before it is abandoned for Standard.
# Bounds what a background turn can lose to queueing per call, so the task
# handler timeouts sized for Standard still hold.
FLEX_MAX_WAIT_SECONDS = 120.0

# How long an entry stops attempting Flex after a capacity failure.
FLEX_COOLDOWN_SECONDS = 600.0

_CAPACITY_ERRORS = (
    RateLimitError,
    ServiceUnavailableError,
    ProviderTimeoutError,
    TimeoutError,
)


class FlexClient:
    """Runs background calls at Flex, degrading to Standard on the same model."""

    def __init__(
        self,
        inner: LLMInterface,
        *,
        model: str,
        max_wait_seconds: float = FLEX_MAX_WAIT_SECONDS,
        cooldown_seconds: float = FLEX_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.flex_model = model
        self._max_wait_seconds = max_wait_seconds
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._suspended_until: float | None = None

    def __getattr__(self, name: str) -> object:
        # Code that reads a client's model or provider by duck typing
        # (diagnostics, indexing provenance) sees the wrapped client's.
        return getattr(self.inner, name)

    @property
    def flex_suspended(self) -> bool:
        """Whether a recent capacity failure has paused Flex on this entry."""
        return (
            self._suspended_until is not None and self._clock() < self._suspended_until
        )

    def _attempt_flex(self) -> bool:
        return current_workload() is Workload.BACKGROUND and not self.flex_suspended

    def _degrade(self, error: BaseException) -> None:
        self._suspended_until = self._clock() + self._cooldown_seconds
        logger.warning(
            "Flex attempt on %s had no capacity (%s: %s); retrying at Standard "
            "and pausing Flex for %.0fs.",
            self.flex_model,
            type(error).__name__,
            error,
            self._cooldown_seconds,
        )
        trace.get_current_span().add_event(
            "llm.flex_degraded",
            attributes={
                "model": self.flex_model,
                "error_type": type(error).__name__,
            },
        )

    async def _run(self, invoke: Callable[[], Awaitable[R]]) -> R:
        if self._attempt_flex():
            try:
                with with_service_tier("flex"):
                    async with asyncio.timeout(self._max_wait_seconds):
                        return await invoke()
            except _CAPACITY_ERRORS as error:
                self._degrade(error)
        return await invoke()

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        return await self._run(
            lambda: self.inner.generate_response(messages, tools, tool_choice)
        )

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        if self._attempt_flex():
            stream = self.inner.generate_response_stream(messages, tools, tool_choice)
            try:
                first = await self._first_flex_event(stream)
            except StopAsyncIteration:
                return
            except _CAPACITY_ERRORS as error:
                # The protocol types the stream as an iterator; every provider's
                # is an async generator, which an abandoned attempt must close.
                aclose = getattr(stream, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        closing = aclose()
                        if inspect.isawaitable(closing):
                            await closing
                self._degrade(error)
            else:
                async for event in self._continue_flex_stream(first, stream):
                    yield event
                return
        async for event in self.inner.generate_response_stream(
            messages, tools, tool_choice
        ):
            yield event

    async def _first_flex_event(
        self, stream: AsyncIterator[LLMStreamEvent]
    ) -> LLMStreamEvent:
        """The stream's first event at Flex, or the capacity error it amounts to.

        Waiting for the first event is what queueing for Flex capacity looks
        like on a stream, so that is what the maximum wait bounds.
        """
        with with_service_tier("flex"):
            async with asyncio.timeout(self._max_wait_seconds):
                first = await anext(stream)
        # Providers that cannot raise mid-generator report a failure as an
        # error event; one that arrives first is a request that never started,
        # as retryable as a raised one.
        if first.type == "error":
            error = stream_error_to_exception(first)
            if isinstance(error, _CAPACITY_ERRORS):
                raise error
        return first

    @staticmethod
    async def _continue_flex_stream(
        first: LLMStreamEvent, stream: AsyncIterator[LLMStreamEvent]
    ) -> AsyncIterator[LLMStreamEvent]:
        """The rest of an accepted Flex stream.

        The tier is held only around each pull, never across a ``yield``, so
        it cannot leak into whatever the consumer does between events.
        """
        yield first
        while True:
            try:
                with with_service_tier("flex"):
                    event = await anext(stream)
            except StopAsyncIteration:
                return
            yield event

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        return await self._run(
            lambda: self.inner.generate_structured(
                messages, response_model, max_retries
            )
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        return await self._run(lambda: self.inner.generate_json(messages, max_retries))

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        return await self.inner.format_user_message_with_file(
            prompt_text, file_path, mime_type, max_text_length
        )

    def create_attachment_injection(self, attachment: ToolAttachment) -> UserMessage:
        return self.inner.create_attachment_injection(attachment)

    async def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
