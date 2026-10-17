"""Judging whether a camera frame satisfies a condition.

The camera search tools ask one question of many frames, so the per-frame work
is a judgement -- the probability that a frame shows what the condition
describes -- not a description. Jev answers that directly when the deployment
has TypeSafe configured; otherwise the turn's model answers it with structured
output, mapped onto the same probability. See docs/design/camera-analyst-jev.md.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field

from family_assistant.llm.base import LLMProviderError
from family_assistant.llm.messages import (
    ImageUrlContentPart,
    SystemMessage,
    TextContentPart,
    UserMessage,
)
from family_assistant.llm.typesafe import JevImage, NoulQuestion, TypeSafeError

if TYPE_CHECKING:
    from family_assistant.config_models import CameraFrameJudgeConfig
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.typesafe import JevClient

logger = logging.getLogger(__name__)


class FrameJudgeError(Exception):
    """The judge could not answer for this frame."""


class FrameJudge(Protocol):
    """Answers how likely a frame is to satisfy a condition."""

    @property
    def name(self) -> str:
        """Which judge answered, for the tool's result and the logs."""
        ...

    async def judge(self, jpeg_bytes: bytes, condition: str) -> float:
        """The probability that the frame satisfies *condition*.

        Raises :class:`FrameJudgeError` when there is no answer.
        """
        ...


class JevFrameJudge:
    """Asks Jev one yes/no question per frame, with the frame as an image asset."""

    def __init__(self, client: JevClient, *, timeout_seconds: float) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return f"typesafe:{self._client.model}"

    async def judge(self, jpeg_bytes: bytes, condition: str) -> float:
        reference = str(uuid.uuid4())
        question = NoulQuestion(
            f"Look at the security camera frame `frame`. Is this true: {condition}"
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                answers = await self._client.ask(
                    {"frame": reference},
                    {"condition": question},
                    images={reference: JevImage(jpeg_bytes, "jpeg")},
                )
        except TypeSafeError as error:
            raise FrameJudgeError(str(error)) from error
        except TimeoutError as error:
            raise FrameJudgeError(
                f"Jev did not answer within {self._timeout_seconds}s"
            ) from error
        return answers.nouls["condition"]


class _FrameJudgementResponse(BaseModel):
    satisfied: bool = Field(description="Whether the frame satisfies the condition")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in that answer, from 0.0 to 1.0"
    )


_LLM_JUDGE_SYSTEM_PROMPT = (
    "You are judging a single frame from a security camera recording. Decide "
    "whether the frame satisfies the condition you are given, and how confident "
    "you are. Judge only what is visible in this frame."
)


class LlmFrameJudge:
    """Asks a chat model, with structured output, whether the frame satisfies it."""

    def __init__(self, client: LLMInterface, *, name: str) -> None:
        self._client = client
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def judge(self, jpeg_bytes: bytes, condition: str) -> float:
        image_part = ImageUrlContentPart(
            type="image_url",
            image_url={
                "url": "data:image/jpeg;base64,"
                + base64.b64encode(jpeg_bytes).decode("ascii")
            },
        )
        text_part = TextContentPart(type="text", text=f"Condition: {condition}")
        try:
            response = await self._client.generate_structured(
                messages=[
                    SystemMessage(content=_LLM_JUDGE_SYSTEM_PROMPT),
                    UserMessage(content=[image_part, text_part]),
                ],
                response_model=_FrameJudgementResponse,
            )
        except LLMProviderError as error:
            # One frame's failure -- a rate limit, a timeout, unparseable
            # output -- leaves that frame unjudged rather than ending the search.
            raise FrameJudgeError(
                f"The model could not judge the frame: {error}"
            ) from error
        if response.satisfied:
            return response.confidence
        return 1.0 - response.confidence


def jev_frame_judge(
    client: JevClient | None, config: CameraFrameJudgeConfig
) -> JevFrameJudge | None:
    """Jev as the frame judge, unless the deployment has no Jev or turned it off here."""
    if client is None or config.mode == "off":
        return None
    return JevFrameJudge(client, timeout_seconds=config.timeout_seconds)
