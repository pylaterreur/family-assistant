"""Unit tests for camera tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import CameraFrameJudgeConfig, TypeSafeConfig
from family_assistant.llm.typesafe import (
    ChoiceQuestion,
    JevAnswers,
    JevImage,
    NoulQuestion,
)
from family_assistant.plugins.reolink.fake import FakeCameraBackend
from family_assistant.plugins.reolink.instance import ReolinkInstance
from family_assistant.plugins.reolink.protocol import (
    CameraEvent,
    Recording,
)
from family_assistant.plugins.reolink.tools import (
    find_camera_change_tool,
    get_camera_frame_tool,
    get_camera_frames_batch_tool,
    get_camera_recordings_tool,
    list_cameras_tool,
    scan_camera_frames_tool,
    search_camera_events_tool,
)
from family_assistant.plugins.runtime import ProfilePlugins
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from tests.mocks.fake_jev import FakeJevClient

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from family_assistant.llm import LLMInterface
    from family_assistant.processing.service import ProcessingService


@pytest.fixture
def fake_camera_backend() -> FakeCameraBackend:
    """Create a FakeCameraBackend with test data."""
    backend = FakeCameraBackend()

    # Add test cameras
    backend.add_camera("cam_front", "Front Door", "online")
    backend.add_camera("cam_back", "Back Yard", "online")
    backend.add_camera("cam_garage", "Garage", "offline")

    # Add test events
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    backend.add_event(
        CameraEvent(
            camera_id="cam_front",
            start_time=base_time,
            end_time=base_time + timedelta(seconds=30),
            event_type="person",
            confidence=0.95,
            metadata={"zone": "entry"},
        )
    )
    backend.add_event(
        CameraEvent(
            camera_id="cam_front",
            start_time=base_time + timedelta(hours=1),
            end_time=base_time + timedelta(hours=1, seconds=15),
            event_type="vehicle",
            confidence=0.88,
        )
    )
    backend.add_event(
        CameraEvent(
            camera_id="cam_back",
            start_time=base_time + timedelta(minutes=30),
            end_time=base_time + timedelta(minutes=30, seconds=45),
            event_type="pet",
            confidence=0.92,
        )
    )

    # Add test recordings
    backend.add_recording(
        Recording(
            camera_id="cam_front",
            start_time=base_time - timedelta(hours=1),
            end_time=base_time + timedelta(hours=2),
            filename="front_20240115_1100.mp4",
            size_bytes=1024 * 1024 * 100,  # 100 MB
        )
    )
    backend.add_recording(
        Recording(
            camera_id="cam_back",
            start_time=base_time,
            end_time=base_time + timedelta(hours=1),
            filename="back_20240115_1200.mp4",
            size_bytes=1024 * 1024 * 50,  # 50 MB
        )
    )

    # Add test frames
    frame_time = base_time
    for i in range(5):
        backend.set_frame(
            "cam_front",
            frame_time + timedelta(minutes=i * 15),
            f"frame_{i}".encode(),
        )

    return backend


@pytest.fixture
def exec_context(fake_camera_backend: FakeCameraBackend) -> ToolExecutionContext:
    """Create a ToolExecutionContext with fake camera backend."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        plugins=ProfilePlugins((ReolinkInstance(fake_camera_backend),)),
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_list_cameras_success(exec_context: ToolExecutionContext) -> None:
    """Test listing cameras successfully returns all cameras."""
    result = await list_cameras_tool(exec_context)

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "cameras" in data
    assert data["count"] == 3

    cameras = data["cameras"]
    assert len(cameras) == 3

    # Verify camera data structure
    front_cam = next(c for c in cameras if c["id"] == "cam_front")
    assert front_cam["name"] == "Front Door"
    assert front_cam["status"] == "online"
    assert front_cam["backend"] == "fake"

    garage_cam = next(c for c in cameras if c["id"] == "cam_garage")
    assert garage_cam["status"] == "offline"


