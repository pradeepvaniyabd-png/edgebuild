"""Error hierarchy and the classifier the self-healing engine keys its strategies on."""

from __future__ import annotations

import asyncio
import json
import subprocess

from .types import ErrorClass, ProviderStatus


class SuperAgentError(Exception):
    error_class: ErrorClass = ErrorClass.UNKNOWN

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} (hint: {self.hint})" if self.hint else self.message


class ConfigError(SuperAgentError):
    error_class = ErrorClass.DEPENDENCY


class SecurityError(SuperAgentError):
    error_class = ErrorClass.TOOL


class PathEscapeError(SecurityError):
    pass


class CommandBlockedError(SecurityError):
    pass


class PermissionDeniedError(SecurityError):
    pass


class ApprovalRequired(SuperAgentError):
    """Control flow, not a failure: the task parks in WAITING_APPROVAL until a human decides."""

    def __init__(self, approval_id: str, action: str, reason: str):
        super().__init__(f"approval required for {action}: {reason}")
        self.approval_id = approval_id
        self.action = action
        self.reason = reason


class ApprovalDenied(SuperAgentError):
    error_class = ErrorClass.TOOL


class BudgetExceeded(SuperAgentError):
    def __init__(self, scope: str, estimate: float, limit: float, spent: float):
        super().__init__(
            f"estimated ${estimate:.4f} would exceed the {scope} budget "
            f"(${spent:.4f} spent of ${limit:.2f})"
        )
        self.scope = scope
        self.estimate = estimate
        self.limit = limit
        self.spent = spent


_PROVIDER_CLASS = {
    ProviderStatus.RATE_LIMITED: ErrorClass.RATE_LIMIT,
    ProviderStatus.AUTH_ERROR: ErrorClass.AUTH,
    ProviderStatus.NETWORK_ERROR: ErrorClass.NETWORK,
    ProviderStatus.TEMPORARY_ERROR: ErrorClass.NETWORK,
    ProviderStatus.UNAVAILABLE: ErrorClass.MODEL,
    ProviderStatus.NOT_CONFIGURED: ErrorClass.DEPENDENCY,
}


class ProviderError(SuperAgentError):
    def __init__(
        self,
        provider: str,
        status: ProviderStatus,
        message: str,
        *,
        retry_after: float | None = None,
        hint: str | None = None,
    ):
        super().__init__(f"[{provider}] {message}", hint=hint)
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.error_class = _PROVIDER_CLASS.get(status, ErrorClass.MODEL)


class NoProviderAvailable(SuperAgentError):
    """Every candidate for a capability was unconfigured, cooling down or failed."""

    error_class = ErrorClass.DEPENDENCY

    def __init__(self, kind: str, reasons: dict[str, str], *, hint: str | None = None):
        detail = "; ".join(f"{name}: {why}" for name, why in reasons.items()) or "no provider registered"
        super().__init__(f"no {kind} provider available — {detail}", hint=hint)
        self.kind = kind
        self.reasons = reasons


class ToolError(SuperAgentError):
    error_class = ErrorClass.TOOL


class ValidationFailed(SuperAgentError):
    error_class = ErrorClass.VALIDATION


class VerificationFailed(SuperAgentError):
    error_class = ErrorClass.VALIDATION

    def __init__(self, message: str, failed_checks: list[str] | None = None):
        super().__init__(message)
        self.failed_checks = failed_checks or []


class TaskTimeout(SuperAgentError):
    error_class = ErrorClass.TIMEOUT


class DependencyMissing(SuperAgentError):
    error_class = ErrorClass.DEPENDENCY


class MediaError(SuperAgentError):
    error_class = ErrorClass.MEDIA


class CodeError(SuperAgentError):
    error_class = ErrorClass.CODE

    def __init__(self, message: str, *, exit_code: int | None = None, output: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.output = output


class FileProblem(SuperAgentError):
    error_class = ErrorClass.FILE


def classify_error(exc: BaseException) -> ErrorClass:
    if isinstance(exc, SuperAgentError):
        return exc.error_class
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return ErrorClass.TIMEOUT
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ErrorClass.TIMEOUT
        if isinstance(exc, httpx.TransportError):
            return ErrorClass.NETWORK
    except ImportError:  # pragma: no cover - httpx is a core dependency
        pass
    if isinstance(exc, ModuleNotFoundError | ImportError):
        return ErrorClass.DEPENDENCY
    if isinstance(exc, FileNotFoundError | IsADirectoryError | NotADirectoryError | PermissionError):
        return ErrorClass.FILE
    if isinstance(exc, ConnectionError):
        return ErrorClass.NETWORK
    if isinstance(exc, SyntaxError):
        return ErrorClass.CODE
    if isinstance(exc, subprocess.CalledProcessError):
        return ErrorClass.CODE
    if isinstance(exc, json.JSONDecodeError):
        return ErrorClass.VALIDATION
    try:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            return ErrorClass.VALIDATION
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, ValueError | TypeError | KeyError):
        return ErrorClass.VALIDATION
    if isinstance(exc, OSError):
        return ErrorClass.FILE
    return ErrorClass.UNKNOWN
