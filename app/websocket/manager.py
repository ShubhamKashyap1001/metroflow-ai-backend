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

Cross-process delivery, heartbeat, de-duplication, and reconnect state
resync
--------------------------------------------------------------------
Several real bugs existed in earlier versions of this file and its
call sites, explained in full in docs/realtime-websocket-system.md;
summarised here since they shaped this file's structure:

1. **Cross-process fan-out was opt-in, and most call sites opted out.**
   An earlier version added `broadcast_everywhere()` / `start_relay()`
   (Redis pub/sub) so simulator-tick events reach every worker
   process, not just whichever one is currently elected leader. But
   that was a *second*, separate method next to the original
   `broadcast()` - and 5 of the 7 real call sites (alert_service,
   crowd_service's request-triggered check-in update,
   schedule_service's delay alert, and 3 of notification_service's 4
   push sites) called the plain, non-relaying
   `notify()`/`notify_user()`, because nothing forced them to pick the
   relay-aware variant. Under N>1 workers, an alert raised by a
   request that happened to land on worker B never reached a dashboard
   connected to worker A. **Fixed by removing the second API
   entirely**: `broadcast()`/`broadcast_to_user()` (and therefore
   `notify()`/`notify_user()`, which just schedule them) now always
   relay across every process whenever Redis is available. There is
   only one correct way to push an event now, so this class of bug
   cannot recur through a future call site forgetting to opt in.
   `broadcast_everywhere()` is kept as a no-op-different alias only so
   the two simulator files that already called it don't need edits.

2. **The sync wrappers silently dropped events on an idle worker.**
   Independent of bug 1, `notify()`/`notify_user()` both returned
   immediately if *this* process had zero locally-connected clients
   (`not self.active_connections`) - before ever reaching the
   broadcast call. Under a load balancer spreading connections across
   N workers, any worker with zero currently-connected clients at the
   moment a request landed on it would drop that alert/notification
   for the whole cluster, not just for itself, because it never even
   published to Redis. Fixed by removing that guard: whether or not
   *this* process has local clients right now is irrelevant to whether
   the event needs to be relayed for everyone else's.

3. **No de-duplication and no real heartbeat.** See `_seen_event_ids`
   and the `start_reaper()`/`_reap_stale()` machinery below.

4. **The relay thread never recovered from a startup race or a
   mid-stream error.** `start_relay()` used to check Redis
   reachability before spawning the background thread, so a startup
   race where Redis wasn't reachable yet meant the thread was never
   created at all, and nothing ever retried. A later error on an
   already-subscribed connection permanently exited the thread the
   same way. Fixed with an outer retry loop and backoff inside
   `_relay_loop()` itself - see `_RELAY_BACKOFF_FLOOR_SECONDS` below.

5. **A reconnecting client stayed silent until the next tick, and lost
   any update that landed while it was offline.** `connect()` used to
   just register the new socket and return. Fixed with a small
   last-known-state cache (`_remember_latest_state`/
   `_send_latest_state`) that's sent to a newly (re)connected client
   immediately, before the next live tick - see below.
