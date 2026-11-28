"""Domain exceptions and the handlers that turn them into consistent JSON errors.

Every error response has the shape:
    {"error": {"code": "<machine_code>", "message": "<safe human message>"}}
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for errors that are safe to expose to API clients."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, *, details: dict | None = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details


class AuthenticationRequiredError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_required"
    message = "Authentication is required."


class InvalidLoginError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_login"
    # Same message for unknown email and wrong password, so emails can't be enumerated.
    message = "Invalid email or password."


class AccountLockedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "account_locked"
    message = "Too many failed sign-in attempts. The account is locked; try again later."


class RateLimitError(AppError):
    """Step I slice 2: the caller exceeded an API rate limit (per-IP or per-user).

    Distinct from AccountLockedError (per-account failed-login lockout): this
    is the flood/abuse throttle. Handlers render it as 429 with the standard
    error envelope; the middleware also sends a Retry-After header.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please try again shortly."


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "You do not have permission to perform this action."


class CsrfRejectedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "csrf_rejected"
    message = "Missing required request header."


class MfaEnrollmentRequiredError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "mfa_enrollment_required"
    message = "Organization policy requires MFA enrollment before using this feature."


class MfaReauthenticationRequiredError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "mfa_reauthentication_required"
    message = "Sign out and sign in again to verify the organization-required second factor."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The resource was changed by another request. Reload and try again."


class InvalidInputError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "invalid_input"
    message = "The request contains invalid values."


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please try again shortly."


class FileRejectedError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "file_rejected"
    message = "The file was rejected."


class FileTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "file_too_large"
    message = "The file is too large."


class ProviderVerificationFailedError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "provider_verification_failed"
    message = "The provider rejected or could not verify the credentials."


class CredentialUnreadableError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "credential_unreadable"
    message = (
        "The stored credential cannot be decrypted with the configured master key. "
        "Stage a new API key."
    )


class LLMNotConfiguredError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "llm_not_configured"
    message = "No AI provider is active. An administrator must configure one."


class LLMTimeoutError(AppError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "llm_timeout"
    message = "The AI provider did not respond in time. Please try again."


class LLMRateLimitError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "llm_rate_limited"
    message = "The AI provider is busy. Please try again shortly."


class RoutingBudgetExceededError(AppError):
    """The smart router refused a task: it would exceed the monthly AI budget."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "routing_budget_exceeded"
    message = "The monthly AI budget for this task is exhausted."


class LLMUnavailableError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "llm_unavailable"
    message = "The AI provider is currently unavailable."


class AgentActionUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "agent_action_unavailable"
    message = "This approved action has no live adapter yet. It remains approved and was not marked executed."


class DatabaseAnalysisQueryError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "database_analysis_query_invalid"
    message = "The requested analysis does not fit the configured database allowlist."


class DatabaseAnalysisUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "database_analysis_unavailable"
    message = "The read-only business database is unavailable or is not configured safely."


class LegacyBrowserUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "legacy_browser_unavailable"
    message = "Controlled legacy-browser review is unavailable. An administrator must enable and configure it."


def _error_body(code: str, message: str, details: object | None = None) -> dict:
    body: dict = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("Request failed: code=%s path=%s", exc.code, request.url.path)
        return JSONResponse(
            status_code=exc.status_code, content=_error_body(exc.code, exc.message, exc.details)
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Strip the submitted input from the details so user content is never echoed back.
        details = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_body("validation_error", "Request validation failed.", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body("http_error", str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Exception messages and traceback context can contain submitted data or provider responses.
        # Keep the log to safe metadata; the request ID correlates the generic failure with its trace.
        request_id = getattr(request.state, "request_id", None)
        logger.error(
            "Unhandled request error_type=%s path=%s request_id=%s",
            type(exc).__name__, request.url.path, request_id,
        )
        body = _error_body("internal_error", "An unexpected error occurred.")
        headers = {}
        if request_id:
            body["error"]["request_id"] = request_id
            headers["X-Request-ID"] = request_id  # this handler runs outside the middleware that sets it
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body, headers=headers)
