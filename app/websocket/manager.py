"""Connection manager for real-time operational monitoring
(Milestone 2 outcome), live alert / train-position push (Milestone 3),
and per-user notification push (Milestone 4).

Two ways to push an event:
  - `await manager.broadcast(event, data)` - use from code that's
    already running on the main asyncio event loop (e.g. the crowd/
    train simulators, which are asyncio background tasks).
  - `manager.notify(event, data)` - a sync, thread-safe, fire-and-forget
    version. FastAPI runs plain `def` routes/services in a worker
    thread pool, not on the event loop, so those call this instead of
    awaiting broadcast() directly (see alert_service / schedule_service).

Connections optionally carry a `user_id` (see /ws/monitor's optional
?token= param in main.py). That lets a single event be delivered to
just one person's tab(s) instead of everyone:
  - `await manager.broadcast_to_user(user_id, event, data)` / sync
    `manager.notify_user(user_id, event, data)` - only the given
    user's connection(s). Anonymous (no-token) connections never
    receive these.
"""
import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 5

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        # Parallel mapping: which user (if any) each open connection
        # belongs to. A connection with no entry here (or a None
        # value) is anonymous/broadcast-only.
        self._connection_users: dict[WebSocket, str | None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Call once at app startup (inside the lifespan, which runs on
        the real event loop) so notify() has a loop to schedule onto."""
        self._loop = loop or asyncio.get_event_loop()

    async def connect(self, websocket: WebSocket, user_id: str | None = None) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)
        self._connection_users[websocket] = user_id

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self._connection_users.pop(websocket, None)

    async def _send_one(self, connection: WebSocket, payload: str) -> WebSocket | None:
        """Send to a single connection with a timeout. Returns the
        connection if it should be dropped (send failed or timed out),
        else None - so the caller can remove dead connections in one
        pass after every send has been attempted."""
        try:
            await asyncio.wait_for(connection.send_text(payload), timeout=SEND_TIMEOUT_SECONDS)
            return None
        except asyncio.TimeoutError:
            logger.warning("[websocket] send timed out after %ss - dropping stale connection.",
                            SEND_TIMEOUT_SECONDS)
            return connection
        except Exception:
                                                                     
            return connection

    async def broadcast(self, event: str, data: dict) -> None:
        if not self.active_connections:
            return
        payload = json.dumps({"event": event, "data": data}, default=str)

        connections = list(self.active_connections)

        results = await asyncio.gather(
            *(self._send_one(connection, payload) for connection in connections),
            return_exceptions=False,
        )

        stale = [connection for connection in results if connection is not None]
        for connection in stale:
            self.disconnect(connection)

    def notify(self, event: str, data: dict) -> None:
        """Sync/thread-safe fire-and-forget broadcast. Safe to call
        from sync service functions (alert_service.create_alert,
        schedule_service.handle_delay, etc.) that run inside FastAPI's
        threadpool, as well as from async code already on the loop."""
        if self._loop is None or not self.active_connections:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(event, data), self._loop)
        except RuntimeError:
                                                                      
            pass

    async def broadcast_to_user(self, user_id: str, event: str, data: dict) -> None:
        """Deliver only to connection(s) registered under this user_id
        (a user with the app open in multiple tabs gets it in all of
        them). No-op if that user has no open connection right now -
        this is push-on-top-of-pull, so the row is still there next
        time they poll/load the notification center."""
        targets = [
            ws
            for ws, uid in self._connection_users.items()
            if uid is not None and str(uid) == str(user_id)
        ]
        if not targets:
            return
        payload = json.dumps({"event": event, "data": data}, default=str)
        results = await asyncio.gather(
            *(self._send_one(connection, payload) for connection in targets),
            return_exceptions=False,
        )
        for connection in results:
            if connection is not None:
                self.disconnect(connection)

    def notify_user(self, user_id: str, event: str, data: dict) -> None:
        """Sync/thread-safe fire-and-forget version of
        broadcast_to_user(), for use from plain `def` service
        functions running in FastAPI's threadpool (e.g.
        notification_service.create_notification)."""
        if self._loop is None or not self.active_connections:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_to_user(user_id, event, data), self._loop
            )
        except RuntimeError:
            pass

manager = ConnectionManager()
