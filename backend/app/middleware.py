from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applies defense-in-depth HTTP security headers to all API responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    In-memory sliding window rate limiter per client IP.
    Protects endpoints against brute-force, scraping, and denial-of-service.
    """

    def __init__(self, app, per_minute: int = 120, enabled: bool = True) -> None:
        super().__init__(app)
        self.per_minute = per_minute
        self.enabled = enabled
        self.requests: dict[str, list[float]] = defaultdict(list)
        self.exempt_paths = {
            "/health",
            "/health/ready",
            "/health/detailed",
            "/health/database",
            "/docs",
            "/openapi.json",
            "/redoc",
        }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled or request.url.path in self.exempt_paths:
            return await call_next(request)

        forwarded = request.headers.get("x-forwarded-for")
        client_ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else "unknown")
        )

        now = time.time()
        window_start = now - 60.0

        # Purge timestamps older than 60 seconds
        timestamps = [ts for ts in self.requests[client_ip] if ts > window_start]
        self.requests[client_ip] = timestamps

        if len(timestamps) >= self.per_minute:
            retry_after = int(60.0 - (now - timestamps[0])) if timestamps else 60
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "Rate limit exceeded. Please slow down your requests.",
                    "retry_after_seconds": max(1, retry_after),
                },
                headers={
                    "Retry-After": str(max(1, retry_after)),
                    "X-RateLimit-Limit": str(self.per_minute),
                    "X-RateLimit-Remaining": "0",
                },
            )

        self.requests[client_ip].append(now)
        response = await call_next(request)
        remaining = max(0, self.per_minute - len(self.requests[client_ip]))
        response.headers["X-RateLimit-Limit"] = str(self.per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
