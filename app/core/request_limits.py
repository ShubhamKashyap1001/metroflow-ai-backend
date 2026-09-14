"""Request/upload size protection for Render Free 512MB.

FastAPI/Starlette impose NO limit on incoming HTTP request body size by
default - a client (malicious or just a buggy retry loop) can send an
arbitrarily large body and Starlette will happily buffer the whole
thing in memory before FastAPI ever gets to validate it against a
Pydantic model, which is more than enough to OOM a 512MB instance from
a single request.

This is a plain ASGI middleware (no new dependency - Starlette/FastAPI
are already in requirements.txt and this only uses their public ASGI
types), deliberately NOT a `@app.middleware("http")`/
`BaseHTTPMiddleware` one: BaseHTTPMiddleware works by fully awaiting a
`StreamingResponse`-like wrapper around the downstream app, which does
not give a hard guarantee about the request body never being consumed
by the framework before this layer gets a chance to reject it. A raw
ASGI middleware that wraps `receive` directly is the only reliable way
to guarantee rejection happens before the oversized remainder of a
body is ever read off the socket into memory.

Two layers, cheapest first:
  1. `Content-Length` header check - rejects with ZERO bytes read from
     the socket whenever the client declares a size up front (true for
     every normal JSON POST/PUT/PATCH this API receives).
  2. Streaming byte-count check - covers chunked transfer-encoding (no
     Content-Length) or a client that lies about it. Each chunk is
     counted as it arrives; the instant the running total exceeds the
     limit, the connection is aborted - the oversized rest of the body
     is never buffered or handed to the app.

Only wraps `scope["type"] == "http"` - WebSocket connections and the
ASGI lifespan scope pass straight through untouched, so this has no
effect on `/ws/monitor` or its message protocol.
"""
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class PayloadTooLarge(Exception):
    """Raised internally once the streamed body exceeds the configured
    limit - never escapes this module."""


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size <= 0:
            await self.app(scope, receive, send)
            return

        content_length = self._declared_content_length(scope)
        if content_length is not None and content_length > self.max_body_size:
            await self._reject(send, declared_size=content_length)
            return

        total_received = 0

        async def limited_receive() -> Message:
            nonlocal total_received
            message = await receive()
            if message["type"] == "http.request":
                total_received += len(message.get("body") or b"")
                if total_received > self.max_body_size:
                    raise PayloadTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except PayloadTooLarge:
            logger.warning(
                "[request_limits] rejected oversized request body on %s "
                "(exceeded %d bytes, no/incorrect Content-Length header).",
                scope.get("path"), self.max_body_size,
            )
            await self._reject(send, declared_size=None)

    @staticmethod
    def _declared_content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    async def _reject(self, send: Send, declared_size: int | None) -> None:
        if declared_size is not None:
            logger.warning(
                "[request_limits] rejected oversized request (Content-Length=%d, "
                "max %d bytes).", declared_size, self.max_body_size,
            )
        body = (
            b'{"detail":"Request payload too large (max '
            + str(self.max_body_size).encode() + b' bytes)."}'
        )
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # Force the connection closed instead of trying to
                # keep it alive for a next request - part or all of
                # the oversized body may still be unread on the wire,
                # and reusing the connection without draining it would
                # desync the next request on it.
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