"""
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

# Maximum number of simultaneous WebSocket connections this process
# will hold open at once (see connect()/_reject() below), and a
# per-authenticated-user sub-limit on top of it. Read from
# app.core.config.settings when that module is importable (the normal
# running app, where these are also configurable via the
# WS_MAX_CONNECTIONS/WS_MAX_CONNECTIONS_PER_USER env vars through
# pydantic-settings); falls back to reading those same env vars
# directly otherwise, so this module keeps working unmodified inside
# the offline scripts/verify_ws_*.py harnesses, which inject a minimal
# fake `fastapi` and never construct a real app.core.config.Settings
# (which requires DATABASE_URL/SUPABASE_URL/SUPABASE_KEY to be set).
try:
    from app.core.config import settings as _settings

    MAX_CONNECTIONS = int(getattr(_settings, "WS_MAX_CONNECTIONS", 200))
    MAX_CONNECTIONS_PER_USER = int(getattr(_settings, "WS_MAX_CONNECTIONS_PER_USER", 20))
except Exception:
    MAX_CONNECTIONS = int(os.environ.get("WS_MAX_CONNECTIONS", "200"))
    MAX_CONNECTIONS_PER_USER = int(os.environ.get("WS_MAX_CONNECTIONS_PER_USER", "20"))

# WS close code sent to a connection rejected for being over capacity,
# before its handshake is ever accepted (see _reject()). 1013 is the
# standard "Try Again Later" code (RFC 6455 / IANA registry) for a
# server that's temporarily overloaded - exactly this situation, and
# distinct from an auth/policy rejection (which would be 1008).
WS_CLOSE_TRY_AGAIN_LATER = 1013

# Channel used to fan any event out to every API worker process, not
# just whichever one generated it (simulator ticks) or happened to
# handle the triggering HTTP request (alerts/notifications/delays/
# check-in crowd updates).
RELAY_CHANNEL = "metroflow:ws:relay"

# Relay auto-reconnect backoff. Mirrors app/core/cache.py's
# own reconnect backoff shape (floor/cap, doubling) for consistency,
# though this is tracked independently inside the relay thread itself
# rather than in cache.py, since it needs to distinguish "no client
# yet" from "had a client, then it errored" for its own retry pacing.
_RELAY_BACKOFF_FLOOR_SECONDS = 1.0
_RELAY_BACKOFF_CAP_SECONDS = 30.0

# How many recently-seen event ids to remember for de-duplication (see
# _already_seen/_mark_seen). Bounded/LRU so this never grows unbounded
# on a long-lived process.
DEDUPE_MAX_ENTRIES = 4096

# A connection we haven't heard *anything* from in this long (no
# client-sent ping, no other inbound frame) gets an unsolicited
# server-initiated ping as a liveness probe (see _reap_stale). Set
# comfortably above the frontend's own heartbeat cadence (ping every
# 15s, 10s reply timeout = a healthy tab always generates inbound
# activity at least every 25s) so a healthy connection is never
# probed, only ones that have gone quiet.
STALE_AFTER_SECONDS = 45
REAP_INTERVAL_SECONDS = 20

# Request-triggered single-item crowd updates (check-in/check-out,
# manual admin count edits) are coalesced for this long before being
# flushed as one merged, multi-station update - see notify()/
# _buffer_for_coalesce(). Well under the ~5-10s update cadence the
# product requires, so nobody perceives any added latency; this exists
# purely to stop a burst of requests (a "request storm" - many
# check-ins in the same instant, a retried client, a small load test)
# from turning into an equally large burst of individual WebSocket
# broadcasts, each of which is its own asyncio.gather() over every
# connected client.
COALESCE_WINDOW_SECONDS = 0.2

# Only these events carry the `{"updates": [...]}` shape (a list of
# per-entity items, each keyed by this field) that can be safely
# merged by last-value-wins - the same shape/merge key the frontend
# already uses for its own client-side coalescing in
# LiveSocketProvider.tsx. Every other event (alerts, notifications,
# delay notices) is small/rare enough that debouncing it would only
# add latency for no benefit, so those are still dispatched instantly.
_COALESCE_MERGE_KEY = {
    events.CROWD_UPDATE: "station_id",
    events.TRAIN_POSITION: "train_id",
}

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        # Parallel mapping: which user (if any) each open connection
        # belongs to. A connection with no entry here (or a None
        # value) is anonymous/broadcast-only.
        self._connection_users: dict[WebSocket, str | None] = {}
        # Last time we heard ANYTHING from this connection (a client
        # heartbeat ping, or - defensively - any other inbound frame).
        # Used by the reaper to find connections that have gone quiet;
        # see start_reaper()/_reap_stale().
        self._last_seen: dict[WebSocket, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

        # Identifies THIS process among any others sharing the relay
        # channel, so a message this process published (and already
        # delivered to its own clients directly) isn't delivered to
        # them a second time when the subscriber echoes it back.
        self._instance_id = uuid.uuid4().hex
        self._relay_thread: threading.Thread | None = None
        self._relay_stop = threading.Event()

        # De-duplication: every locally-*or*-relay-originated delivery
        # is tagged with a per-event uuid. Belt-and-suspenders on top
        # of the origin check above - guards against a message being
        # delivered twice for any reason (e.g. a relay reconnect
        # replay), which is exactly the "duplicate events" failure
        # mode this phase was asked to test for.
        self._seen_event_ids: "OrderedDict[str, None]" = OrderedDict()
        self._dedupe_lock = threading.Lock()

        # Request-triggered update coalescing (see COALESCE_WINDOW_
        # SECONDS above). Guarded by a real Lock, not just relying on
        # asyncio - notify()/_buffer_for_coalesce() are called from
        # FastAPI's sync threadpool, so multiple OS threads really can
        # race here.
        self._coalesce_buffers: dict[str, dict] = {}
        self._coalesce_scheduled: set[str] = set()
        self._coalesce_lock = threading.Lock()

        # Background reaper task (see start_reaper/_reap_stale) that
        # proactively drops connections that have gone silent, instead
        # of only ever noticing a dead connection when the next
        # broadcast happens to try (and fail) to send to it.
        self._reaper_task: asyncio.Task | None = None

        # Reconnect state resync (see docs/realtime-websocket-system.md).
        # Last-known value of every "updates": [...] -shaped event
        # (crowd_update/train_position - the same two events
        # _COALESCE_MERGE_KEY already knows how to merge by key),
        # keyed by event name -> {merge_key_value: item}. Kept up to
        # date on every broadcast() (including relayed ones, so a
        # snapshot built from another process's simulator tick is just
        # as current as one built locally). A newly-connect()ed socket
        # is sent this immediately, so "disconnect -> reconnect" no
        # longer means sitting on stale data for however long it takes
        # the next simulator tick (up to SIMULATOR_INTERVAL_SECONDS) to
        # come around - the client has the current state the instant
        # the socket reopens, before a single new tick has happened.
        self._latest_state: dict[str, dict] = {}
        self._latest_state_ts: dict[str, object] = {}

        # Operational counters for app/core/metrics.py's WebSocket
        # collector (connection/reconnect/event-failure metrics).
        # Deliberately plain ints/dicts guarded by a stdlib Lock, NOT
        # prometheus_client Counters/Gauges - this module has to stay
        # importable with nothing beyond fastapi/app.core.cache in the
        # offline verification harnesses under scripts/verify_ws_*.py
        # (see their own module docstrings), which inject fake
        # stand-ins for those two and would break if this module
        # gained a hard import on prometheus_client. app/core/metrics.py
        # reads these back via get_metrics_snapshot() at scrape time -
        # a pull, not a push - and republishes them as real Prometheus
        # series under their own metric names/help text.
        #
        # "Reconnect" has no separate counter of its own: a dropped
        # WebSocket carries no server-side identity a new TCP
        # handshake could resume, so from this process's point of view
        # every reconnect attempt IS simply a new connect() call (the
        # same thing the frontend's own LiveSocketProvider.tsx
        # scheduleReconnect()/connect() does - close the old socket,
        # open a brand new one). Reconnect churn is therefore the
        # relationship between _connects_total and
        # _disconnects_total over time, not a separate figure - the
        # same way any WebSocket service's dashboards derive it.
        self._metrics_lock = threading.Lock()
        self._connects_total = 0
        self._disconnects_total: dict[str, int] = {}
        self._event_send_failures_total: dict[str, int] = {}

    def get_metrics_snapshot(self) -> dict:
        """Point-in-time snapshot for app/core/metrics.py's Prometheus
        collector. Never called from any hot path in this module
        itself - only from collect(), at scrape time."""
        with self._metrics_lock:
            return {
                "connects_total": self._connects_total,
                "disconnects_total": dict(self._disconnects_total),
                "event_send_failures_total": dict(self._event_send_failures_total),
                "active_connections": len(self.active_connections),
            }

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Call once at app startup (inside the lifespan, which runs on
        the real event loop) so notify() has a loop to schedule onto."""
        self._loop = loop or asyncio.get_event_loop()

    # ------------------------------------------------------------------
    # Cross-process relay (Redis pub/sub)
    # ------------------------------------------------------------------

    def start_relay(self) -> None:
        """Start (once) a background thread that owns connecting,
        subscribing, and RE-connecting to the cross-process relay
        channel, so THIS process's WebSocket clients get every event -
        simulator ticks AND request-triggered alerts/notifications/
        delays/check-in updates - even on the (very normal) case where
        a DIFFERENT process generated it, either because it's the
        elected simulator leader (leader_election.py) or because it
        happened to be the one that handled that particular HTTP
        request.

        This used to check `cache.get_client()` right here
        and simply not start the thread at all if Redis wasn't
        reachable yet - so a Redis-not-up-yet startup race permanently
        disabled the relay for the process's whole life (bug #1), and
        even once started, the thread itself gave up for good on its
        first pubsub error rather than reconnecting (bug #2; see
        _relay_loop()). Fixed by always starting the thread regardless
        of whether Redis is reachable *right now* - the thread itself
        (see _relay_loop()) owns retrying with backoff, both for the
        initial connect and for any later disconnect, so a startup
        race or a later Redis blip is never a permanent condition for
        this process.
        """
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
            # Bounded join so shutdown never hangs: the loop polls
            # get_message() with a 1s timeout and checks _relay_stop
            # at least that often, so 2s is comfortably above the
            # worst case.
            thread.join(timeout=2.0)

    def _relay_loop(self) -> None:
        """Runs on a dedicated background thread (redis-py's pub/sub API
        is synchronous/blocking). Owns its own connect/subscribe/consume
        lifecycle and RETRIES it, with backoff, for as long as the
        process runs - instead of returning (and thereby permanently
        disabling the relay) the moment Redis isn't reachable yet or a
        mid-stream error occurs. Three layers this loops over:

          1. No client available yet (Redis down/not up yet at
             startup, or a later outage per app/core/cache.py's own
             reconnect state machine) - wait with backoff, then check
             again. This is bug #1: previously start_relay() simply
             never started this thread at all in this case.
          2. subscribe() itself fails (e.g. connection drops between
             obtaining the client and subscribing) - close, wait with
             backoff, retry from the top.
          3. An established subscription errors mid-stream (Redis
             restarts, network blip) - this used to `return` and kill
             the thread for good (bug #2). Now it closes the dead
             pubsub and falls through to reconnect instead.

        Polls get_message() with a 1s timeout (rather than a blocking
        listen()) so both `_relay_stop` and backoff waits take effect
        within ~1s instead of hanging until the next message arrives.
        """
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
                        break  # fall through to close + reconnect below
                    if message is None or message.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if payload.get("origin") == self._instance_id:
                        # Our own publish, echoed back by the subscription -
                        # we already delivered it to our own clients directly
                        # before publishing. Skip, don't double-deliver.
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
            # We had a working subscription and lost it (not a clean
            # stop_relay() call) - brief backoff before reconnecting so
            # a sustained outage doesn't spin-loop.
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

    # ------------------------------------------------------------------
    # De-duplication
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _remember_latest_state(self, event: str, data: dict) -> None:
        """Fold a broadcast's `{"updates": [...]}` payload into the
        per-event last-known-state cache (see `_latest_state` in
        __init__), keyed the same way _buffer_for_coalesce() keys its
        merge buffer. Called for every broadcast (local or relayed) of
        a stateful event - NOT just full-snapshot ticks - so a partial,
        request-triggered update (a single check-in's crowd_update)
        updates just that one entry rather than clobbering everything
        else this process has ever seen."""
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
        """Catch a just-connected socket up on current state immediately
        - see the `_latest_state` docstring in __init__ for why this
        exists. Sent directly to this one connection only (never
        broadcast/relayed/dedup-tracked): it's a resync for the new
        arrival, not a new event for everyone else who's already
        current."""
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
                # Connection died before it even got its resync - the
                # normal receive loop in main.py will see the
                # disconnect and call manager.disconnect() itself; no
                # special handling needed here beyond not raising.
                return

    async def connect(
        self,
        websocket: WebSocket,
        user_id: str | None = None,
        subprotocol: str | None = None,
    ) -> bool:
        """Returns True if the connection was accepted and registered,
        False if it was rejected for being at capacity (see
        MAX_CONNECTIONS/MAX_CONNECTIONS_PER_USER above). A rejected
        socket is closed (see _reject()) *before* websocket.accept()
        is ever called, so it's never added to active_connections/
        _connection_users - it doesn't occupy a connection slot or any
        other per-connection memory, and every already-connected
        client is completely unaffected. Callers (see /ws/monitor in
        app/main.py) must check the return value and stop - the socket
        has already been closed, there's nothing left to accept()."""
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

        # `subprotocol` is the single value (out of whatever the client
        # offered via Sec-WebSocket-Protocol) we're accepting the
        # handshake with - required by the WS spec whenever the client
        # sent that header, or most browsers abort the connection. See
        # /ws/monitor in main.py, which uses this to carry the auth
        # token off the URL and onto that header instead.
        await websocket.accept(subprotocol=subprotocol)
        self.active_connections.append(websocket)
        self._connection_users[websocket] = user_id
        self._last_seen[websocket] = time.monotonic()
        with self._metrics_lock:
            self._connects_total += 1
        # Send whatever state we already know about right
        # away - covers both a first-ever connection (nothing to lose)
        # and a reconnect (this is exactly the state that would
        # otherwise only arrive on the next simulator tick, up to
        # SIMULATOR_INTERVAL_SECONDS later).
        await self._send_latest_state(websocket)
        return True

    async def _reject(self, websocket: WebSocket, reason: str) -> None:
        """Cleanly deny a handshake that hasn't been accept()ed yet -
        never touches active_connections/_connection_users/_last_seen,
        so a rejected connection can never end up stored anywhere.
        Sending `websocket.close` before `websocket.accept` is a
        standard ASGI/WebSocket handshake denial (the client sees the
        upgrade fail, e.g. as an HTTP 403), not a post-accept
        disconnect, so this deliberately does NOT go through
        disconnect() - there is nothing in the manager's state to
        clean up."""
        try:
            await websocket.close(code=WS_CLOSE_TRY_AGAIN_LATER, reason=reason)
        except Exception:
            # Client may have already given up on the handshake -
            # either way, it was never registered, so there's nothing
            # left to do.
            pass
        with self._metrics_lock:
            self._disconnects_total["rejected_at_capacity"] = (
                self._disconnects_total.get("rejected_at_capacity", 0) + 1
            )

    def disconnect(self, websocket: WebSocket, reason: str = "unknown") -> None:
        """`reason` is a short, fixed label value for
        ws_disconnects_total (see app/core/metrics.py) - e.g.
        "client_close", "error", "stale_reaped", "send_failed". Never
        required: every existing call site keeps working unchanged if
        it doesn't pass one, it just gets counted under "unknown"."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self._connection_users.pop(websocket, None)
        self._last_seen.pop(websocket, None)
        with self._metrics_lock:
            self._disconnects_total[reason] = self._disconnects_total.get(reason, 0) + 1

    def record_activity(self, websocket: WebSocket) -> None:
        """Call whenever an inbound frame is received on this
        connection (see /ws/monitor's receive loop in app/main.py).
        Any inbound frame counts as proof of life, not just a ping -
        this is what start_reaper()/_reap_stale() checks against."""
        self._last_seen[websocket] = time.monotonic()

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

    # ------------------------------------------------------------------
    # Broadcasting - the two primitives everything else is built on.
    # Both ALWAYS relay across every process (when Redis is available):
    # there is deliberately no separate "local-only" variant any more
    # (see the module docstring at the top of this file for why).
    # ------------------------------------------------------------------

    async def broadcast(
        self, event: str, data: dict, *, _from_relay: bool = False, _event_id: str | None = None,
    ) -> None:
        event_id = _event_id or uuid.uuid4().hex
        if self._already_seen(event_id):
            return
        self._mark_seen(event_id)
        # Update the resync cache for EVERY broadcast of a
        # stateful event, local or relayed, dedup-checked above like
        # everything else - so a socket that (re)connects to this
        # process sees state that originated on another process too.
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
            # Relay even if THIS process has zero local connections
            # right now - other processes' clients still need it (see
            # bug #2 in the module docstring).
            self._publish_relay(event, data, event_id, target_user_id=None)

    async def broadcast_everywhere(self, event: str, data: dict) -> None:
        """Deprecated alias kept only so app/simulator/csv_replay_simulator.py
        and app/simulator/train_simulator.py don't need edits:
        broadcast() itself now always fans out cluster-wide, so there is
        no behavioural difference any more."""
        await self.broadcast(event, data)

    async def broadcast_to_user(
        self, user_id: str, event: str, data: dict, *, _from_relay: bool = False, _event_id: str | None = None,
    ) -> None:
        """Deliver only to connection(s) registered under this user_id
        (a user with the app open in multiple tabs gets it in all of
        them, on whichever worker process each tab happens to be
        connected to). No-op for THIS process if that user has no
        locally-open connection right now - this is push-on-top-of-pull,
        so the row is still there next time they poll/load the
        notification center - but still relays cluster-wide in case
        their tab is connected to a different worker."""
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

    # ------------------------------------------------------------------
    # Sync, thread-safe entry points for FastAPI's plain `def` routes/
    # services (crowd_service, alert_service, schedule_service,
    # notification_service) which run in the threadpool, not on the
    # event loop.
    # ------------------------------------------------------------------

    def notify(self, event: str, data: dict) -> None:
        """Sync/thread-safe fire-and-forget broadcast. Safe to call
        from sync service functions (alert_service.create_alert,
        schedule_service.handle_delay, etc.) that run inside FastAPI's
        threadpool, as well as from async code already on the loop.

        crowd_update (and any future event with the same
        `{"updates": [...]}` shape) is debounced/merged for
        COALESCE_WINDOW_SECONDS instead of being dispatched
        immediately - see _buffer_for_coalesce()."""
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
        """Sync/thread-safe fire-and-forget version of
        broadcast_to_user(), for use from plain `def` service
        functions running in FastAPI's threadpool (e.g.
        notification_service.create_notification)."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_to_user(user_id, event, data), self._loop
            )
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Request-storm coalescing for notify()
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Stale-connection reaper
    # ------------------------------------------------------------------

    def start_reaper(self) -> None:
        """Begin proactively checking for connections that have gone
        quiet (see STALE_AFTER_SECONDS). Complements the existing
        reactive cleanup in broadcast()/broadcast_to_user() (which only
        ever notices a dead connection when it happens to try, and
        fail, to send to it): on a quiet page with infrequent events,
        or a worker process that currently holds no simulator
        leadership and is only relaying occasional events, a half-open
        connection (network interruption, laptop sleep, wifi drop -
        anything that doesn't cleanly send a WebSocket close frame)
        could otherwise sit in active_connections indefinitely."""
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
        # An unsolicited liveness probe. If the OS-level send itself
        # fails or times out, the connection is genuinely gone (a
        # half-open TCP connection - the client vanished without a
        # close frame) and is dropped. If the send succeeds we can't
        # be 100% certain the far end is still there (a TCP send can
        # complete into a kernel buffer before a dead peer's absence
        # is noticed), but this is the same best-effort liveness check
        # every WebSocket heartbeat scheme makes; a genuinely dead
        # peer that slips through one cycle will fail the next.
        ping_payload = json.dumps({"event": "ping", "data": {}})
        for ws in idle:
            dead = await self._send_one(ws, ping_payload)
            if dead is not None:
                self.disconnect(dead, reason="stale_reaped")
            else:
                self._last_seen[ws] = now

manager = ConnectionManager()
