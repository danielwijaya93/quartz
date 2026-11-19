"""Typed errors shared by model providers so callers can react precisely.

Every provider translates its own transport/status failures into these types.
Callers (agent loop, CLI, ACP) branch on the type, never on message text.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for all errors Quartz raises deliberately."""


class ProviderError(AgentError):
    """A model provider rejected a request or could not be reached.

    `detail` carries the provider's own message for diagnostics; `status_code`
    is the HTTP status when one was received.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.detail = detail

    def __str__(self) -> str:
        text = self.message
        if self.status_code is not None:
            text += f" (status {self.status_code})"
        if self.detail:
            text += f": {self.detail}"
        return text


class ContextOverflowError(ProviderError):
    """The prompt exceeded the model's context window and must be compacted."""


class CredentialError(AgentError):
    """Required credentials are missing or unusable; retrying cannot help."""


class EndpointConfigError(AgentError):
    """The configured provider endpoint is unknown or contradictory."""


# Phrases providers use when a request is rejected for being too long.
# Matched against the lower-cased error body, only for 400/413 responses.
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "exceeds the maximum number of tokens",
    "input token count",
    "prompt is too long",
    "too many tokens",
    "context window",
)

_OVERFLOW_STATUSES = frozenset({400, 413})


def is_context_overflow(status_code: int | None, body: str) -> bool:
    """Return True only when a provider response means "prompt too long"."""
    if status_code not in _OVERFLOW_STATUSES:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def provider_error(
    provider: str,
    status_code: int,
    body: str,
    *,
    context: str | None = None,
) -> ProviderError:
    """Build the most specific ProviderError for a non-success HTTP response."""
    summary = context or f"{provider} request failed"
    if is_context_overflow(status_code, body):
        return ContextOverflowError(
            summary, provider=provider, status_code=status_code, detail=body
        )
    return ProviderError(summary, provider=provider, status_code=status_code, detail=body)
