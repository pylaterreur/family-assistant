"""Tests for image generation tools."""

import base64
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import patch

import httpx
import openai
import pytest
from google.genai import types as genai_types
from openai.types import Image as OpenAIImage
from openai.types import ImagesResponse
from openai.types.images_response import Usage, UsageInputTokensDetails
from PIL import Image
from pydantic import SecretStr

from family_assistant.config_models import (
    AppConfig,
    OpenAIImageConfig,
    OpenAIImageRequestConfig,
)
from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.services.attachment_registry import AttachmentMetadata
from family_assistant.tools.image_backends import (
    GeminiImageBackend,
    ImageGenerationBackend,
    ImageReference,
    ImageReferenceInputs,
    MockImageBackend,
    OpenAIImageBackend,
)
from family_assistant.tools.image_generation import (
    generate_image_tool,
    transform_image_tool,
)
from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.database import Database
    from family_assistant.tools.types import ToolExecutionContext


@dataclass
class _FakeProcessingService:
    """Carries the app config the image tools read to choose a backend."""

    app_config: AppConfig


class _FakeExecContext:
    """Carries only the execution-context fields the image tools read."""

    def __init__(
        self,
        *,
        app_config: AppConfig | None = None,
        backend: ImageGenerationBackend | None = None,
    ) -> None:
        self.processing_service = (
            _FakeProcessingService(app_config) if app_config is not None else None
        )
        if backend is not None:
            self.image_backend = backend


def _exec_context(
    *,
    app_config: AppConfig | None = None,
    backend: ImageGenerationBackend | None = None,
) -> "ToolExecutionContext":
    return cast(
        "ToolExecutionContext",
        _FakeExecContext(app_config=app_config, backend=backend),
    )


class _FakeAttachmentRegistry:
    """Serves attachment content from memory and records every fetch."""

    def __init__(self) -> None:
        self._contents: dict[str, bytes | Exception] = {}
        self.fetches: list[str] = []

    def add(self, attachment_id: str, content: bytes | Exception) -> None:
        self._contents[attachment_id] = content

    async def get_attachment_content(
        self,
        db_context: object,
        attachment_id: str,
        *,
        acting_user_id: str | None,
    ) -> bytes | None:
        self.fetches.append(attachment_id)
        content = self._contents[attachment_id]
        if isinstance(content, Exception):
            raise content
        return content


def _script_attachment(
    registry: _FakeAttachmentRegistry,
    attachment_id: str,
    content: bytes | Exception,
    mime_type: str = "image/png",
) -> ScriptAttachment:
    registry.add(attachment_id, content)
    metadata = AttachmentMetadata(
        attachment_id=attachment_id,
        source_type="user",
        source_id="user-1",
        mime_type=mime_type,
        description="Test image",
        size=len(content) if isinstance(content, bytes) else 0,
    )
    return ScriptAttachment(
        metadata=metadata,
        registry=cast("AttachmentRegistry", registry),
        db_context_getter=lambda: cast("Database", None),
        user_id="user-1",
    )


