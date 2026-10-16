"""Searching recorded camera footage with a frame judge.

Cameras that record on motion leave most of a day unrecorded, so frames are
sampled from inside the recordings a range holds rather than on a blind grid,
which would mostly land in gaps. See docs/design/camera-analyst-jev.md.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from family_assistant.plugins.reolink.judge import FrameJudgeError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from family_assistant.plugins.reolink.judge import FrameJudge
    from family_assistant.plugins.reolink.protocol import CameraBackend, Recording

logger = logging.getLogger(__name__)

_ONE_SECOND = timedelta(seconds=1)
# Seeking playback near the end of a clip leaves the camera streaming nothing
# until the extraction times out -- a clip's listed end overstates its footage
# by a few seconds -- so samples keep well clear of the end, and a little clear
# of the start.
_CLIP_HEAD = timedelta(seconds=2)
_CLIP_TAIL = timedelta(seconds=8)


def _sampleable_span(recording: Recording) -> tuple[datetime, datetime]:
    """The part of a recording a frame can be extracted from reliably."""
    start = recording.start_time + _CLIP_HEAD
    end = recording.end_time - _CLIP_TAIL
    if start > end:
        third = recording.start_time + (recording.end_time - recording.start_time) / 3
        return third, third
    return start, end


# Probes judged at each narrowing step. One is plain bisection; more split the
# gap further per step, which only pays where the camera extracts concurrently.
_PROBES_PER_STEP = 1


@dataclass(frozen=True, slots=True)
class SampledFrame:
    """A frame sampled at ``timestamp``, or why there is none."""

    timestamp: datetime
    jpeg_bytes: bytes | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class JudgedFrame:
    """A frame and the probability that it satisfies the condition, or why not."""

    timestamp: datetime
    jpeg_bytes: bytes | None
    probability: float | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Change:
    """Where the condition's answer flips between two judged frames.

    ``before`` and ``after`` are the closest pair the search reached. The
    change is ``resolved`` when they are within the requested precision;
    otherwise ``reason`` says why the search stopped short.
    """

    before: JudgedFrame
    after: JudgedFrame
    resolved: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeSearch:
    """The coarse pass over a range and the changes found in it."""

    coarse: list[JudgedFrame]
    changes: list[Change]
    frames_judged: int


def plan_sample_times(
    recordings: Sequence[Recording],
    start_time: datetime,
    end_time: datetime,
    *,
    interval: timedelta,
    max_frames: int,
) -> list[datetime]:
    """When to sample a range: every *interval* that is recorded, plus each
    recording no grid point fell in, spread evenly down to *max_frames*.

    On a camera that records continuously this is the plain grid. On one that
    records on motion it is a frame from each burst of activity, which is
    where anything worth finding happened.
    """
    spans = [
        (max(span_start, start_time), min(span_end, end_time))
        for span_start, span_end in map(_sampleable_span, recordings)
    ]
    spans = [(low, high) for low, high in spans if low <= high]
    times: set[datetime] = set()
    covered: set[int] = set()
    point = start_time
    while point <= end_time:
        for index, (low, high) in enumerate(spans):
            if low <= point <= high:
                times.add(point)
                covered.add(index)
                break
        point += interval
    for index, (low, high) in enumerate(spans):
        if index not in covered:
            times.add(low + (high - low) / 2)
    ordered = sorted(times)
    if len(ordered) <= max_frames:
        return ordered
    if max_frames == 1:
        return [ordered[len(ordered) // 2]]
    last = len(ordered) - 1
    return [ordered[round(i * last / (max_frames - 1))] for i in range(max_frames)]


def recorded_times_between(
    recordings: Sequence[Recording], after: datetime, before: datetime, *, count: int
) -> list[datetime]:
    """Up to *count* recorded moments strictly between two times, spread evenly.

    Each aims at an even division of the gap and lands on the closest moment
    that can be sampled. Empty when nothing between them was recorded.
    """
    low, high = after + _ONE_SECOND, before - _ONE_SECOND
    if low > high:
        return []
    spans = [
        (max(span_start, low), min(span_end, high))
        for span_start, span_end in map(_sampleable_span, recordings)
    ]
    spans = [(start, end) for start, end in spans if start <= end]
    moments: set[datetime] = set()
    for index in range(1, count + 1):
        target = after + (before - after) * index / (count + 1)
        candidates = [min(max(target, start), end) for start, end in spans]
        if candidates:
            moments.add(min(candidates, key=lambda moment: abs(moment - target)))
    return sorted(moments)


async def sample_frames(
    backend: CameraBackend, camera_id: str, times: Sequence[datetime]
) -> list[SampledFrame]:
    """The frame at each time, or the reason it could not be extracted."""

    async def sample(timestamp: datetime) -> SampledFrame:
        try:
            jpeg_bytes = await backend.get_frame(camera_id, timestamp)
        except (ValueError, RuntimeError) as error:
            logger.warning("Could not extract frame at %s: %s", timestamp, error)
            return SampledFrame(timestamp, None, str(error))
        return SampledFrame(timestamp, jpeg_bytes)

    return list(await asyncio.gather(*(sample(time) for time in times)))


async def judge_frames(
    judge: FrameJudge, frames: Sequence[SampledFrame], condition: str
) -> list[JudgedFrame]:
    """Judge every extracted frame concurrently; a frame without one stays unjudged."""

    async def judge_one(frame: SampledFrame) -> JudgedFrame:
        if frame.jpeg_bytes is None:
            return JudgedFrame(frame.timestamp, None, None, frame.error)
        try:
            probability = await judge.judge(frame.jpeg_bytes, condition)
        except FrameJudgeError as error:
            logger.warning(
                "%s could not judge frame at %s: %s", judge.name, frame.timestamp, error
            )
            return JudgedFrame(frame.timestamp, frame.jpeg_bytes, None, str(error))
        return JudgedFrame(frame.timestamp, frame.jpeg_bytes, probability)

    return list(await asyncio.gather(*(judge_one(frame) for frame in frames)))


async def find_changes(
    *,
    backend: CameraBackend,
    judge: FrameJudge,
    camera_id: str,
    recordings: Sequence[Recording],
    start_time: datetime,
    end_time: datetime,
    condition: str,
    threshold: float,
    precision: timedelta,
    coarse_frames: int,
    max_changes: int,
    frame_budget: int,
) -> ChangeSearch:
    """Find where the condition's answer flips, and bisect each flip to *precision*.

    A coarse pass judges frames spread over the recordings in the range; each
    adjacent pair that disagrees brackets a change, which is then narrowed by
    judging recorded moments between the pair. A change that happened while
    the camera was not recording cannot be narrowed past the gap, and is
    reported at the gap's edges.
    """
    times = plan_sample_times(
        recordings,
        start_time,
        end_time,
        interval=(end_time - start_time) / max(coarse_frames - 1, 1),
        max_frames=coarse_frames,
    )
    coarse = await judge_frames(
        judge, await sample_frames(backend, camera_id, times), condition
    )
    frames_judged = len(coarse)

    def holds(frame: JudgedFrame) -> bool:
        assert frame.probability is not None
        return frame.probability >= threshold

    usable = [frame for frame in coarse if frame.probability is not None]
    brackets = [
        (before, after)
        for before, after in pairwise(usable)
        if holds(before) != holds(after)
    ][:max_changes]

    changes: list[Change] = []
    for before, after in brackets:
        change, probes = await _narrow(
            backend=backend,
            judge=judge,
            camera_id=camera_id,
            recordings=recordings,
            condition=condition,
            threshold=threshold,
            precision=precision,
            before=before,
            after=after,
            budget=frame_budget - frames_judged,
        )
        changes.append(change)
        frames_judged += probes
    return ChangeSearch(coarse=coarse, changes=changes, frames_judged=frames_judged)


async def _narrow(
    *,
    backend: CameraBackend,
    judge: FrameJudge,
    camera_id: str,
    recordings: Sequence[Recording],
    condition: str,
    threshold: float,
    precision: timedelta,
    before: JudgedFrame,
    after: JudgedFrame,
    budget: int,
) -> tuple[Change, int]:
    """Bisect one bracketed change; returns it and how many frames that took."""

    def holds(frame: JudgedFrame) -> bool:
        assert frame.probability is not None
        return frame.probability >= threshold

    probes = 0
    reason: str | None = None
    while after.timestamp - before.timestamp > precision:
        if probes >= budget:
            reason = "frame budget exhausted"
            break
        moments = recorded_times_between(
            recordings,
            before.timestamp,
            after.timestamp,
            count=min(_PROBES_PER_STEP, budget - probes),
        )
        if not moments:
            reason = "the camera was not recording between these frames"
            break
        judged = await judge_frames(
            judge, await sample_frames(backend, camera_id, moments), condition
        )
        probes += len(judged)
        usable = [frame for frame in judged if frame.probability is not None]
        if not usable:
            failure = judged[0]
            reason = f"no judgement at {failure.timestamp.isoformat()}: {failure.error}"
            break
        flipped = next(
            (frame for frame in usable if holds(frame) != holds(before)), None
        )
        if flipped is None:
            before = usable[-1]
        else:
            after = flipped
            before = max(
                (frame for frame in usable if frame.timestamp < flipped.timestamp),
                key=lambda frame: frame.timestamp,
                default=before,
            )
    change = Change(
        before=before,
        after=after,
        resolved=after.timestamp - before.timestamp <= precision,
        reason=reason,
    )
    return change, probes
