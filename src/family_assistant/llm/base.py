"""
Base exception hierarchy for LLM providers.

The data classes and protocol are defined in __init__.py to avoid circular imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.llm import LLMStreamEvent


# Exception hierarchy for LLM providers
class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""

    def __init__(self, message: str, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__(message)


class RateLimitError(LLMProviderError):
    """Raised when hitting provider rate limits."""

    def __init__(
        self,
        message: str,
        provider: str,
        model: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider, model)
        self.retry_after = retry_after


class AuthenticationError(LLMProviderError):
    """Raised when authentication fails."""


class ModelNotFoundError(LLMProviderError):
    """Raised when the requested model doesn't exist."""


class ContextLengthError(LLMProviderError):
    """Raised when exceeding model context limits."""


class InvalidRequestError(LLMProviderError):
    """Raised when the request format is invalid."""


class ProviderConnectionError(LLMProviderError):
    """Raised when unable to connect to the provider."""


class ProviderTimeoutError(LLMProviderError):
    """Raised when a provider request times out."""


class ServiceUnavailableError(LLMProviderError):
    """Raised when the service is temporarily unavailable (503 errors)."""


class StructuredOutputError(LLMProviderError):
    """Raised when structured output generation fails after retries.

    This error indicates that the LLM was unable to generate a valid response
    matching the requested Pydantic model schema, even after retry attempts.
    """

    def __init__(
        self,
        message: str,
        provider: str,
        model: str,
        raw_response: str | None = None,
        validation_error: Exception | None = None,
    ) -> None:
        super().__init__(message, provider, model)
        self.raw_response = raw_response
        self.validation_error = validation_error


_ERROR_TYPE_TO_EXCEPTION: dict[str, type[LLMProviderError]] = {
    "RateLimitError": RateLimitError,
    "ContextLengthError": ContextLengthError,
    "AuthenticationError": AuthenticationError,
    "InvalidRequestError": InvalidRequestError,
    "ModelNotFoundError": ModelNotFoundError,
    "ProviderConnectionError": ProviderConnectionError,
    "ProviderTimeoutError": ProviderTimeoutError,
    "ServiceUnavailableError": ServiceUnavailableError,
}

_NORMALIZED_ERROR_TYPE_TO_EXCEPTION: dict[str, type[LLMProviderError]] = {
    # Canonical class names
    "ratelimiterror": RateLimitError,
    "contextlengtherror": ContextLengthError,
    "authenticationerror": AuthenticationError,
    "invalidrequesterror": InvalidRequestError,
    "modelnotfounderror": ModelNotFoundError,
    "providerconnectionerror": ProviderConnectionError,
    "providertimeouterror": ProviderTimeoutError,
    "serviceunavailableerror": ServiceUnavailableError,
    # Provider snake_case / shorthand variants
    "ratelimit": RateLimitError,
    "contextlength": ContextLengthError,
    "authentication": AuthenticationError,
    "invalidrequest": InvalidRequestError,
    "modelnotfound": ModelNotFoundError,
    "connection": ProviderConnectionError,
    "timeout": ProviderTimeoutError,
    "serviceunavailable": ServiceUnavailableError,
}


def _normalize_error_type(error_type: str) -> str:
    """Normalize provider error type values to a consistent lookup key."""
    return "".join(char for char in error_type.lower() if char.isalnum())


def stream_error_to_exception(event: LLMStreamEvent) -> Exception:
    """Map an LLM stream error event to a typed exception.

    When the streaming provider yields an error event (because content was already
    emitted and it can't raise), this converts the metadata back to a typed exception
    so the caller can handle it appropriately.
    """
    error_msg = event.error or "Unknown streaming error"
    metadata = event.metadata or {}
    error_type = metadata.get("error_type", "")
    provider = metadata.get("provider", "unknown")
    model = metadata.get("model", "unknown")

    exc_class = _ERROR_TYPE_TO_EXCEPTION.get(error_type)
    if exc_class is None and error_type:
        normalized_error_type = _normalize_error_type(error_type)
        exc_class = _NORMALIZED_ERROR_TYPE_TO_EXCEPTION.get(normalized_error_type)
    if exc_class:
        return exc_class(error_msg, provider=provider, model=model)

    return RuntimeError(f"LLM streaming error: {error_msg}")
