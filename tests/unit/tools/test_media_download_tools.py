"""Tests for media download tools."""

from collections.abc import Generator
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from yt_dlp.utils import DownloadError

from family_assistant.config_models import AppConfig, AttachmentConfig
from family_assistant.tools.media_download import (
    MEDIA_DOWNLOAD_TOOLS_DEFINITION,
    _sanitize_title,  # noqa: PLC2701 - unit tests need direct access to internal helpers
    download_media_tool,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)
from tests.mocks.idle_processing_service import make_idle_processing_service
from tests.mocks.unconnected_database import unconnected_database


@dataclass
class _Download:
    """A file the fake downloader writes into its requested destination."""

    filename: str
    content: bytes
    info: dict[str, object]
    include_filepath: bool = True


class FakeYoutubeDL:
    """Stands in for ``yt_dlp.YoutubeDL`` for one extraction."""

    def __init__(self, fake: "FakeYtDlp", options: dict[str, object]) -> None:
        self._fake = fake
        self._options = options

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> dict[str, object] | None:
        self._fake.extract_calls.append((url, download))
        if self._fake.error is not None:
            raise self._fake.error
        spec = self._fake.download
        if not download:
            assert spec is None, "a download was configured but not requested"
            return self._fake.metadata
        assert spec is not None, "test did not configure a download"
        output_dir = Path(cast("str", self._options["outtmpl"])).parent
        file_path = output_dir / spec.filename
        file_path.write_bytes(spec.content)
        if spec.include_filepath:
            return {**spec.info, "requested_downloads": [{"filepath": str(file_path)}]}
        return spec.info


class FakeYtDlp:
    """Stands in for the ``yt_dlp`` module, recording every extraction."""

    def __init__(self) -> None:
        self.metadata: dict[str, object] | None = None
        self.download: _Download | None = None
        self.error: Exception | None = None
        self.extract_calls: list[tuple[str, bool]] = []

    def YoutubeDL(self, options: dict[str, object]) -> FakeYoutubeDL:
        return FakeYoutubeDL(self, options)


@pytest.fixture
def fake_yt_dlp() -> Generator[FakeYtDlp]:
    fake = FakeYtDlp()
    with patch("family_assistant.tools.media_download.yt_dlp", new=fake):
        yield fake


