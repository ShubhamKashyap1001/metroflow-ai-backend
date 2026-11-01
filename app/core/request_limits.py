import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

class PayloadTooLarge(Exception):
    pass

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

                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