class _RecordingImageBackend:
    """Image backend that records transform requests and returns a fixed PNG."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.transform_requests: list[tuple[ImageReferenceInputs, str]] = []

    async def generate_image(self, prompt: str, style: str = "auto") -> bytes:
        if self._error is not None:
            raise self._error
        return _make_png_bytes()

    async def transform_image(
        self, image_bytes: ImageReferenceInputs, instruction: str
    ) -> bytes:
        self.transform_requests.append((image_bytes, instruction))
        if self._error is not None:
            raise self._error
        return _make_png_bytes()


def _mock_backend(*_args: object, **_kwargs: object) -> MockImageBackend:
    return MockImageBackend()


class _FakeImagesEndpoint:
    """Stands in for an OpenAI images method, replaying outcomes in order."""

    def __init__(self, *outcomes: ImagesResponse | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> ImagesResponse:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeGenerateContent:
    """Stands in for the genai async generate_content method."""

    def __init__(self, response: genai_types.GenerateContentResponse) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> genai_types.GenerateContentResponse:
        self.calls.append(kwargs)
        return self._response


def _images_response(
    data: list[OpenAIImage] | None, usage: Usage | None = None
) -> ImagesResponse:
    return ImagesResponse(created=0, data=data, usage=usage)


def _uploaded_files(call: dict[str, object]) -> list[io.BytesIO]:
    image = call["image"]
    files = cast("list[object]", image) if isinstance(image, list) else [image]
    assert all(isinstance(file, io.BytesIO) for file in files)
    return cast("list[io.BytesIO]", files)


class TestMockImageBackend:
    """Test the mock image backend."""

    @pytest.fixture
    def mock_backend(self) -> MockImageBackend:
        """Create a mock backend instance."""
        return MockImageBackend()

    @pytest.mark.asyncio
    async def test_generate_image_basic(self, mock_backend: MockImageBackend) -> None:
        """Test basic image generation."""
        image_bytes = await mock_backend.generate_image("test prompt", "auto")

        # Verify we got valid PNG bytes
        assert isinstance(image_bytes, bytes)
        assert len(image_bytes) > 0

        # Verify it's a valid image
        img = Image.open(io.BytesIO(image_bytes))
        assert img.format == "PNG"
        assert img.size == (512, 512)

    @pytest.mark.asyncio
    async def test_generate_image_with_keywords(
        self, mock_backend: MockImageBackend
    ) -> None:
        """Test image generation background colour reflects prompt keywords."""
        sunset_bytes = await mock_backend.generate_image("colorful sunset", "auto")
        sunset_img = Image.open(io.BytesIO(sunset_bytes)).convert("RGB")

        night_bytes = await mock_backend.generate_image(
            "a quiet scene at night", "auto"
        )
        night_img = Image.open(io.BytesIO(night_bytes)).convert("RGB")
        sunset_background = sunset_img.getpixel((
            sunset_img.width // 2,
            sunset_img.height * 3 // 4,
        ))
        night_background = night_img.getpixel((
            night_img.width // 2,
            night_img.height * 3 // 4,
        ))
        assert sunset_background != night_background

    @pytest.mark.asyncio
    async def test_transform_image_basic(self, mock_backend: MockImageBackend) -> None:
        """Test basic image transformation."""
        # Create a test image first
        original_bytes = await mock_backend.generate_image("test image", "auto")

        # Transform it
        transformed_bytes = await mock_backend.transform_image(
            original_bytes, "make it darker"
        )

        # Verify transformation
        assert isinstance(transformed_bytes, bytes)
        assert len(transformed_bytes) > 0
        assert transformed_bytes != original_bytes  # Should be different

    @pytest.mark.asyncio
    async def test_transform_image_grayscale(
        self, mock_backend: MockImageBackend
    ) -> None:
        """Test grayscale transformation actually desaturates the image."""
        original_bytes = await mock_backend.generate_image("colorful sunset", "auto")
        original_img = Image.open(io.BytesIO(original_bytes)).convert("RGB")
        sample = (original_img.width // 2, original_img.height * 3 // 4)
        original_pixel = original_img.getpixel(sample)
        assert isinstance(original_pixel, tuple)
        assert len(set(original_pixel)) > 1

        transformed_bytes = await mock_backend.transform_image(
            original_bytes, "convert to black and white"
        )
        transformed_img = Image.open(io.BytesIO(transformed_bytes)).convert("RGB")

        pixel = transformed_img.getpixel(sample)
        assert isinstance(pixel, tuple)
        r, g, b = pixel
        assert r == g == b


class TestImageGenerationTools:
    """Test the actual tool functions."""

    @pytest.fixture
    def exec_context(self) -> "ToolExecutionContext":
        """Create an execution context with an injected mock backend."""
        return _exec_context(backend=MockImageBackend())

    @pytest.fixture
    def attachment_registry(self) -> _FakeAttachmentRegistry:
        return _FakeAttachmentRegistry()

    @pytest.fixture
    async def script_attachment(
        self, attachment_registry: _FakeAttachmentRegistry
    ) -> ScriptAttachment:
        """Create a ScriptAttachment holding a generated test image."""
        test_image_bytes = await MockImageBackend().generate_image(
            "test attachment image", "auto"
        )
        return _script_attachment(
            attachment_registry, "test-attachment-id", test_image_bytes
        )

    @pytest.mark.asyncio
    async def test_generate_image_tool_basic(
        self, exec_context: "ToolExecutionContext"
    ) -> None:
        """Test basic image generation."""
        result = await generate_image_tool(
            exec_context, prompt="a beautiful landscape", style="auto"
        )

        assert isinstance(result, ToolResult)
        assert "Generated image for: a beautiful landscape" in result.get_text()
        assert result.attachments and len(result.attachments) > 0
        assert isinstance(result.attachments[0], ToolAttachment)
        assert result.attachments[0].mime_type == "image/png"
        assert result.attachments[0].content is not None
        assert len(result.attachments[0].content) > 0

    @pytest.mark.asyncio
    async def test_generate_image_tool_photorealistic(
        self, exec_context: "ToolExecutionContext"
    ) -> None:
        """Test photorealistic image generation."""
        result = await generate_image_tool(
            exec_context, prompt="a city skyline", style="photorealistic"
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        assert "Generated image: a city skyline" in result.attachments[0].description

    @pytest.mark.asyncio
    async def test_generate_image_tool_long_prompt(
        self, exec_context: "ToolExecutionContext"
    ) -> None:
        """Test generation with a long prompt."""
        long_prompt = "a very detailed and elaborate description of a fantasy landscape with mountains, rivers, castles, and magical creatures"

        result = await generate_image_tool(
            exec_context, prompt=long_prompt, style="auto"
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        # Description should be truncated with ellipsis
        assert (
            len(result.attachments[0].description) <= 70
        )  # "Generated image: " + truncated prompt + "..."

    @pytest.mark.asyncio
    async def test_transform_image_tool_basic(
        self,
        exec_context: "ToolExecutionContext",
        attachment_registry: _FakeAttachmentRegistry,
        script_attachment: ScriptAttachment,
    ) -> None:
        """Test basic image transformation."""
        result = await transform_image_tool(
            exec_context,
            image=script_attachment,
            instruction="make it brighter",
        )

        assert isinstance(result, ToolResult)
        assert "Transformed image: make it brighter" in result.get_text()
        assert result.attachments and len(result.attachments) > 0
        assert isinstance(result.attachments[0], ToolAttachment)
        assert result.attachments[0].mime_type == "image/png"
        assert result.attachments[0].content is not None

        assert attachment_registry.fetches == ["test-attachment-id"]

    @pytest.mark.asyncio
    async def test_transform_image_tool_multiple_references(
        self, attachment_registry: _FakeAttachmentRegistry
    ) -> None:
        """Test transformation with multiple reference images."""
        backend = _RecordingImageBackend()
        exec_context = _exec_context(backend=backend)

        first_attachment = _script_attachment(
            attachment_registry,
            "first-attachment",
            b"first image content",
            mime_type="image/jpeg",
        )
        second_attachment = _script_attachment(
            attachment_registry,
            "second-attachment",
            b"second image content",
            mime_type="image/webp",
        )

        result = await transform_image_tool(
            exec_context,
            images=[first_attachment, second_attachment],
            instruction="combine these into one image",
        )

        assert isinstance(result, ToolResult)
        assert "Transformed image: combine these into one image" in result.get_text()
        assert result.attachments and len(result.attachments) > 0
        assert backend.transform_requests == [
            (
                [
                    ImageReference(b"first image content", "image/jpeg"),
                    ImageReference(b"second image content", "image/webp"),
                ],
                "combine these into one image",
            )
        ]
        assert attachment_registry.fetches == [
            "first-attachment",
            "second-attachment",
        ]

    @pytest.mark.asyncio
    async def test_transform_image_tool_requires_image(
        self, exec_context: "ToolExecutionContext"
    ) -> None:
        """Test transformation requires at least one image attachment."""
        result = await transform_image_tool(
            exec_context, instruction="make it brighter"
        )

        assert isinstance(result, ToolResult)
        assert "Provide at least one image to transform" in result.get_text()
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_grayscale(
        self,
        exec_context: "ToolExecutionContext",
        script_attachment: ScriptAttachment,
    ) -> None:
        """Test grayscale transformation."""
        result = await transform_image_tool(
            exec_context,
            image=script_attachment,
            instruction="convert to black and white",
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        assert (
            "Transformed: convert to black and white"
            in result.attachments[0].description
        )

    @pytest.mark.asyncio
    async def test_transform_image_tool_no_content(
        self,
        exec_context: "ToolExecutionContext",
        attachment_registry: _FakeAttachmentRegistry,
    ) -> None:
        """Test transformation when attachment has no content."""
        attachment = _script_attachment(attachment_registry, "empty-attachment", b"")

        result = await transform_image_tool(
            exec_context, image=attachment, instruction="transform this"
        )

        assert isinstance(result, ToolResult)
        assert "Could not access the image content" in result.get_text()
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_long_instruction(
        self,
        exec_context: "ToolExecutionContext",
        script_attachment: ScriptAttachment,
    ) -> None:
        """Test transformation with long instruction."""
        long_instruction = "transform this image into a magnificent fantasy landscape with dragons, castles, magic, and beautiful colors"

        result = await transform_image_tool(
            exec_context,
            image=script_attachment,
            instruction=long_instruction,
        )

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0
        # Description should be truncated
        assert (
            len(result.attachments[0].description) <= 70
        )  # "Transformed: " + truncated instruction + "..."

    @pytest.mark.asyncio
    async def test_generate_image_tool_error_handling(self) -> None:
        """Test error handling in image generation."""
        exec_context = _exec_context(
            backend=_RecordingImageBackend(error=Exception("Test error"))
        )

        result = await generate_image_tool(
            exec_context, prompt="test prompt", style="auto"
        )

        assert isinstance(result, ToolResult)
        assert "Error generating image: Test error" in result.get_text()
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_error_handling(
        self,
        exec_context: "ToolExecutionContext",
        attachment_registry: _FakeAttachmentRegistry,
    ) -> None:
        """Test error handling in image transformation."""
        attachment = _script_attachment(
            attachment_registry, "error-attachment", Exception("Attachment error")
        )

        result = await transform_image_tool(
            exec_context, image=attachment, instruction="transform this"
        )

        assert isinstance(result, ToolResult)
        assert (
            "Error transforming image: Failed to get attachment content: "
            "Attachment error" in result.get_text()
        )
        assert not result.attachments or len(result.attachments) == 0

    @pytest.mark.asyncio
    async def test_transform_image_tool_backend_error_handling(
        self, attachment_registry: _FakeAttachmentRegistry
    ) -> None:
        """Test error handling in backend transformation."""
        backend = _RecordingImageBackend(error=Exception("Backend error"))
        exec_context = _exec_context(backend=backend)
        attachment = _script_attachment(
            attachment_registry, "test-attachment", b"fake image content"
        )

        result = await transform_image_tool(
            exec_context, image=attachment, instruction="transform this"
        )

        assert isinstance(result, ToolResult)
        assert "Error transforming image: Backend error" in result.get_text()
        assert not result.attachments or len(result.attachments) == 0
        assert backend.transform_requests == [
            (
                [ImageReference(b"fake image content", "image/png")],
                "transform this",
            )
        ]


def _make_png_b64() -> str:
    """Create a minimal valid PNG and return as base64."""
    img = Image.new("RGB", (64, 64), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _make_app_config(
    *,
    image_generation_backend: Literal["openai", "gemini", "mock"] | None,
    openai_api_key: str | None = None,
    gemini_api_key: str | None = None,
    openai_image: OpenAIImageConfig | None = None,
) -> AppConfig:
    return AppConfig(
        image_generation_backend=image_generation_backend,
        openai_api_key=SecretStr(openai_api_key) if openai_api_key else None,
        gemini_api_key=SecretStr(gemini_api_key) if gemini_api_key else None,
        openai_image=openai_image or OpenAIImageConfig(),
    )


class TestOpenAIImageBackend:
    """Test the OpenAI image backend."""

    @pytest.fixture
    def openai_backend(self) -> OpenAIImageBackend:
        """Create an OpenAI backend with a dummy key."""
        return OpenAIImageBackend(
            api_key="test-key",
            generate_config=OpenAIImageRequestConfig(),
            edit_config=OpenAIImageRequestConfig(),
        )

    @pytest.mark.asyncio
    async def test_generate_image(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test image generation calls the OpenAI API correctly."""
        generate = _FakeImagesEndpoint(
            _images_response(
                [OpenAIImage(b64_json=_make_png_b64())],
                usage=Usage(
                    input_tokens=10,
                    output_tokens=100,
                    total_tokens=110,
                    input_tokens_details=UsageInputTokensDetails(
                        image_tokens=0, text_tokens=10
                    ),
                ),
            )
        )
        monkeypatch.setattr(openai_backend.client.images, "generate", generate)

        result = await openai_backend.generate_image("a red square", "photorealistic")

        assert isinstance(result, bytes)
        assert len(result) > 0
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"

        assert len(generate.calls) == 1
        call_kwargs = generate.calls[0]
        assert call_kwargs["model"] == "gpt-image-2.5-sunburst"
        prompt = call_kwargs["prompt"]
        assert isinstance(prompt, str)
        assert "photorealistic" in prompt
        assert "a red square" in prompt
        assert call_kwargs["quality"] == "high"

    @pytest.mark.asyncio
    async def test_generate_image_auto_style_passes_prompt_through(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that auto style passes the prompt through unchanged."""
        generate = _FakeImagesEndpoint(
            _images_response([OpenAIImage(b64_json=_make_png_b64())])
        )
        monkeypatch.setattr(openai_backend.client.images, "generate", generate)

        await openai_backend.generate_image("test", "auto")

        assert generate.calls[0]["prompt"] == "test"

    @pytest.mark.asyncio
    async def test_generate_image_no_data_raises(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test error when API returns no data."""
        generate = _FakeImagesEndpoint(_images_response(None))
        monkeypatch.setattr(openai_backend.client.images, "generate", generate)

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.generate_image("test")

    @pytest.mark.asyncio
    async def test_generate_image_no_b64_raises(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test error when API returns empty b64_json."""
        generate = _FakeImagesEndpoint(_images_response([OpenAIImage(b64_json=None)]))
        monkeypatch.setattr(openai_backend.client.images, "generate", generate)

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.generate_image("test")

    @pytest.mark.asyncio
    async def test_transform_image(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test image transformation calls the edit endpoint."""
        edit = _FakeImagesEndpoint(
            _images_response([OpenAIImage(b64_json=_make_png_b64())])
        )
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)

        # Create source image bytes
        src_img = Image.new("RGB", (64, 64), color=(0, 0, 255))
        buf = io.BytesIO()
        src_img.save(buf, format="PNG")
        src_bytes = buf.getvalue()

        result = await openai_backend.transform_image(src_bytes, "make it red")

        assert isinstance(result, bytes)
        assert len(result) > 0

        assert len(edit.calls) == 1
        call_kwargs = edit.calls[0]
        assert call_kwargs["model"] == "gpt-image-2.5-sunburst"
        assert call_kwargs["prompt"] == "make it red"
        assert call_kwargs["quality"] == "high"
        assert "input_fidelity" not in call_kwargs
        assert call_kwargs["output_format"] == "png"

    @pytest.mark.asyncio
    async def test_transform_image_drops_configured_input_fidelity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test image transformation omits input_fidelity from edit calls."""
        openai_backend = OpenAIImageBackend(
            api_key="test-key",
            model="gpt-image-1",
            generate_config=OpenAIImageRequestConfig(),
            edit_config=OpenAIImageRequestConfig(
                size="auto",
                quality="high",
                input_fidelity="high",
                output_format="png",
            ),
        )
        edit = _FakeImagesEndpoint(
            _images_response([OpenAIImage(b64_json=_make_png_b64())])
        )
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)

        result = await openai_backend.transform_image(_make_png_bytes(), "make it red")

        assert isinstance(result, bytes)
        call_kwargs = edit.calls[0]
        assert call_kwargs["model"] == "gpt-image-1"
        assert "input_fidelity" not in call_kwargs

    @pytest.mark.asyncio
    async def test_transform_image_multiple_references(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test image transformation forwards multiple reference files."""
        edit = _FakeImagesEndpoint(
            _images_response([OpenAIImage(b64_json=_make_png_b64())])
        )
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)

        result = await openai_backend.transform_image(
            [_make_png_bytes(), _make_png_bytes()],
            "combine these images",
        )

        assert isinstance(result, bytes)
        call_kwargs = edit.calls[0]
        assert isinstance(call_kwargs["image"], list)
        image_files = _uploaded_files(call_kwargs)
        assert [image_file.name for image_file in image_files] == [
            "image_1.png",
            "image_2.png",
        ]
        assert call_kwargs["prompt"] == "combine these images"
        assert "input_fidelity" not in call_kwargs

    @pytest.mark.asyncio
    async def test_transform_image_names_upload_after_actual_format(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the upload filename follows the bytes, not a wrong declared type."""
        edit = _FakeImagesEndpoint(
            _images_response([OpenAIImage(b64_json=_make_png_b64())])
        )
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)

        await openai_backend.transform_image(
            ImageReference(content=_make_jpeg_bytes(), mime_type="image/png"),
            "make it red",
        )

        assert [file.name for file in _uploaded_files(edit.calls[0])] == ["image_1.jpg"]

    @pytest.mark.asyncio
    async def test_transform_image_retries_with_reencoded_bytes(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a rejected image is re-encoded and retried rather than given up on."""
        edit = _FakeImagesEndpoint(
            _invalid_image_file_error(),
            _images_response([OpenAIImage(b64_json=_make_png_b64())]),
        )
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)
        cmyk_source = _make_jpeg_bytes("CMYK")

        result = await openai_backend.transform_image(
            ImageReference(content=cmyk_source, mime_type="image/jpeg"),
            "make it red",
        )

        assert isinstance(result, bytes)
        assert len(edit.calls) == 2
        (retried_file,) = _uploaded_files(edit.calls[1])
        assert retried_file.name == "image_1.jpg"
        retried_bytes = retried_file.getvalue()
        assert retried_bytes != cmyk_source
        assert Image.open(io.BytesIO(retried_bytes)).mode == "RGB"

    @pytest.mark.asyncio
    async def test_transform_image_reraises_unrelated_bad_request(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a rejection that isn't about the image bytes is not retried."""
        moderation_error = openai.BadRequestError(
            "Your request was rejected by the safety system",
            response=httpx.Response(
                400,
                request=httpx.Request("POST", "https://api.openai.com/v1/images/edits"),
            ),
            body={"code": "moderation_blocked", "message": "rejected"},
        )
        edit = _FakeImagesEndpoint(moderation_error)
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)

        with pytest.raises(openai.BadRequestError, match="safety system"):
            await openai_backend.transform_image(_make_png_bytes(), "make it red")

        assert len(edit.calls) == 1

    @pytest.mark.asyncio
    async def test_transform_image_no_data_raises(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test error when transform API returns no data."""
        edit = _FakeImagesEndpoint(_images_response(None))
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)

        # Create dummy image bytes
        src_img = Image.new("RGB", (64, 64), color=(0, 0, 255))
        buf = io.BytesIO()
        src_img.save(buf, format="PNG")
        src_bytes = buf.getvalue()

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.transform_image(src_bytes, "make it red")

    @pytest.mark.asyncio
    async def test_transform_image_no_b64_raises(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test error when transform API returns empty b64_json."""
        edit = _FakeImagesEndpoint(_images_response([OpenAIImage(b64_json=None)]))
        monkeypatch.setattr(openai_backend.client.images, "edit", edit)

        # Create dummy image bytes
        src_img = Image.new("RGB", (64, 64), color=(0, 0, 255))
        buf = io.BytesIO()
        src_img.save(buf, format="PNG")
        src_bytes = buf.getvalue()

        with pytest.raises(ValueError, match="No image data"):
            await openai_backend.transform_image(src_bytes, "make it red")

    @pytest.mark.asyncio
    async def test_generate_image_artistic_style_reaches_the_prompt(
        self, openai_backend: OpenAIImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that the artistic style is folded into the prompt sent to the API."""
        generate = _FakeImagesEndpoint(
            _images_response([OpenAIImage(b64_json=_make_png_b64())])
        )
        monkeypatch.setattr(openai_backend.client.images, "generate", generate)

        await openai_backend.generate_image("a cat", "artistic")

        prompt = generate.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "artistic" in prompt
        assert "a cat" in prompt


class TestGeminiImageBackend:
    """Test the Gemini image backend."""

    @pytest.fixture
    def gemini_backend(self) -> GeminiImageBackend:
        """Create a Gemini backend with a dummy key."""
        return GeminiImageBackend(api_key="test-key")

    @pytest.mark.asyncio
    async def test_transform_image_multiple_references(
        self, gemini_backend: GeminiImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test Gemini transformation forwards multiple reference images."""
        png_bytes = _make_png_bytes()
        generate_content = _FakeGenerateContent(
            genai_types.GenerateContentResponse(
                candidates=[
                    genai_types.Candidate(
                        content=genai_types.Content(
                            parts=[
                                genai_types.Part(
                                    inline_data=genai_types.Blob(
                                        data=png_bytes, mime_type="image/png"
                                    )
                                )
                            ]
                        )
                    )
                ]
            )
        )
        monkeypatch.setattr(
            gemini_backend.client.aio.models, "generate_content", generate_content
        )

        result = await gemini_backend.transform_image(
            [
                ImageReference(b"first image content", "image/jpeg"),
                ImageReference(b"second image content", "image/webp"),
            ],
            "combine these images",
        )

        assert result == png_bytes
        assert len(generate_content.calls) == 1
        call_kwargs = generate_content.calls[0]
        assert call_kwargs["model"] == "gemini-3-pro-image"
        contents = call_kwargs["contents"]
        assert isinstance(contents, list)
        contents = cast("list[object]", contents)
        assert contents[0] == "combine these images"
        reference_parts = contents[1:]
        assert all(isinstance(part, genai_types.Part) for part in reference_parts)
        assert [
            part.inline_data for part in cast("list[genai_types.Part]", reference_parts)
        ] == [
            genai_types.Blob(data=b"first image content", mime_type="image/jpeg"),
            genai_types.Blob(data=b"second image content", mime_type="image/webp"),
        ]
        config = call_kwargs["config"]
        assert isinstance(config, genai_types.GenerateContentConfig)
        assert config.response_modalities == ["TEXT", "IMAGE"]

    @pytest.mark.asyncio
    async def test_transform_image_no_data_raises(
        self, gemini_backend: GeminiImageBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test error when Gemini transform API returns no image data."""
        generate_content = _FakeGenerateContent(
            genai_types.GenerateContentResponse(candidates=None)
        )
        monkeypatch.setattr(
            gemini_backend.client.aio.models, "generate_content", generate_content
        )

        with pytest.raises(ValueError, match="No image data"):
            await gemini_backend.transform_image(_make_png_bytes(), "make it red")


@pytest.mark.asyncio
async def test_openai_image_config_applied_to_backend_selection() -> None:
    """Configured OpenAI image options are forwarded to backend."""
    app_config = _make_app_config(
        image_generation_backend="openai",
        openai_api_key="test-openai-key",
        openai_image=OpenAIImageConfig(
            model="gpt-image-2",
            default_generate=OpenAIImageRequestConfig(
                size="1024x1024",
                quality="medium",
                input_fidelity="low",
                output_format="jpeg",
                output_compression=75,
            ),
            default_edit=OpenAIImageRequestConfig(
                size="auto",
                quality="high",
                input_fidelity="high",
                output_format="png",
                output_compression=None,
            ),
        ),
    )
    context = _exec_context(app_config=app_config)

    with patch(
        "family_assistant.tools.image_generation.OpenAIImageBackend",
        autospec=True,
        side_effect=_mock_backend,
    ) as backend_cls:
        result = await generate_image_tool(context, prompt="test")

    assert result.attachments
    call_kwargs = backend_cls.call_args.kwargs
    assert call_kwargs["model"] == "gpt-image-2"
    assert call_kwargs["generate_config"] == OpenAIImageRequestConfig(
        size="1024x1024",
        quality="medium",
        input_fidelity="low",
        output_format="jpeg",
        output_compression=75,
    )
    assert call_kwargs["edit_config"] == OpenAIImageRequestConfig(
        size="auto",
        quality="high",
        input_fidelity="high",
        output_format="png",
        output_compression=None,
    )


class TestBackendSelection:
    """Test that the correct backend is selected based on configuration."""

    @pytest.mark.asyncio
    async def test_openai_backend_selected(self) -> None:
        """When image_generation_backend is 'openai', OpenAI backend is used."""
        context = _exec_context(
            app_config=_make_app_config(
                image_generation_backend="openai",
                openai_api_key="test-openai-key",
            )
        )

        with patch(
            "family_assistant.tools.image_generation.OpenAIImageBackend",
            autospec=True,
            side_effect=_mock_backend,
        ) as backend_cls:
            result = await generate_image_tool(context, prompt="test")

        assert result.attachments
        backend_cls.assert_called_once()
        assert backend_cls.call_args.args == ("test-openai-key",)
        call_kwargs = backend_cls.call_args.kwargs
        assert call_kwargs["model"] == "gpt-image-2.5-sunburst"
        assert call_kwargs["generate_config"].quality == "high"
        assert call_kwargs["edit_config"].quality == "high"
        assert call_kwargs["edit_config"].input_fidelity == "high"

    @pytest.mark.asyncio
    async def test_gemini_backend_selected(self) -> None:
        """When image_generation_backend is 'gemini', Gemini backend is used."""
        context = _exec_context(
            app_config=_make_app_config(
                image_generation_backend="gemini",
                gemini_api_key="test-gemini-key",
            )
        )

        with patch(
            "family_assistant.tools.image_generation.GeminiImageBackend",
            autospec=True,
            side_effect=_mock_backend,
        ) as backend_cls:
            result = await generate_image_tool(context, prompt="test")

        assert result.attachments
        backend_cls.assert_called_once_with(
            "test-gemini-key", model="gemini-3-pro-image"
        )

    @pytest.mark.asyncio
    async def test_mock_backend_selected(self) -> None:
        """When image_generation_backend is 'mock', mock backend is used."""
        context = _exec_context(
            app_config=_make_app_config(image_generation_backend="mock")
        )

        result = await generate_image_tool(context, prompt="test landscape")

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_no_backend_configured_autodetects_openai(self) -> None:
        """When image_generation_backend is None and OpenAI key exists, use OpenAI."""
        context = _exec_context(
            app_config=_make_app_config(
                image_generation_backend=None,
                openai_api_key="test-key",
            )
        )

        with patch(
            "family_assistant.tools.image_generation.OpenAIImageBackend",
            autospec=True,
            side_effect=_mock_backend,
        ) as backend_cls:
            result = await generate_image_tool(context, prompt="test")

        assert result.attachments
        backend_cls.assert_called_once()
        assert backend_cls.call_args.args == ("test-key",)
        call_kwargs = backend_cls.call_args.kwargs
        assert call_kwargs["model"] == "gpt-image-2.5-sunburst"
        assert call_kwargs["generate_config"].output_format == "png"
        assert call_kwargs["edit_config"].output_format == "png"

    @pytest.mark.asyncio
    async def test_no_backend_configured_autodetects_gemini(self) -> None:
        """When image_generation_backend is None and Gemini key exists, use Gemini."""
        context = _exec_context(
            app_config=_make_app_config(
                image_generation_backend=None,
                gemini_api_key="test-key",
            )
        )

        with patch(
            "family_assistant.tools.image_generation.GeminiImageBackend",
            autospec=True,
            side_effect=_mock_backend,
        ) as backend_cls:
            result = await generate_image_tool(context, prompt="test")

        assert result.attachments
        backend_cls.assert_called_once_with("test-key", model="gemini-3-pro-image")

    @pytest.mark.asyncio
    async def test_no_backend_configured_uses_mock(self) -> None:
        """When image_generation_backend is None and no API keys, use mock."""
        context = _exec_context(
            app_config=_make_app_config(image_generation_backend=None)
        )

        result = await generate_image_tool(context, prompt="test landscape")

        assert isinstance(result, ToolResult)
        assert result.attachments and len(result.attachments) > 0

    @pytest.mark.asyncio
    async def test_openai_backend_without_key_raises(self) -> None:
        """When backend is 'openai' but no key, error is returned."""
        context = _exec_context(
            app_config=_make_app_config(image_generation_backend="openai")
        )

        result = await generate_image_tool(context, prompt="test")

        assert isinstance(result, ToolResult)
        assert "OPENAI_API_KEY is not set" in result.get_text()

    @pytest.mark.asyncio
    async def test_gemini_backend_without_key_raises(self) -> None:
        """When backend is 'gemini' but no key, error is returned."""
        context = _exec_context(
            app_config=_make_app_config(image_generation_backend="gemini")
        )

        result = await generate_image_tool(context, prompt="test")

        assert isinstance(result, ToolResult)
        assert "GEMINI_API_KEY is not set" in result.get_text()


def _make_png_bytes() -> bytes:
    """Create minimal valid PNG bytes."""
    img = Image.new("RGB", (64, 64), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(mode: str = "RGB") -> bytes:
    """Create valid JPEG bytes in the given colour mode."""
    img = Image.new(mode, (64, 64), color=(0, 0, 255) if mode == "RGB" else None)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _invalid_image_file_error() -> "openai.BadRequestError":
    """Build the error OpenAI returns when it refuses the uploaded bytes."""
    return openai.BadRequestError(
        "Invalid image file or mode for image 1",
        response=httpx.Response(
            400,
            request=httpx.Request("POST", "https://api.openai.com/v1/images/edits"),
        ),
        body={
            "code": "invalid_image_file",
            "message": "Invalid image file or mode for image 1",
        },
    )
