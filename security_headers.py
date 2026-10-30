"""Baseline browser security headers for every HTTP response (AST-006).

A pure ASGI middleware, so it also covers static assets, FastAPI's own 404/405
JSON, validation errors and responses from other middleware (CORS preflight,
rate limiting). A header already set by a route is never overwritten.

Configuration (environment):
- SECURITY_HEADERS_ENABLED: default "true". Set "false" to roll back without a
  code change.
- HSTS_ENABLED: default "false". Only enable once the production domain is
  confirmed HTTPS-only; browsers then refuse plain HTTP for HSTS_MAX_AGE seconds.
- HSTS_MAX_AGE: default 31536000 (one year).

The SPA content security policy allows only same-origin scripts, styles, fonts,
images and API calls. Fonts are self-hosted, so no third-party origins are
needed. Swagger UI (/docs) and ReDoc (/redoc) load FastAPI's default CDN assets
and an inline bootstrap script, so those two pages get a narrower relaxation.
"""

import os

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


APP_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))

DOCS_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly",
    "worker-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
))

DOCS_PATHS = ("/docs", "/redoc")

BASE_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, enabled: bool | None = None,
                 hsts: bool | None = None, hsts_max_age: int | None = None) -> None:
        self.app = app
        self.enabled = _flag("SECURITY_HEADERS_ENABLED", True) if enabled is None else enabled
        use_hsts = _flag("HSTS_ENABLED", False) if hsts is None else hsts
        max_age = int(os.getenv("HSTS_MAX_AGE", "31536000")) if hsts_max_age is None else hsts_max_age
        if max_age < 0:
            raise RuntimeError("HSTS_MAX_AGE must be a non-negative integer")
        self.hsts = f"max-age={max_age}; includeSubDomains".encode() if use_hsts else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        docs = path in DOCS_PATHS or path.startswith(tuple(p + "/" for p in DOCS_PATHS))
        additions = list(BASE_HEADERS)
        additions.append((b"content-security-policy", (DOCS_CSP if docs else APP_CSP).encode()))
        if self.hsts:
            additions.append((b"strict-transport-security", self.hsts))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((name, value) for name, value in additions if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