@pytest.mark.asyncio
async def test_list_cameras_no_backend() -> None:
    """Test listing cameras returns error when the profile has no cameras."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    result = await list_cameras_tool(exec_context)

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_search_camera_events_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test searching camera events finds events in time range."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=2)).isoformat()

    result = await search_camera_events_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "events" in data
    assert data["count"] == 2

    events = data["events"]
    assert len(events) == 2

    # Verify first event (person)
    person_event = events[0]
    assert person_event["camera_id"] == "cam_front"
    assert person_event["event_type"] == "person"
    assert person_event["confidence"] == 0.95
    assert person_event["metadata"]["zone"] == "entry"

    # Verify second event (vehicle)
    vehicle_event = events[1]
    assert vehicle_event["event_type"] == "vehicle"
    assert vehicle_event["confidence"] == 0.88


@pytest.mark.asyncio
async def test_search_camera_events_with_type_filter(
    exec_context: ToolExecutionContext,
) -> None:
    """Test searching camera events with event type filter."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=2)).isoformat()

    result = await search_camera_events_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        event_types=["person"],
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["count"] == 1

    events = data["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "person"


@pytest.mark.asyncio
async def test_search_camera_events_no_backend() -> None:
    """Test searching events returns error when the profile has no cameras."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await search_camera_events_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_frame_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting a single camera frame returns frame attachment."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    timestamp = base_time.isoformat()

    result = await get_camera_frame_tool(
        exec_context,
        camera_id="cam_front",
        timestamp=timestamp,
    )

    assert isinstance(result, ToolResult)
    assert result.attachments is not None
    assert len(result.attachments) == 1

    attachment = result.attachments[0]
    assert attachment.mime_type == "image/jpeg"
    assert attachment.content == b"frame_0"
    assert "Camera frame at" in attachment.description


@pytest.mark.asyncio
async def test_get_camera_frame_not_found(
    exec_context: ToolExecutionContext,
) -> None:
    """A moment no recording covers is an error, not a frame."""
    timestamp = datetime(2024, 1, 15, 20, 0, 0, tzinfo=UTC).isoformat()

    result = await get_camera_frame_tool(
        exec_context,
        camera_id="cam_front",
        timestamp=timestamp,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "No recording found" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_frame_no_backend() -> None:
    """Test getting frame returns error when the profile has no cameras."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    timestamp = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC).isoformat()
    result = await get_camera_frame_tool(
        exec_context,
        camera_id="cam_front",
        timestamp=timestamp,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_frames_batch_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting batch frames returns multiple frame attachments."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = base_time.isoformat()
    end_time = (base_time + timedelta(hours=1)).isoformat()

    result = await get_camera_frames_batch_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        interval_minutes=15,
        max_frames=10,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "timestamps" in data
    assert data["count"] == 5  # 5 frames at 15-minute intervals (0, 15, 30, 45, 60)

    # Verify attachments
    assert result.attachments is not None
    assert len(result.attachments) == 5

    for i, attachment in enumerate(result.attachments):
        assert attachment.mime_type == "image/jpeg"
        assert attachment.content == f"frame_{i}".encode()
        assert f"Frame {i + 1}/5" in attachment.description


@pytest.mark.asyncio
async def test_get_camera_frames_batch_no_frames(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting batch frames returns empty result when no frames available."""
    # Use a time range with no frames
    start_time = datetime(2024, 1, 15, 20, 0, 0, tzinfo=UTC).isoformat()
    end_time = datetime(2024, 1, 15, 21, 0, 0, tzinfo=UTC).isoformat()

    result = await get_camera_frames_batch_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
        interval_minutes=15,
        max_frames=10,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["count"] == 0
    assert len(data["timestamps"]) == 0


@pytest.mark.asyncio
async def test_get_camera_frames_batch_no_backend() -> None:
    """Test getting batch frames returns error when the profile has no cameras."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await get_camera_frames_batch_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_get_camera_recordings_success(
    exec_context: ToolExecutionContext,
) -> None:
    """Test getting camera recordings returns recording list."""
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    start_time = (base_time - timedelta(hours=2)).isoformat()
    end_time = (base_time + timedelta(hours=3)).isoformat()

    result = await get_camera_recordings_tool(
        exec_context,
        camera_id="cam_front",
        start_time=start_time,
        end_time=end_time,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "recordings" in data
    assert data["count"] == 1

    recordings = data["recordings"]
    assert len(recordings) == 1

    recording = recordings[0]
    assert recording["camera_id"] == "cam_front"
    assert recording["filename"] == "front_20240115_1100.mp4"
    assert recording["size_bytes"] == 1024 * 1024 * 100
    assert recording["duration_seconds"] == 3 * 3600  # 3 hours


@pytest.mark.asyncio
async def test_get_camera_recordings_no_backend() -> None:
    """Test getting recordings returns error when the profile has no cameras."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = await get_camera_recordings_tool(
        exec_context,
        camera_id="cam_front",
        start_time=base_time.isoformat(),
        end_time=(base_time + timedelta(hours=1)).isoformat(),
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "Camera backend not configured" in data["error"]


# --- Tests for scan_camera_frames_tool and find_camera_change_tool ---

_SCAN_BASE = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


def _jev_by_bytes(scores: Mapping[bytes, float]) -> FakeJevClient:
    """A Jev that answers each frame by its bytes, so a test controls every judgement."""

    def answer(
        images: Mapping[str, JevImage],
        questions: Mapping[str, NoulQuestion | ChoiceQuestion],
    ) -> JevAnswers:
        (image,) = images.values()
        return JevAnswers(
            "jev-test", dict.fromkeys(questions, scores[image.data]), {}, 10
        )

    return FakeJevClient(respond_to_images=answer)


def _judging_context(
    backend: FakeCameraBackend,
    *,
    jev: FakeJevClient | None = None,
    camera_frames: CameraFrameJudgeConfig | None = None,
    llm_client: object = None,
) -> ToolExecutionContext:
    service = cast(
        "ProcessingService",
        SimpleNamespace(
            jev_client=jev,
            app_config=SimpleNamespace(
                typesafe=TypeSafeConfig(
                    camera_frames=camera_frames or CameraFrameJudgeConfig()
                )
            ),
        ),
    )
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=Mock(),
        processing_service=service,
        llm_client=cast("LLMInterface | None", llm_client),
        clock=None,
        plugins=ProfilePlugins((ReolinkInstance(backend),)),
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


def _continuous_camera(frames: Mapping[int, bytes]) -> FakeCameraBackend:
    """A camera recording 11:00-15:00 continuously, frames set at minutes past 12:00."""
    backend = FakeCameraBackend()
    backend.add_camera("cam", "Camera")
    backend.add_recording(
        Recording(
            camera_id="cam",
            start_time=_SCAN_BASE - timedelta(hours=1),
            end_time=_SCAN_BASE + timedelta(hours=3),
        )
    )
    for minute, jpeg in frames.items():
        backend.set_frame("cam", _SCAN_BASE + timedelta(minutes=minute), jpeg)
    return backend


@pytest.mark.asyncio
async def test_scan_attaches_only_matching_frames_most_likely_first() -> None:
    backend = _continuous_camera({
        0: b"empty",
        15: b"person",
        30: b"maybe",
        45: b"empty",
    })
    jev = _jev_by_bytes({b"empty": 0.05, b"person": 0.95, b"maybe": 0.6})

    result = await scan_camera_frames_tool(
        _judging_context(backend, jev=jev),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(minutes=45)).isoformat(),
        condition="A person is visible.",
        interval_seconds=900,
    )

    assert result.attachments is not None
    assert [a.content for a in result.attachments] == [b"person", b"maybe"]


@pytest.mark.asyncio
async def test_scan_reports_every_judged_frame_in_the_timeline() -> None:
    backend = _continuous_camera({0: b"empty", 15: b"person", 30: b"empty"})
    jev = _jev_by_bytes({b"empty": 0.05, b"person": 0.95})

    result = await scan_camera_frames_tool(
        _judging_context(backend, jev=jev),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(minutes=30)).isoformat(),
        condition="A person is visible.",
        interval_seconds=900,
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert [entry["probability"] for entry in data["timeline"]] == [0.05, 0.95, 0.05]
    assert data["judge"] == "typesafe:jev-test"


@pytest.mark.asyncio
async def test_scan_without_a_match_names_the_closest_frames() -> None:
    backend = _continuous_camera({0: b"far", 15: b"near"})
    jev = _jev_by_bytes({b"far": 0.05, b"near": 0.4})

    result = await scan_camera_frames_tool(
        _judging_context(backend, jev=jev),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(minutes=15)).isoformat(),
        condition="A person is visible.",
        interval_seconds=900,
    )

    assert result.attachments is None
    assert "Closest: 2024-01-15T12:15:00+00:00 (p=0.40)" in (result.text or "")


@pytest.mark.asyncio
async def test_scan_samples_each_motion_clip_rather_than_a_blind_grid() -> None:
    backend = FakeCameraBackend()
    backend.add_camera("cam", "Camera")
    clips = [(7, b"first"), (41, b"second")]
    for minute, jpeg in clips:
        clip_start = _SCAN_BASE + timedelta(minutes=minute)
        backend.add_recording(
            Recording(
                camera_id="cam",
                start_time=clip_start,
                end_time=clip_start + timedelta(seconds=30),
            )
        )
        backend.set_frame("cam", clip_start, jpeg)
    jev = _jev_by_bytes({b"first": 0.9, b"second": 0.9})

    result = await scan_camera_frames_tool(
        _judging_context(backend, jev=jev),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(hours=1)).isoformat(),
        condition="A chicken is visible.",
        interval_seconds=900,
    )

    assert result.attachments is not None
    assert sorted(a.content or b"" for a in result.attachments) == [b"first", b"second"]


@pytest.mark.asyncio
async def test_scan_counts_frames_the_judge_could_not_answer() -> None:
    backend = _continuous_camera({0: b"frame"})

    result = await scan_camera_frames_tool(
        _judging_context(backend, jev=FakeJevClient(fail=True)),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(minutes=15)).isoformat(),
        condition="A person is visible.",
        interval_seconds=900,
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["warning"] == "2 frame(s) could not be extracted or judged"


@pytest.mark.asyncio
async def test_scan_reports_a_range_with_nothing_recorded() -> None:
    backend = _continuous_camera({0: b"frame"})

    result = await scan_camera_frames_tool(
        _judging_context(backend, jev=_jev_by_bytes({})),
        camera_id="cam",
        start_time=(_SCAN_BASE + timedelta(hours=5)).isoformat(),
        end_time=(_SCAN_BASE + timedelta(hours=6)).isoformat(),
        condition="A person is visible.",
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["message"] == "The camera recorded nothing in this time range."


@pytest.mark.asyncio
async def test_scan_asks_the_turn_model_when_jev_is_off_for_camera_frames() -> None:
    backend = _continuous_camera({0: b"frame"})

    async def judge(messages: object, response_model: type[BaseModel]) -> BaseModel:
        return response_model.model_validate({"satisfied": False, "confidence": 0.8})

    llm = Mock()
    llm.generate_structured = judge

    result = await scan_camera_frames_tool(
        _judging_context(
            backend,
            jev=_jev_by_bytes({}),
            camera_frames=CameraFrameJudgeConfig(mode="off"),
            llm_client=llm,
        ),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=_SCAN_BASE.isoformat(),
        condition="A person is visible.",
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["judge"] == "turn model"
    assert data["timeline"][0]["probability"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_scan_without_any_judge_is_an_error() -> None:
    backend = _continuous_camera({0: b"frame"})
    context = _judging_context(backend)

    result = await scan_camera_frames_tool(
        context,
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=_SCAN_BASE.isoformat(),
        condition="A person is visible.",
    )

    assert result.get_data() == {
        "error": "No frame judge is available for this search."
    }


@pytest.mark.asyncio
async def test_scan_no_backend() -> None:
    context = _judging_context(FakeCameraBackend(), jev=_jev_by_bytes({}))
    context.plugins = None

    result = await scan_camera_frames_tool(
        context,
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=_SCAN_BASE.isoformat(),
        condition="A person is visible.",
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert "Camera backend not configured" in data["error"]


@pytest.mark.asyncio
async def test_find_change_narrows_to_the_precision_around_the_true_moment() -> None:
    # A parcel arrives at 12:47:30 and stays.
    backend = _continuous_camera({0: b"step"})
    arrival = _SCAN_BASE + timedelta(minutes=47, seconds=30)
    backend.set_frame("cam", arrival, b"parcel")
    jev = _jev_by_bytes({b"step": 0.03, b"parcel": 0.97})

    result = await find_camera_change_tool(
        _judging_context(backend, jev=jev),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(hours=2)).isoformat(),
        condition="A parcel is on the doorstep.",
        precision_seconds=30,
    )

    data = result.get_data()
    assert isinstance(data, dict)
    (change,) = data["changes"]
    before = datetime.fromisoformat(change["last_before"]["timestamp"])
    after = datetime.fromisoformat(change["first_after"]["timestamp"])
    assert (change["became"], change["resolved"]) == ("true", True)
    assert before < arrival <= after
    assert after - before <= timedelta(seconds=30)


@pytest.mark.asyncio
async def test_find_change_attaches_the_frames_either_side_of_it() -> None:
    backend = _continuous_camera({0: b"step"})
    backend.set_frame("cam", _SCAN_BASE + timedelta(minutes=47), b"parcel")
    jev = _jev_by_bytes({b"step": 0.03, b"parcel": 0.97})

    result = await find_camera_change_tool(
        _judging_context(backend, jev=jev),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(hours=2)).isoformat(),
        condition="A parcel is on the doorstep.",
    )

    assert result.attachments is not None
    assert [a.content for a in result.attachments] == [b"step", b"parcel"]


@pytest.mark.asyncio
async def test_find_change_during_a_recording_gap_stops_at_the_gap() -> None:
    backend = FakeCameraBackend()
    backend.add_camera("cam", "Camera")
    for start_minute, jpeg in ((0, b"step"), (60, b"parcel")):
        clip_start = _SCAN_BASE + timedelta(minutes=start_minute)
        backend.add_recording(
            Recording(
                camera_id="cam",
                start_time=clip_start,
                end_time=clip_start + timedelta(minutes=20),
            )
        )
        backend.set_frame("cam", clip_start, jpeg)
    jev = _jev_by_bytes({b"step": 0.03, b"parcel": 0.97})

    result = await find_camera_change_tool(
        _judging_context(backend, jev=jev),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(minutes=80)).isoformat(),
        condition="A parcel is on the doorstep.",
    )

    data = result.get_data()
    assert isinstance(data, dict)
    (change,) = data["changes"]
    assert change["resolved"] is False
    assert change["stopped_because"] == (
        "the camera was not recording between these frames"
    )


@pytest.mark.asyncio
async def test_find_change_says_when_the_condition_never_flips() -> None:
    backend = _continuous_camera({0: b"step"})

    result = await find_camera_change_tool(
        _judging_context(backend, jev=_jev_by_bytes({b"step": 0.03})),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=(_SCAN_BASE + timedelta(hours=1)).isoformat(),
        condition="A parcel is on the doorstep.",
    )

    assert result.text == (
        "No change found: 'A parcel is on the doorstep.' was false in all 12 "
        "frames judged across the range."
    )


@pytest.mark.asyncio
async def test_find_change_rejects_a_range_that_does_not_move_forward() -> None:
    backend = _continuous_camera({0: b"step"})

    result = await find_camera_change_tool(
        _judging_context(backend, jev=_jev_by_bytes({b"step": 0.03})),
        camera_id="cam",
        start_time=_SCAN_BASE.isoformat(),
        end_time=_SCAN_BASE.isoformat(),
        condition="A parcel is on the doorstep.",
    )

    assert result.get_data() == {"error": "end_time must be after start_time."}
