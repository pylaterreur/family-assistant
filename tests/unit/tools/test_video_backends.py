"""Tests for video generation backends and backend selection."""

from __future__ import annotations

import base64
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Literal, Self, cast
from unittest.mock import patch

import pytest
from google.genai import types as genai_types
from pydantic import SecretStr

from family_assistant.config_models import AppConfig
from family_assistant.tools.video_backends import (
    GeminiOmniVideoBackend,
    MockVideoBackend,
    VeoVideoBackend,
    VideoGenerationError,
    VideoGenerationRequest,
    VideoReferenceImage,
)
from family_assistant.tools.video_generation import (
    _create_video_backend,  # noqa: PLC2701 - testing internal backend selection
)
from tests.mocks.recording_async_call import RecordingAsyncCall

if TYPE_CHECKING:
    from collections.abc import Generator

    from family_assistant.tools.types import ToolExecutionContext

_BackendChoice = Literal["veo", "gemini_omni", "mock"]


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def _ctx_with_config(config: AppConfig | None) -> ToolExecutionContext:
    service = SimpleNamespace(app_config=config) if config is not None else None
    return cast("ToolExecutionContext", SimpleNamespace(processing_service=service))


@pytest.mark.parametrize(
    ("backend_choice", "expected"),
    [
        ("veo", VeoVideoBackend),
        ("gemini_omni", GeminiOmniVideoBackend),
        ("mock", MockVideoBackend),
    ],
)
def test_explicit_backend_choice(
    backend_choice: _BackendChoice, expected: type
) -> None:
    config = AppConfig(
        gemini_api_key=SecretStr("key"), video_generation_backend=backend_choice
    )
    backend = _create_video_backend(_ctx_with_config(config), model_override=None)
    assert isinstance(backend, expected)


def test_explicit_backend_requires_api_key() -> None:
    config = AppConfig(video_generation_backend="veo")
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="GEMINI_API_KEY"),
    ):
        _create_video_backend(_ctx_with_config(config), model_override=None)


def test_auto_select_defaults_to_gemini_omni() -> None:
    config = AppConfig(gemini_api_key=SecretStr("key"))
    backend = _create_video_backend(_ctx_with_config(config), model_override=None)
    assert isinstance(backend, GeminiOmniVideoBackend)
    assert backend.model == "gemini-omni-1.1-flash"


def test_auto_select_infers_omni_from_model() -> None:
    config = AppConfig(gemini_api_key=SecretStr("key"))
    backend = _create_video_backend(
        _ctx_with_config(config), model_override="gemini-omni-1.1-flash"
    )
    assert isinstance(backend, GeminiOmniVideoBackend)
    assert backend.model == "gemini-omni-1.1-flash"


def test_auto_select_infers_veo_from_model() -> None:
    config = AppConfig(gemini_api_key=SecretStr("key"))
    backend = _create_video_backend(
        _ctx_with_config(config), model_override="veo-3.1-fast"
    )
    assert isinstance(backend, VeoVideoBackend)
    assert backend.model == "veo-3.1-fast"


def test_auto_select_uses_veo_for_veo_only_features() -> None:
    config = AppConfig(gemini_api_key=SecretStr("key"))
    backend = _create_video_backend(
        _ctx_with_config(config),
        model_override=None,
        requires_veo_features=True,
    )
    assert isinstance(backend, VeoVideoBackend)
    assert backend.model == "veo-3.1-generate-preview"


def test_omni_model_override_remains_authoritative_for_veo_only_features() -> None:
    config = AppConfig(gemini_api_key=SecretStr("key"))
    backend = _create_video_backend(
        _ctx_with_config(config),
        model_override="gemini-omni-1.1-flash",
        requires_veo_features=True,
    )
    assert isinstance(backend, GeminiOmniVideoBackend)


def test_auto_select_falls_back_to_mock_without_key() -> None:
    config = AppConfig()
    with patch.dict("os.environ", {}, clear=True):
        backend = _create_video_backend(_ctx_with_config(config), model_override=None)
    assert isinstance(backend, MockVideoBackend)


