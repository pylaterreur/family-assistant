"""Sampling recorded footage, judging frames, and the camera's wall clock."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.typesafe import JevAnswers
from family_assistant.plugins.reolink.backend import as_wall_clock, wall_clock_days
from family_assistant.plugins.reolink.judge import (
    FrameJudgeError,
    JevFrameJudge,
    LlmFrameJudge,
)
from family_assistant.plugins.reolink.protocol import Recording
from family_assistant.plugins.reolink.search import (
    plan_sample_times,
    recorded_times_between,
)
from tests.mocks.fake_jev import FakeJevClient
from tests.mocks.mock_llm import RuleBasedMockLLMClient

SYDNEY = ZoneInfo("Australia/Sydney")
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=SYDNEY)


def _clip(start_minute: float, seconds: float) -> Recording:
    start = NOON + timedelta(minutes=start_minute)
    return Recording(
        camera_id="cam", start_time=start, end_time=start + timedelta(seconds=seconds)
    )


def test_continuous_recording_is_sampled_on_the_grid() -> None:
    recordings = [_clip(-60, 3 * 3600)]

    times = plan_sample_times(
        recordings,
        NOON,
        NOON + timedelta(minutes=30),
        interval=timedelta(minutes=10),
        max_frames=10,
    )

    assert times == [NOON + timedelta(minutes=m) for m in (0, 10, 20, 30)]


def test_each_motion_clip_the_grid_misses_gets_a_frame() -> None:
    recordings = [_clip(3, 60), _clip(37, 60)]

    times = plan_sample_times(
        recordings,
        NOON,
        NOON + timedelta(hours=1),
        interval=timedelta(minutes=10),
        max_frames=10,
    )

    assert len(times) == 2
    assert all(
        clip.start_time < time < clip.end_time
        for clip, time in zip(recordings, times, strict=True)
    )


def test_samples_keep_clear_of_the_end_of_a_clip() -> None:
    clip = _clip(0, 30)

    (time,) = plan_sample_times(
        [clip],
        NOON,
        NOON + timedelta(hours=1),
        interval=timedelta(hours=1),
        max_frames=5,
    )

    assert time <= clip.end_time - timedelta(seconds=8)


def test_more_candidates_than_frames_are_thinned_evenly() -> None:
    recordings = [_clip(minute, 30) for minute in range(0, 60, 5)]

    times = plan_sample_times(
        recordings,
        NOON,
        NOON + timedelta(hours=1),
        interval=timedelta(hours=1),
        max_frames=3,
    )

    assert [time.minute for time in times] == [0, 30, 55]


def test_moments_between_two_frames_come_from_inside_recordings() -> None:
    recordings = [_clip(0, 600), _clip(30, 600)]

    moments = recorded_times_between(
        recordings, NOON, NOON + timedelta(minutes=40), count=3
    )

    assert moments
    assert all(
        any(clip.start_time <= moment <= clip.end_time for clip in recordings)
        for moment in moments
    )


def test_nothing_recorded_between_two_frames_gives_no_moments() -> None:
    recordings = [_clip(0, 60), _clip(30, 60)]

    moments = recorded_times_between(
        recordings,
        NOON + timedelta(seconds=55),
        NOON + timedelta(minutes=30, seconds=1),
        count=3,
    )

    assert moments == []


def test_a_range_across_midnight_is_searched_one_day_at_a_time() -> None:
    start = datetime(2026, 10, 15, 22, 0, tzinfo=SYDNEY)

    windows = wall_clock_days(start, start + timedelta(hours=4))

    assert windows == [
        (start, datetime(2026, 10, 15, 23, 59, 59, tzinfo=SYDNEY)),
        (
            datetime(2026, 10, 16, 0, 0, tzinfo=SYDNEY),
            datetime(2026, 10, 16, 2, 0, tzinfo=SYDNEY),
        ),
    ]


def test_a_camera_time_keeps_its_wall_clock_in_the_household_zone() -> None:
    # reolink_aio tags times with the camera's base offset, ignoring daylight
    # saving that the camera's own clock follows.
    reported = datetime(2026, 10, 15, 16, 36, tzinfo=ZoneInfo("Etc/GMT-10"))

    assert as_wall_clock(reported, SYDNEY) == datetime(
        2026, 10, 15, 16, 36, tzinfo=SYDNEY
    )


@pytest.mark.asyncio
async def test_jev_judges_a_frame_by_its_image() -> None:
    jev = FakeJevClient(answers=JevAnswers("jev-test", {"condition": 0.8}, {}, 10))

    probability = await JevFrameJudge(jev, timeout_seconds=1).judge(
        b"jpeg", "A person is visible."
    )

    ((image,),) = (list(images.values()) for images in jev.image_calls)
    assert (probability, image.data, image.format) == (0.8, b"jpeg", "jpeg")


@pytest.mark.asyncio
async def test_jev_failure_is_a_frame_judge_error() -> None:
    judge = JevFrameJudge(FakeJevClient(fail=True), timeout_seconds=1)

    with pytest.raises(FrameJudgeError):
        await judge.judge(b"jpeg", "A person is visible.")


@pytest.mark.asyncio
async def test_a_model_sure_the_condition_fails_gives_a_low_probability() -> None:
    llm = RuleBasedMockLLMClient(
        rules=[],
        structured_rules=[
            (
                lambda _: True,
                lambda call: call["response_model"].model_validate({
                    "satisfied": False,
                    "confidence": 0.9,
                }),
            ),
        ],
    )

    probability = await LlmFrameJudge(llm, name="turn model").judge(
        b"jpeg", "A person is visible."
    )

    assert probability == pytest.approx(0.1)


def test_wall_clock_days_of_a_range_inside_one_day_is_that_range() -> None:
    start = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    assert wall_clock_days(start, start + timedelta(hours=1)) == [
        (start, start + timedelta(hours=1))
    ]
