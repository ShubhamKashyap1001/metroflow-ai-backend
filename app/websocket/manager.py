import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict

from fastapi import WebSocket

from app.core import cache
from app.websocket import events

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 5

try:
    from app.core.config import settings as _settings

    MAX_CONNECTIONS = int(getattr(_settings, "WS_MAX_CONNECTIONS", 200))
    MAX_CONNECTIONS_PER_USER = int(getattr(_settings, "WS_MAX_CONNECTIONS_PER_USER", 20))
except Exception:
    MAX_CONNECTIONS = int(os.environ.get("WS_MAX_CONNECTIONS", "200"))
    MAX_CONNECTIONS_PER_USER = int(os.environ.get("WS_MAX_CONNECTIONS_PER_USER", "20"))

WS_CLOSE_TRY_AGAIN_LATER = 1013

RELAY_CHANNEL = "metroflow:ws:relay"

_RELAY_BACKOFF_FLOOR_SECONDS = 1.0
_RELAY_BACKOFF_CAP_SECONDS = 30.0

DEDUPE_MAX_ENTRIES = 4096

STALE_AFTER_SECONDS = 45
REAP_INTERVAL_SECONDS = 20

COALESCE_WINDOW_SECONDS = 0.2

_COALESCE_MERGE_KEY = {
    events.CROWD_UPDATE: "station_id",
    events.TRAIN_POSITION: "train_id",
}

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

        self._connection_users: dict[WebSocket, str | None] = {}

        self._last_seen: dict[WebSocket, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

        self._instance_id = uuid.uuid4().hex
        self._relay_thread: threading.Thread | None = None
        self._relay_stop = threading.Event()

        self._seen_event_ids: "OrderedDict[str, None]" = OrderedDict()
        self._dedupe_lock = threading.Lock()

        self._coalesce_buffers: dict[str, dict] = {}
        self._coalesce_scheduled: set[str] = set()
        self._coalesce_lock = threading.Lock()

        self._reaper_task: asyncio.Task | None = None

        self._latest_state: dict[str, dict] = {}
        self._latest_state_ts: dict[str, object] = {}

        self._metrics_lock = threading.Lock()
        self._connects_total = 0
        self._disconnects_total: dict[str, int] = {}
        self._event_send_failures_total: dict[str, int] = {}

    def get_metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            return {
                "connects_total": self._connects_total,
                "disconnects_total": dict(self._disconnects_total),
                "event_send_failures_total": dict(self._event_send_failures_total),
                "active_connections": len(self.active_connections),
            }

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_event_loop()

    def start_relay(self) -> None:
        if self._relay_thread is not None and self._relay_thread.is_alive():
            return
        self._relay_stop.clear()
        self._relay_thread = threading.Thread(
            target=self._relay_loop, name="ws-relay-subscriber", daemon=True
        )
        self._relay_thread.start()

    def stop_relay(self) -> None:
        self._relay_stop.set()
        thread, self._relay_thread = self._relay_thread, None
        if thread is not None:

            thread.join(timeout=2.0)

    def _relay_loop(self) -> None:
        backoff = _RELAY_BACKOFF_FLOOR_SECONDS
        redis_down_logged = False

        while not self._relay_stop.is_set():
            client = cache.get_client()
            if client is None:
                if not redis_down_logged:
                    logger.warning(
                        "[websocket] relay: Redis unavailable - will keep "
                        "retrying (backoff up to %.0fs) until it's reachable. "
                        "Until then, each process only delivers events to its "
                        "own locally connected clients.",
                        _RELAY_BACKOFF_CAP_SECONDS,
                    )
                    redis_down_logged = True
                if self._relay_stop.wait(timeout=backoff):
                    return
                backoff = min(backoff * 2, _RELAY_BACKOFF_CAP_SECONDS)
                continue

            if redis_down_logged:
                logger.info("[websocket] relay: Redis reachable again - resubscribing.")
                redis_down_logged = False
            backoff = _RELAY_BACKOFF_FLOOR_SECONDS

            pubsub = client.pubsub()
            try:
                pubsub.subscribe(RELAY_CHANNEL)
            except Exception as exc:
                logger.warning(
                    "[websocket] relay: subscribe failed (%s) - retrying in %.0fs.",
                    exc, backoff,
                )
                try:
                    pubsub.close()
                except Exception:
                    pass
                if self._relay_stop.wait(timeout=backoff):
                    return
                backoff = min(backoff * 2, _RELAY_BACKOFF_CAP_SECONDS)
                continue

            try:
                while not self._relay_stop.is_set():
                    try:
                        message = pubsub.get_message(timeout=1.0)
                    except Exception as exc:
                        logger.warning(
                            "[websocket] relay: subscriber error (%s) - "
                            "reconnecting.", exc,
                        )
                        break  
                    if message is None or message.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if payload.get("origin") == self._instance_id:

                        continue
                    if self._loop is None:
                        continue
                    event = payload.get("event")
                    data = payload.get("data")
                    if event is None:
                        continue
                    event_id = payload.get("event_id")
                    target_user_id = payload.get("target_user_id")
                    try:
                        if target_user_id is not None:
                            asyncio.run_coroutine_threadsafe(
                                self.broadcast_to_user(
                                    target_user_id, event, data,
                                    _from_relay=True, _event_id=event_id,
                                ),
                                self._loop,
                            )
                        else:
                            asyncio.run_coroutine_threadsafe(
                                self.broadcast(event, data, _from_relay=True, _event_id=event_id),
                                self._loop,
                            )
                    except RuntimeError:
                        continue
            finally:
                try:
                    pubsub.close()
                except Exception:
                    pass

            if self._relay_stop.is_set():
                return

            if self._relay_stop.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, _RELAY_BACKOFF_CAP_SECONDS)

    def _publish_relay(self, event: str, data: dict, event_id: str, target_user_id: str | None) -> None:
        client = cache.get_client()
        if client is None:
            return
        payload = {"origin": self._instance_id, "event_id": event_id, "event": event, "data": data}
        if target_user_id is not None:
            payload["target_user_id"] = target_user_id
        try:
            client.publish(RELAY_CHANNEL, json.dumps(payload, default=str))
        except Exception:
            pass

    def _already_seen(self, event_id: str | None) -> bool:
        if event_id is None:
            return False
        with self._dedupe_lock:
            return event_id in self._seen_event_ids

    def _mark_seen(self, event_id: str | None) -> None:
        if event_id is None:
            return
        with self._dedupe_lock:
            self._seen_event_ids[event_id] = None
            self._seen_event_ids.move_to_end(event_id)
            while len(self._seen_event_ids) > DEDUPE_MAX_ENTRIES:
                self._seen_event_ids.popitem(last=False)

    def _remember_latest_state(self, event: str, data: dict) -> None:
        merge_key = _COALESCE_MERGE_KEY.get(event)
        if not merge_key or not isinstance(data, dict):
            return
        items = data.get("updates")
        if not isinstance(items, list):
            return
        bucket = self._latest_state.setdefault(event, {})
        for item in items:
            if isinstance(item, dict) and merge_key in item:
                bucket[item[merge_key]] = item
        if data.get("timestamp") is not None:
            self._latest_state_ts[event] = data["timestamp"]

    async def _send_latest_state(self, websocket: WebSocket) -> None:
        for event, bucket in self._latest_state.items():
            if not bucket:
                continue
            payload = json.dumps({
                "event": event,
                "data": {
                    "updates": list(bucket.values()),
                    "timestamp": self._latest_state_ts.get(event),
                },
            }, default=str)
            dead = await self._send_one(websocket, payload)
            if dead is not None:

                return

    async def connect(
        self,
        websocket: WebSocket,
        user_id: str | None = None,
        subprotocol: str | None = None,
    ) -> bool:
        if len(self.active_connections) >= MAX_CONNECTIONS:
            await self._reject(websocket, "server at capacity")
            return False
        if user_id is not None:
            per_user = sum(
                1 for uid in self._connection_users.values() if uid == user_id
            )
            if per_user >= MAX_CONNECTIONS_PER_USER:
                await self._reject(websocket, "too many connections for this user")
                return False

        await websocket.accept(subprotocol=subprotocol)
        self.active_connections.append(websocket)
        self._connection_users[websocket] = user_id
        self._last_seen[websocket] = time.monotonic()
        with self._metrics_lock:
            self._connects_total += 1

        await self._send_latest_state(websocket)
        return True

    async def _reject(self, websocket: WebSocket, reason: str) -> None:
        try:
            await websocket.close(code=WS_CLOSE_TRY_AGAIN_LATER, reason=reason)
        except Exception:

            pass
        with self._metrics_lock:
            self._disconnects_total["rejected_at_capacity"] = (
                self._disconnects_total.get("rejected_at_capacity", 0) + 1
            )

    def disconnect(self, websocket: WebSocket, reason: str = "unknown") -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self._connection_users.pop(websocket, None)
        self._last_seen.pop(websocket, None)
        with self._metrics_lock:
            self._disconnects_total[reason] = self._disconnects_total.get(reason, 0) + 1

    def record_activity(self, websocket: WebSocket) -> None:
        self._last_seen[websocket] = time.monotonic()

    async def _send_one(self, connection: WebSocket, payload: str) -> WebSocket | None:
        try:
            await asyncio.wait_for(connection.send_text(payload), timeout=SEND_TIMEOUT_SECONDS)
            return None
        except asyncio.TimeoutError:
            logger.warning("[websocket] send timed out after %ss - dropping stale connection.",
                            SEND_TIMEOUT_SECONDS)
            return connection
        except Exception:
            return connection

    async def broadcast(
        self, event: str, data: dict, *, _from_relay: bool = False, _event_id: str | None = None,
    ) -> None:
        event_id = _event_id or uuid.uuid4().hex
        if self._already_seen(event_id):
            return
        self._mark_seen(event_id)

        self._remember_latest_state(event, data)

        if self.active_connections:
            payload = json.dumps({"event": event, "data": data}, default=str)
            connections = list(self.active_connections)
            results = await asyncio.gather(
                *(self._send_one(connection, payload) for connection in connections),
                return_exceptions=False,
            )
            stale = [connection for connection in results if connection is not None]
            if stale:
                with self._metrics_lock:
                    self._event_send_failures_total[event] = (
                        self._event_send_failures_total.get(event, 0) + len(stale)
                    )
            for connection in stale:
                self.disconnect(connection, reason="send_failed")

        if not _from_relay:

            self._publish_relay(event, data, event_id, target_user_id=None)

    async def broadcast_everywhere(self, event: str, data: dict) -> None:
        await self.broadcast(event, data)

    async def broadcast_to_user(
        self, user_id: str, event: str, data: dict, *, _from_relay: bool = False, _event_id: str | None = None,
    ) -> None:
        event_id = _event_id or uuid.uuid4().hex
        if self._already_seen(event_id):
            return
        self._mark_seen(event_id)

        targets = [
            ws
            for ws, uid in self._connection_users.items()
            if uid is not None and str(uid) == str(user_id)
        ]
        if targets:
            payload = json.dumps({"event": event, "data": data}, default=str)
            results = await asyncio.gather(
                *(self._send_one(connection, payload) for connection in targets),
                return_exceptions=False,
            )
            stale = [connection for connection in results if connection is not None]
            if stale:
                with self._metrics_lock:
                    self._event_send_failures_total[event] = (
                        self._event_send_failures_total.get(event, 0) + len(stale)
                    )
            for connection in stale:
                self.disconnect(connection, reason="send_failed")

        if not _from_relay:
            self._publish_relay(event, data, event_id, target_user_id=str(user_id))

    def notify(self, event: str, data: dict) -> None:
        if self._loop is None:
            return
        merge_key = _COALESCE_MERGE_KEY.get(event)
        if merge_key and isinstance(data, dict) and isinstance(data.get("updates"), list):
            self._buffer_for_coalesce(event, data, merge_key)
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(event, data), self._loop)
        except RuntimeError:
            pass

    def notify_user(self, user_id: str, event: str, data: dict) -> None:
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_to_user(user_id, event, data), self._loop
            )
        except RuntimeError:
            pass

    def _buffer_for_coalesce(self, event: str, data: dict, merge_key: str) -> None:
        schedule_needed = False
        with self._coalesce_lock:
            bucket = self._coalesce_buffers.setdefault(event, {"by_key": {}, "timestamp": None})
            for item in data.get("updates") or []:
                key = item.get(merge_key) if isinstance(item, dict) else None
                bucket["by_key"][key] = item
            if data.get("timestamp") is not None:
                bucket["timestamp"] = data["timestamp"]
            if event not in self._coalesce_scheduled:
                self._coalesce_scheduled.add(event)
                schedule_needed = True

        if not schedule_needed:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._flush_coalesced_after_delay(event), self._loop)
        except RuntimeError:
            with self._coalesce_lock:
                self._coalesce_scheduled.discard(event)

    async def _flush_coalesced_after_delay(self, event: str) -> None:
        await asyncio.sleep(COALESCE_WINDOW_SECONDS)
        with self._coalesce_lock:
            bucket = self._coalesce_buffers.pop(event, None)
            self._coalesce_scheduled.discard(event)
        if not bucket or not bucket["by_key"]:
            return
        merged = {"updates": list(bucket["by_key"].values()), "timestamp": bucket["timestamp"]}
        await self.broadcast(event, merged)

    def start_reaper(self) -> None:
        if self._loop is None:
            return
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        self._reaper_task = self._loop.create_task(self._reap_loop())

    async def stop_reaper(self) -> None:
        if self._reaper_task is not None:
            task, self._reaper_task = self._reaper_task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(REAP_INTERVAL_SECONDS)
                await self._reap_stale()
        except asyncio.CancelledError:
            raise

    async def _reap_stale(self) -> None:
        now = time.monotonic()
        idle = [
            ws for ws, last in list(self._last_seen.items())
            if now - last > STALE_AFTER_SECONDS
        ]
        if not idle:
            return

        ping_payload = json.dumps({"event": "ping", "data": {}})
        for ws in idle:
            dead = await self._send_one(ws, ping_payload)
            if dead is not None:
                self.disconnect(dead, reason="stale_reaped")
            else:
                self._last_seen[ws] = now

manager = ConnectionManager()