def test_model_override_applies_to_configured_backend() -> None:
    config = AppConfig(
        gemini_api_key=SecretStr("key"), video_generation_backend="gemini_omni"
    )
    backend = _create_video_backend(
        _ctx_with_config(config), model_override="gemini-omni-flash-custom"
    )
    assert isinstance(backend, GeminiOmniVideoBackend)
    assert backend.model == "gemini-omni-flash-custom"


@pytest.mark.asyncio
async def test_mock_backend_returns_placeholder() -> None:
    backend = MockVideoBackend()
    result = await backend.generate_video(VideoGenerationRequest(prompt="hi"))
    assert result.content.startswith(b"\x00\x00\x00\x18ftyp")
    assert result.mime_type == "video/mp4"


# --------------------------------------------------------------------------- #
# Fake genai client
# --------------------------------------------------------------------------- #


class FakeGenaiClient:
    """Stands in for ``genai.Client`` and the async client its ``.aio`` yields.

    Each SDK coroutine the backends call is a ``RecordingAsyncCall``; a test
    scripts the ones it expects by replacing them, and any call it did not
    script fails.
    """

    def __init__(self) -> None:
        self.models = SimpleNamespace(generate_videos=RecordingAsyncCall[object]())
        self.operations = SimpleNamespace(get=RecordingAsyncCall[object]())
        self.files = SimpleNamespace(download=RecordingAsyncCall[bytes]())
        self.interactions = SimpleNamespace(
            create=RecordingAsyncCall[object](), get=RecordingAsyncCall[object]()
        )
        self.api_keys: list[str] = []
        self.open_sessions = 0

    def create(self, api_key: str) -> Self:
        self.api_keys.append(api_key)
        return self

    @property
    def aio(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        self.open_sessions += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.open_sessions -= 1


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.fixture
def fake_genai() -> Generator[FakeGenaiClient]:
    client = FakeGenaiClient()
    with (
        patch(
            "family_assistant.tools.video_backends._create_genai_client",
            new=client.create,
        ),
        patch("asyncio.sleep", new=_no_sleep),
    ):
        yield client


# --------------------------------------------------------------------------- #
# Veo backend
# --------------------------------------------------------------------------- #


def _veo_operation(
    *, name: str = "operation/123", done: bool = True
) -> genai_types.GenerateVideosOperation:
    return genai_types.GenerateVideosOperation.model_validate({
        "name": name,
        "done": done,
        "response": (
            genai_types.GenerateVideosResponse(
                generated_videos=[
                    genai_types.GeneratedVideo(
                        video=genai_types.Video(uri="files/file-ref")
                    )
                ]
            )
            if done
            else None
        ),
    })


@pytest.fixture
def veo_client(fake_genai: FakeGenaiClient) -> FakeGenaiClient:
    fake_genai.models.generate_videos = RecordingAsyncCall[object](_veo_operation())
    fake_genai.files.download = RecordingAsyncCall(b"video-content")
    return fake_genai


@pytest.mark.asyncio
async def test_veo_generate_returns_downloaded_bytes(
    veo_client: FakeGenaiClient,
) -> None:
    backend = VeoVideoBackend("key")
    result = await backend.generate_video(
        VideoGenerationRequest(prompt="A cat", duration_seconds=8)
    )

    assert result.content == b"video-content"
    assert result.mime_type == "video/mp4"
    assert result.model == "veo-3.1-generate-preview"

    assert veo_client.api_keys == ["key"]
    assert veo_client.open_sessions == 0
    kwargs = veo_client.models.generate_videos.last_call.kwargs
    assert kwargs["model"] == "veo-3.1-generate-preview"
    source = kwargs["source"]
    assert isinstance(source, genai_types.GenerateVideosSource)
    assert source.prompt == "A cat"
    assert [call.kwargs for call in veo_client.files.download.calls] == [
        {"file": genai_types.Video(uri="files/file-ref")}
    ]


@pytest.mark.asyncio
async def test_veo_forces_duration_with_reference_images(
    veo_client: FakeGenaiClient,
) -> None:
    backend = VeoVideoBackend("key")
    await backend.generate_video(
        VideoGenerationRequest(
            prompt="ref video",
            reference_images=[
                VideoReferenceImage(content=b"img", mime_type="image/png")
            ],
            first_frame=VideoReferenceImage(content=b"first", mime_type="image/jpeg"),
            last_frame=VideoReferenceImage(content=b"last", mime_type="image/jpeg"),
            duration_seconds=4,  # should be forced to 8
        )
    )

    kwargs = veo_client.models.generate_videos.last_call.kwargs
    config = kwargs["config"]
    assert isinstance(config, genai_types.GenerateVideosConfig)
    assert config.duration_seconds == 8
    assert config.reference_images is not None
    reference_image = config.reference_images[0].image
    assert reference_image is not None
    assert reference_image.image_bytes == b"img"
    assert config.last_frame is not None
    assert config.last_frame.image_bytes == b"last"
    source = kwargs["source"]
    assert isinstance(source, genai_types.GenerateVideosSource)
    assert source.image is not None
    assert source.image.image_bytes == b"first"


@pytest.mark.asyncio
async def test_veo_polls_until_done(veo_client: FakeGenaiClient) -> None:
    pending = _veo_operation(name="op/pending", done=False)
    veo_client.models.generate_videos = RecordingAsyncCall[object](pending)
    veo_client.operations.get = RecordingAsyncCall[object](pending, _veo_operation())

    backend = VeoVideoBackend("key")
    result = await backend.generate_video(VideoGenerationRequest(prompt="dog"))

    assert len(veo_client.operations.get.calls) == 2
    assert veo_client.operations.get.calls[0].args == (pending,)
    assert result.content == b"video-content"


@pytest.mark.asyncio
async def test_veo_raises_on_operation_error(veo_client: FakeGenaiClient) -> None:
    veo_client.models.generate_videos = RecordingAsyncCall[object](
        genai_types.GenerateVideosOperation.model_validate({
            "name": "operation/123",
            "done": True,
            "error": {"message": "Safety violation", "code": 400},
        })
    )

    backend = VeoVideoBackend("key")
    with pytest.raises(VideoGenerationError, match=r"Safety violation \(code: 400\)"):
        await backend.generate_video(VideoGenerationRequest(prompt="unsafe"))


@pytest.mark.asyncio
async def test_veo_raises_on_timeout(veo_client: FakeGenaiClient) -> None:
    veo_client.models.generate_videos = RecordingAsyncCall[object](
        _veo_operation(name="op/pending", done=False)
    )

    backend = VeoVideoBackend("key")
    with (
        patch(
            "family_assistant.tools.video_backends.time.time",
            new=iter([1000.0, 1700.0]).__next__,
        ),
        pytest.raises(VideoGenerationError, match="timed out"),
    ):
        await backend.generate_video(VideoGenerationRequest(prompt="slow"))


# --------------------------------------------------------------------------- #
# Gemini Omni Flash backend
# --------------------------------------------------------------------------- #


def _video_output(data: bytes) -> SimpleNamespace:
    """A completed interaction's ``output_video`` block (inline base64 data)."""
    return SimpleNamespace(
        data=base64.b64encode(data).decode("ascii"),
        uri=None,
        mime_type="video/mp4",
    )


def _omni_interaction(
    status: str = "completed", output_video: SimpleNamespace | None = None
) -> SimpleNamespace:
    return SimpleNamespace(id="interaction-1", status=status, output_video=output_video)


@pytest.fixture
def omni_client(fake_genai: FakeGenaiClient) -> FakeGenaiClient:
    fake_genai.interactions.create = RecordingAsyncCall[object](
        _omni_interaction(output_video=_video_output(b"omni-video"))
    )
    return fake_genai


@pytest.mark.asyncio
async def test_omni_returns_video_bytes(omni_client: FakeGenaiClient) -> None:
    backend = GeminiOmniVideoBackend("key")
    result = await backend.generate_video(
        VideoGenerationRequest(prompt="a dancing robot", aspect_ratio="9:16")
    )

    assert result.content == b"omni-video"
    assert result.mime_type == "video/mp4"
    assert result.model == "gemini-omni-1.1-flash"

    kwargs = omni_client.interactions.create.last_call.kwargs
    assert kwargs["model"] == "gemini-omni-1.1-flash"
    assert kwargs["input"] == "a dancing robot"
    assert kwargs["response_format"] == {
        "type": "video",
        "delivery": "uri",
        "aspect_ratio": "9:16",
    }


@pytest.mark.asyncio
async def test_omni_passes_duration_to_response_format(
    omni_client: FakeGenaiClient,
) -> None:
    backend = GeminiOmniVideoBackend("key")
    await backend.generate_video(
        VideoGenerationRequest(prompt="a quick clip", duration_seconds=3)
    )

    kwargs = omni_client.interactions.create.last_call.kwargs
    response_format = kwargs["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["duration"] == "3s"


@pytest.mark.asyncio
async def test_omni_builds_image_input_blocks(omni_client: FakeGenaiClient) -> None:
    backend = GeminiOmniVideoBackend("key")
    await backend.generate_video(
        VideoGenerationRequest(
            prompt="animate this",
            first_frame=VideoReferenceImage(content=b"first", mime_type="image/jpeg"),
            reference_images=[
                VideoReferenceImage(content=b"ref", mime_type="image/png")
            ],
        )
    )

    blocks = omni_client.interactions.create.last_call.kwargs["input"]
    assert blocks == [
        {
            "type": "image",
            "data": base64.b64encode(b"first").decode("ascii"),
            "mime_type": "image/jpeg",
        },
        {
            "type": "image",
            "data": base64.b64encode(b"ref").decode("ascii"),
            "mime_type": "image/png",
        },
        {"type": "text", "text": "animate this"},
    ]


@pytest.mark.asyncio
async def test_omni_polls_until_completed(omni_client: FakeGenaiClient) -> None:
    omni_client.interactions.create = RecordingAsyncCall[object](
        _omni_interaction(status="in_progress")
    )
    omni_client.interactions.get = RecordingAsyncCall[object](
        _omni_interaction(status="in_progress"),
        _omni_interaction(output_video=_video_output(b"polled-video")),
    )

    backend = GeminiOmniVideoBackend("key")
    result = await backend.generate_video(VideoGenerationRequest(prompt="slow video"))

    assert result.content == b"polled-video"
    assert [call.args for call in omni_client.interactions.get.calls] == [
        ("interaction-1",),
        ("interaction-1",),
    ]


@pytest.mark.asyncio
async def test_omni_downloads_uri_when_no_inline_data(
    omni_client: FakeGenaiClient,
) -> None:
    uri_output = SimpleNamespace(
        data=None, uri="https://files/video", mime_type="video/mp4"
    )
    omni_client.interactions.create = RecordingAsyncCall[object](
        _omni_interaction(output_video=uri_output)
    )
    omni_client.files.download = RecordingAsyncCall(b"uri-video")

    backend = GeminiOmniVideoBackend("key")
    result = await backend.generate_video(VideoGenerationRequest(prompt="big video"))

    assert result.content == b"uri-video"
    assert [call.kwargs for call in omni_client.files.download.calls] == [
        {"file": "https://files/video"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "video_request",
    [
        VideoGenerationRequest(
            prompt="interpolate",
            last_frame=VideoReferenceImage(content=b"last"),
        ),
        VideoGenerationRequest(prompt="exclude rain", negative_prompt="rain"),
    ],
)
async def test_omni_rejects_unsupported_veo_features(
    omni_client: FakeGenaiClient, video_request: VideoGenerationRequest
) -> None:
    backend = GeminiOmniVideoBackend("key")

    with pytest.raises(VideoGenerationError, match="use a Veo model"):
        await backend.generate_video(video_request)

    assert omni_client.interactions.create.calls == []


@pytest.mark.asyncio
async def test_omni_raises_when_no_video_output(omni_client: FakeGenaiClient) -> None:
    omni_client.interactions.create = RecordingAsyncCall[object](
        _omni_interaction(output_video=None)
    )

    backend = GeminiOmniVideoBackend("key")
    with pytest.raises(VideoGenerationError, match="No video content"):
        await backend.generate_video(VideoGenerationRequest(prompt="text only"))


@pytest.mark.asyncio
async def test_omni_raises_on_failed_status(omni_client: FakeGenaiClient) -> None:
    omni_client.interactions.create = RecordingAsyncCall[object](
        _omni_interaction(status="failed")
    )

    backend = GeminiOmniVideoBackend("key")
    with pytest.raises(VideoGenerationError, match="status 'failed'"):
        await backend.generate_video(VideoGenerationRequest(prompt="doomed"))
