"""Typed errors shared by model providers so callers can react precisely.

Every provider translates its own transport/status failures into these types.
Callers (agent loop, CLI, ACP) branch on the type, never on message text.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any


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


class AuthenticationError(ProviderError):
    """The provider rejected authentication (HTTP 401/403)."""


class RateLimitError(ProviderError):
    """The provider rate-limited the request (HTTP 429)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            status_code=status_code,
            detail=detail,
        )
        self.retry_after = retry_after


class CredentialError(AgentError):
    """Required credentials are missing or unusable; retrying cannot help."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.message}: {self.detail}" if self.detail else self.message


class EndpointConfigError(AgentError):
    """The configured provider endpoint is unknown or contradictory."""


class SessionNotFoundError(FileNotFoundError):
    """A resume/continue request had no session to resume.

    Subclasses `FileNotFoundError` because that is what it is: a named file was
    asked for and is absent. It carries `cwd` so each transport can phrase the
    message itself -- the terminal collapses the home directory, a web server
    reports whatever its client sent. `str()` is the unformatted fallback so a
    caller that does not special-case it still gets a usable line.
    """

    def __init__(self, cwd: Any) -> None:
        super().__init__(f"No previous session found for {cwd}.")
        self.cwd = cwd


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
    "too many input tokens",
    "prompt is too large",
    "request too large",
    "maximum prompt length",
)

_OVERFLOW_STATUSES = frozenset({400, 413})
_AUTH_STATUSES = frozenset({401, 403})


def parse_retry_after(headers: Any) -> float | None:
    """Parse an HTTP Retry-After header (seconds or HTTP-date) into seconds."""
    if headers is None:
        return None
    raw: Any = None
    if hasattr(headers, "get"):
        raw = headers.get("retry-after")
        if raw is None:
            raw = headers.get("Retry-After")
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    try:
        seconds = float(val)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val)
        import time

        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


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
    retry_after: float | None = None,
) -> ProviderError:
    """Build the most specific ProviderError for a non-success HTTP response."""
    summary = context or f"{provider} request failed"
    if is_context_overflow(status_code, body):
        return ContextOverflowError(
            summary, provider=provider, status_code=status_code, detail=body
        )
    if status_code in _AUTH_STATUSES:
        return AuthenticationError(
            summary, provider=provider, status_code=status_code, detail=body
        )
    if status_code == 429:
        return RateLimitError(
            summary,
            provider=provider,
            status_code=status_code,
            detail=body,
            retry_after=retry_after,
        )
    return ProviderError(summary, provider=provider, status_code=status_code, detail=body)
