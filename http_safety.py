"""Pure-ASGI safety layers: request IDs, sanitised 500s and a request body cap.

Nothing here logs an exception's message, its chained causes, a traceback,
request bodies, headers, query strings or raw paths. PostgreSQL error DETAIL
(``Key (cnic)=(...)``, ``Failing row contains (...)``) and SQLAlchemy's
parameter dumps live in exception text, so only the exception type, SQLSTATE,
constraint name, method, route template and request ID are ever emitted.
"""

import json
import logging
import re
import uuid
from contextvars import ContextVar
from pathlib import Path


logger = logging.getLogger("rideshare.errors")

REQUEST_ID_HEADER = b"x-request-id"
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{8,64}")
_SQLSTATE_PATTERN = re.compile(r"[0-9A-Z]{5}")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")
_APP_ROOT = Path(__file__).resolve().parent
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

INTERNAL_ERROR_BODY = b'{"detail":"Internal server error"}'
TOO_LARGE_BODY = b'{"detail":"Request body too large"}'


async def _send_json(send, status, body, request_id=None):
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode("ascii"))]
    if request_id:
        headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestIdMiddleware:
    """Accept a well-formed inbound X-Request-ID or mint one; echo it back."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = None
        for name, value in scope.get("headers", ()):
            if name == REQUEST_ID_HEADER:
                inbound = value.decode("latin-1")
                break
        request_id = inbound if inbound and _REQUEST_ID_PATTERN.fullmatch(inbound) else str(uuid.uuid4())
        token = request_id_var.set(request_id)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", ()) if k.lower() != REQUEST_ID_HEADER]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def _db_fields(exc):
    """SQLSTATE and constraint name only; both are validated identifiers."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None) or getattr(orig, "constraint_name", None)
    sqlstate = sqlstate if isinstance(sqlstate, str) and _SQLSTATE_PATTERN.fullmatch(sqlstate) else None
    if constraint is not None:
        constraint = constraint if isinstance(constraint, str) and _IDENTIFIER_PATTERN.fullmatch(constraint) else "<invalid>"
    db_error = type(cause).__name__ if cause is not None else (type(orig).__name__ if orig is not None else None)
    return sqlstate, constraint, db_error


def _app_location(exc):
    """Deepest traceback frame inside this repository: file, line and function only."""
    location = None
    tb = exc.__traceback__
    while tb is not None:
        filename = Path(tb.tb_frame.f_code.co_filename)
        try:
            relative = filename.resolve().relative_to(_APP_ROOT)
        except (ValueError, OSError):
            relative = None
        if relative is not None and ".venv" not in relative.parts and "site-packages" not in relative.parts:
            location = f"{relative.as_posix()}:{tb.tb_lineno}:{tb.tb_frame.f_code.co_name}"
        tb = tb.tb_next
    return location


def log_unhandled(exc, scope):
    route = scope.get("route")
    record = {
        "event": "unhandled_exception",
        "request_id": request_id_var.get(),
        "method": scope.get("method"),
        # Template only (e.g. /bookings/{ride_id}); raw paths are never logged.
        "route": getattr(route, "path", None) or "<unmatched>",
        "exc_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "location": _app_location(exc),
    }
    sqlstate, constraint, db_error = _db_fields(exc)
    if sqlstate or constraint or db_error:
        record.update(sqlstate=sqlstate, constraint=constraint, db_error=db_error)
    # Deliberately no exc_info / str(exc): exception chains carry DETAIL and params.
    logger.error(json.dumps(record, sort_keys=True))


class SafeErrorMiddleware:
    """Turn any unhandled exception into a generic 500 and log it safely.

    Starlette's ServerErrorMiddleware always re-raises, and Uvicorn then logs
    the full traceback (including PostgreSQL DETAIL text). This layer sits
    inside it and does not re-raise, so neither ever sees the exception.
    HTTPException and RequestValidationError are handled by FastAPI's inner
    ExceptionMiddleware and never reach here.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except RequestBodyTooLarge:
            raise  # RequestBodyLimitMiddleware (outside this layer) answers 413.
        except Exception as exc:
            log_unhandled(exc, scope)
            del exc  # Drop the traceback reference promptly.
            if not response_started:
                await _send_json(send, 500, INTERNAL_ERROR_BODY, request_id_var.get())
            # If the response already started, the connection is closed by the
            # server as an incomplete response; nothing more can be sent safely.


class RequestBodyTooLarge(Exception):
    """Raised from receive() once a streamed body passes the limit."""


class RequestBodyLimitMiddleware:
    """Reject request bodies above ``max_bytes`` with 413.

    Declared Content-Length above the limit is rejected before the app runs.
    Chunked or under-declared bodies are counted while streaming; once the
    limit is passed, receive() raises and whatever response the app attempts
    (FastAPI turns body-read failures into 400) is replaced by the 413.
    """

    def __init__(self, app, max_bytes):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None  # The server validates the header itself.
                if declared is not None and declared > self.max_bytes:
                    await _send_json(send, 413, TOO_LARGE_BODY, request_id_var.get())
                    return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise RequestBodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal response_started
            if exceeded:
                return  # Suppress the app's own reaction; the 413 is sent below.
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except RequestBodyTooLarge:
            pass
        if exceeded and not response_started:
            await _send_json(send, 413, TOO_LARGE_BODY, request_id_var.get())