def _exec_context(app_config: AppConfig | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        conversation_id="test-conv",
        user_name="test-user",
        interface_type="test",
        timezone=ZoneInfo("UTC"),
        turn_id=None,
        db_context=unconnected_database(),
        processing_service=(
            make_idle_processing_service(app_config=app_config)
            if app_config is not None
            else None
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
    return _exec_context()


def test_tool_definition_structure() -> None:
    """The advertised arguments match the callable tool interface."""
    assert len(MEDIA_DOWNLOAD_TOOLS_DEFINITION) == 1
    tool_def = MEDIA_DOWNLOAD_TOOLS_DEFINITION[0]

    assert tool_def["type"] == "function"
    assert tool_def["function"]["name"] == "download_media"
    assert "description" in tool_def["function"]

    # Cast to dict for test assertions on optional TypedDict keys
    params = cast("dict[str, Any]", tool_def["function"]["parameters"])
    assert params["type"] == "object"
    assert "url" in params["properties"]
    assert "audio_only" in params["properties"]
    assert "metadata_only" in params["properties"]
    assert params["required"] == ["url"]
    callable_parameters = signature(download_media_tool).parameters
    assert set(params["properties"]) == set(callable_parameters) - {"exec_context"}
    assert set(params["required"]) == {
        name
        for name, parameter in callable_parameters.items()
        if name != "exec_context" and parameter.default is parameter.empty
    }


@pytest.mark.asyncio
async def test_download_media_metadata_only(
    exec_context: ToolExecutionContext, fake_yt_dlp: FakeYtDlp
) -> None:
    """Test metadata extraction without downloading."""
    fake_yt_dlp.metadata = {
        "title": "Test Video",
        "duration": 125,
        "uploader": "Test Uploader",
        "upload_date": "20240101",
        "view_count": 1000,
        "description": "Test description",
        "thumbnail": "https://example.com/thumb.jpg",
        "webpage_url": "https://example.com/video",
        "extractor": "generic",
    }

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert "metadata" in result.data

    metadata = result.data["metadata"]
    assert metadata["title"] == "Test Video"
    assert metadata["duration"] == 125
    assert metadata["uploader"] == "Test Uploader"

    # Verify extract_info was called without download
    assert fake_yt_dlp.extract_calls == [("https://example.com/video", False)]


@pytest.mark.asyncio
async def test_download_media_metadata_format_duration(
    exec_context: ToolExecutionContext, fake_yt_dlp: FakeYtDlp
) -> None:
    """Test that duration is formatted correctly in text output."""
    # Test hours
    fake_yt_dlp.metadata = {
        "title": "Long Video",
        "duration": 3725,  # 1h 2m 5s
        "uploader": "Test",
        "extractor": "youtube",
    }

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert result.text is not None
    assert "1h 2m 5s" in result.text


@pytest.mark.asyncio
async def test_download_media_metadata_no_info(
    exec_context: ToolExecutionContext, fake_yt_dlp: FakeYtDlp
) -> None:
    """Test handling when extract_info returns None."""
    fake_yt_dlp.metadata = None

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["error_type"] == "metadata_extraction_failed"
    assert result.text is not None
    assert "Could not extract metadata" in result.text


@pytest.mark.asyncio
async def test_download_media_metadata_extraction_error(
    exec_context: ToolExecutionContext, fake_yt_dlp: FakeYtDlp
) -> None:
    """An extractor ValueError is reported as a metadata failure."""
    fake_yt_dlp.error = ValueError("Could not extract metadata from URL")

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
        metadata_only=True,
    )

    assert isinstance(result.data, dict)
    assert result.data["error_type"] == "metadata_extraction_failed"
    assert "Could not extract metadata from URL" in result.get_text()
    assert fake_yt_dlp.extract_calls == [("https://example.com/video", False)]


@pytest.mark.asyncio
async def test_download_media_video_success(
    exec_context: ToolExecutionContext,
    fake_yt_dlp: FakeYtDlp,
) -> None:
    """Test successful video download."""
    test_content = b"fake video content"
    fake_yt_dlp.download = _Download(
        "Test Video.mp4",
        test_content,
        {
            "title": "Test Video",
            "duration": 60,
            "uploader": "Test Uploader",
            "upload_date": "20240101",
            "webpage_url": "https://example.com/video",
            "extractor": "youtube",
        },
    )

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.data["title"] == "Test Video"
    assert result.data["mime_type"] == "video/mp4"

    # Verify attachment
    assert result.attachments is not None
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert isinstance(attachment, ToolAttachment)
    assert attachment.content == test_content
    assert attachment.mime_type == "video/mp4"


@pytest.mark.asyncio
async def test_download_media_audio_only(
    exec_context: ToolExecutionContext,
    fake_yt_dlp: FakeYtDlp,
) -> None:
    """Test audio-only download."""
    fake_yt_dlp.download = _Download(
        "Test Audio.m4a",
        b"fake audio content",
        {
            "title": "Test Audio",
            "duration": 180,
            "uploader": "Test Uploader",
            "upload_date": "20240101",
            "webpage_url": "https://example.com/video",
            "extractor": "youtube",
        },
    )

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
        audio_only=True,
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.data["mime_type"] == "audio/mp4"

    # Verify attachment
    assert result.attachments is not None
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.mime_type == "audio/mp4"
    assert "Audio" in attachment.description


@pytest.mark.asyncio
async def test_download_media_file_too_large(fake_yt_dlp: FakeYtDlp) -> None:
    """Test handling of files exceeding size limit."""
    exec_context = _exec_context(
        AppConfig(
            attachment_config=AttachmentConfig(max_file_size=50, max_multimodal_size=20)
        )
    )
    fake_yt_dlp.download = _Download(
        "Large Video.mp4",
        b"x" * 100,
        {
            "title": "Large Video",
            "duration": 3600,
        },
    )

    result = await download_media_tool(
        exec_context,
        url="https://example.com/large-video",
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data.get("error") == "file_too_large"
    assert result.data.get("max_size") == 50


@pytest.mark.asyncio
async def test_download_media_download_error(
    exec_context: ToolExecutionContext,
    fake_yt_dlp: FakeYtDlp,
) -> None:
    """Test handling of yt-dlp download errors."""
    fake_yt_dlp.error = DownloadError("Video unavailable")

    result = await download_media_tool(
        exec_context,
        url="https://example.com/unavailable",
    )

    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data.get("error") == "download_failed"
    assert "unavailable" in result.data.get("message", "").lower()


@pytest.mark.asyncio
async def test_download_media_fallback_file_search(
    exec_context: ToolExecutionContext,
    fake_yt_dlp: FakeYtDlp,
) -> None:
    """Test fallback file search when filepath is not in requested_downloads."""
    test_content = b"video content"
    fake_yt_dlp.download = _Download(
        "actual_video.mp4",
        test_content,
        {
            "title": "Test Video",
            "duration": 60,
            "ext": "mp4",
        },
        include_filepath=False,
    )

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
    )

    # Should find the mp4 file via glob fallback
    assert isinstance(result, ToolResult)
    assert result.data is not None
    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.attachments is not None
    assert result.attachments[0].content == test_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ext", "expected_mime"),
    [
        (".mp4", "video/mp4"),
        (".m4a", "audio/mp4"),
        (".webm", "video/webm"),
        (".mkv", "video/x-matroska"),
        (".mp3", "audio/mpeg"),
        (".opus", "audio/opus"),
    ],
)
async def test_download_media_various_formats(
    exec_context: ToolExecutionContext,
    fake_yt_dlp: FakeYtDlp,
    ext: str,
    expected_mime: str,
) -> None:
    """Test MIME type detection for various formats."""
    fake_yt_dlp.download = _Download(
        f"test{ext}",
        b"content",
        {
            "title": "Test",
            "duration": 60,
        },
    )

    result = await download_media_tool(
        exec_context,
        url="https://example.com/video",
    )

    assert isinstance(result.data, dict)
    assert result.data["mime_type"] == expected_mime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://ftp.example.com/file.mp4",
        "example.com/video",
        "https:///path/only",
    ],
)
async def test_download_media_rejects_invalid_url(
    exec_context: ToolExecutionContext, fake_yt_dlp: FakeYtDlp, url: str
) -> None:
    """Invalid URLs are rejected before reaching the downloader."""
    result = await download_media_tool(exec_context, url=url)

    assert isinstance(result.data, dict)
    assert result.data["error"] == "invalid_url"
    assert result.data["error_type"] == "validation_failed"
    assert fake_yt_dlp.extract_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["https://example.com/video", "http://example.com/video"]
)
async def test_download_media_accepts_http_urls(
    exec_context: ToolExecutionContext, fake_yt_dlp: FakeYtDlp, url: str
) -> None:
    """HTTP and HTTPS URLs reach the downloader."""
    fake_yt_dlp.metadata = {"title": "Video"}
    result = await download_media_tool(exec_context, url=url, metadata_only=True)

    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert fake_yt_dlp.extract_calls == [(url, False)]


# Filename sanitization tests


def test_sanitize_title_normal_title() -> None:
    """Test sanitization of normal titles."""
    result = _sanitize_title("My Video Title")
    assert result == "My Video Title"


def test_sanitize_title_with_special_chars() -> None:
    """Test sanitization removes dangerous characters."""
    result = _sanitize_title("Video: Test / Something")
    # Should not contain : or /
    assert "/" not in result
    assert ":" not in result
    assert result  # Should not be empty


def test_sanitize_title_consecutive_dots() -> None:
    """Test that consecutive dots are collapsed."""
    result = _sanitize_title("Video...Title")
    assert ".." not in result


def test_sanitize_title_leading_trailing_dots() -> None:
    """Test that leading/trailing dots are removed."""
    result = _sanitize_title("...hidden")
    assert not result.startswith(".")

    result = _sanitize_title("file...")
    assert not result.endswith(".")


def test_sanitize_title_empty_after_sanitization() -> None:
    """Test that empty titles fallback to 'download'."""
    result = _sanitize_title("///")
    assert result == "download"
