# Realtime WebSocket system

MetroFlow's core UX promise is "backend data changes → WebSocket →
frontend state → UI updates automatically, without a page refresh."
This doc covers `/ws/monitor` (`app/main.py`) and
`app/websocket/manager.py`'s `ConnectionManager`: connection
lifecycle, cross-process delivery, heartbeat, de-duplication,
request-storm coalescing, stale-connection reaping, and reconnect
state resync — plus the two frontend stability issues found while
testing all of the above under real multi-tab, multi-worker
conditions.

## Connection lifecycle

`/ws/monitor` accepts the connection via `manager.connect()`, then
loops on `receive_text()`. Cleanup (`manager.disconnect()`) used to
only run inside `except WebSocketDisconnect` — any *other* exception
escaping `receive_text()`/`send_text()` (a transport error on an
abrupt close, rather than a clean close handshake) skipped cleanup
entirely, leaking the connection in `active_connections`/
`_connection_users` forever (visible in `/health`'s
`websocket_connections` count, and it kept receiving failing send
attempts on every future broadcast). Fixed with
`try/except WebSocketDisconnect/finally: manager.disconnect(websocket)`
— cleanup is now unconditional regardless of how the loop exits.

## Cross-process delivery: the Redis relay

A single elected leader (see
[background-jobs-and-leader-election.md](./background-jobs-and-leader-election.md))
runs the simulator ticks, but a client connected to a *different*
worker process still needs to receive that leader's broadcasts.
`ConnectionManager` runs a background thread per process that
subscribes to a Redis pub/sub channel (`metroflow:ws:relay`):

- `broadcast()` / `broadcast_to_user()` deliver to this process's own
  locally-connected clients immediately, **and** publish the event to
  the relay channel (tagged with this process's instance id).
- Every process subscribes to that channel at startup. On receiving a
  message, it delivers it to its own locally-connected clients —
  unless the message's origin tag matches its own instance id (so a
  process never double-delivers its own event when its own publish
  echoes back).
- If Redis is unreachable, this degrades to "this process only
  delivers to its own clients" — logged once, never raises. Same
  fail-open convention used throughout `app/core/cache.py`.

### Bug: the relay was opt-in, and most call sites opted out

Early on, `broadcast_everywhere()`/`start_relay()` existed as a
*second* method alongside the original, local-process-only
`broadcast()`. Only the crowd simulator and train tracker were
switched to the relay-aware version — `alert_service.py`,
`crowd_service.py` (check-in/check-out), `schedule_service.py`
(delay alerts), and `notification_service.py` all kept calling the
original `notify()`/`notify_user()`. Nothing forced a new call site to
pick the correct one, and a single-process dev environment can't
surface the bug (there's only ever one "local" set of clients).

A second, independent bug compounded this: `notify()`/`notify_user()`
both started with `if self._loop is None or not
self.active_connections: return` — and `not self.active_connections`
means *this process* has zero currently-connected clients, which is
completely normal for one worker among several behind a load balancer.
A request landing on such a worker silently dropped the event
**before it ever reached the relay-publish step** — not just failing
to deliver locally (expected), but failing to reach any other worker's
clients either.

**Fix:** there is now exactly one way to push an event, and it's
always correct. `broadcast()`/`broadcast_to_user()` always deliver
locally *and* publish to the relay, every time, regardless of whether
this process has any local clients right now — the local-only vs.
everywhere split is gone. `notify()`/`notify_user()` no longer
early-return on `not self.active_connections` (only on `self._loop is
None`, meaning the app hasn't finished starting). No service-layer
file needed to change — they all already called `notify()`/
`notify_user()`, which are just correct now, so there's no longer a
non-relay variant a future call site could accidentally pick.

### Bug: the relay didn't survive a startup race or a mid-stream error

`start_relay()` used to check Redis reachability *before* spawning the
background thread — if Redis wasn't reachable at that exact moment
(a container-startup race, a slow Redis boot), the thread was simply
never created, and nothing ever retried for the rest of the process's
life. Separately, `_relay_loop()` ran its subscribe-and-consume logic
once with no outer retry loop — any single error on the live
subscription (a Redis restart, a network blip) `break`ed out of the
loop entirely, the function returned, and since a `threading.Thread`
can't be restarted once its target returns, the relay was gone until
the process itself restarted.

**Fix:** `start_relay()` now unconditionally spawns the thread
(idempotently) and lets the thread own connecting. `_relay_loop()` is
wrapped in an outer `while not self._relay_stop.is_set()` covering
three failure points: no client yet (wait with backoff, re-check),
`subscribe()` itself failing (close, wait, retry), and an established
subscription erroring mid-stream (`break` only the inner consume loop,
then the outer loop reconnects from scratch instead of the thread
exiting). Backoff doubles from a 1s floor to a 30s cap, resetting to
the floor on any success, mirroring `app/core/cache.py`'s own
reconnect backoff shape.

## Heartbeat

The frontend (`LiveSocketProvider.tsx`) sends `{"type":"ping"}` every
15s and force-closes the socket if no message of any kind arrives
within the next 10s. The server used to read every inbound frame —
including the client's own ping — and discard it, so the client's
heartbeat timer was only ever cleared *by accident*, when an unrelated
broadcast happened to arrive in the same window. Whenever there was a
genuine lull (simulator disabled, an admin pausing it, off-peak
hours), a perfectly healthy socket got force-closed roughly every 25s.

Fixed: the receive loop now parses inbound frames and replies to a
ping with an explicit `{"event": "pong", "data": {}}`. The frontend's
`onmessage` handler already treats any message with an `event` field
as proof of life, so no frontend change was required.

## De-duplication and request-storm coalescing

Two defenses added pre-emptively, not in response to an observed
failure:

- **De-duplication.** Every `broadcast()`/`broadcast_to_user()` call is
  tagged with a `uuid4` event id. A small LRU set (`_seen_event_ids`,
  capped at 4096) is checked before any delivery — local or
  relay-originated — so the same event id is never delivered twice.
  This sits alongside the origin-instance check as a second,
  independent layer.
- **Coalescing.** `notify()` recognizes the `{"updates": [...]}` shape
  used by `crowd_update` (check-in/check-out/manual edits) and
  `train_position`, and buffers calls to the same event for 200ms,
  merging by the same per-entity key (`station_id`/`train_id`) the
  frontend already merges by client-side. A burst of N check-ins in
  the same instant now produces one merged broadcast instead of N
  separate delivery passes. 200ms is far below the product's ~5–10s
  update cadence, so nobody perceives added latency. Alerts, delay
  notices, and notifications are **not** coalesced — they're rare
  enough that instant delivery is affordable and desirable (an alert
  shouldn't be debounced).

Verified with 12 real OS threads calling `notify()` at the same
instant (reproducing several check-ins landing on FastAPI's threadpool
together): merged into exactly one WebSocket message with all 12
station updates, repeatably.

## Stale-connection reaping

A dead connection used to only be discovered reactively, when a
broadcast happened to try (and fail/time out) sending to it — a
connection with no traffic for a while could sit around indefinitely
even after the network dropped it. `ConnectionManager` now tracks
`_last_seen` per connection (updated on every inbound frame, including
a client ping) and a background task (every 20s) sends an unsolicited
ping to any connection quiet for more than 45s; if that send fails or
times out, the connection is dropped immediately.

## Reconnect: state resync

Cross-process fan-out and the heartbeat/cleanup fixes above mean a
reconnecting client's *transport* recovers correctly and starts
receiving new ticks right away. But `manager.connect()` used to just
register the new socket and return — it never sent anything, so a
(re)connected client stayed silent until the *next* simulator tick
(up to 5–10s later). Worse: any partial, request-triggered update that
landed while the client was offline (a single check-in's
`crowd_update` for one station) was gone for good by the time the next
full tick overwrote that station's row — a client that reconnected in
between briefly saw stale data with no way to know.

Fixed with a small last-known-value cache for the two stateful,
`{"updates": [...]}`-shaped events (`crowd_update`/`train_position`):

- `_remember_latest_state()` folds every `broadcast()` (local or
  relayed) into this cache, keyed by the same merge key
  (`station_id`/`train_id`), so a partial update only touches the
  entries it actually changed.
- `connect()` now calls `_send_latest_state()` right after registering
  the new socket — sends the full accumulated cache directly to that
  one connection (not broadcast/relayed/dedup-tracked, since it's a
  resync for the new arrival, not a new event for everyone else who's
  already current).

Net effect: disconnect → reconnect → latest state arrives immediately,
before a single new tick — then live updates continue on the normal
cadence. Verified end-to-end (real, non-mocked `fastapi`/`starlette`/
`websockets` stack): connect → full crowd tick + full train tick + one
partial check-in-style update → disconnect → reconnect — the *first*
message received after reconnect is the resynced `crowd_update`,
correctly showing the newer partial value for the touched station and
the last full-tick value for every other station (proving merge-by-key,
not overwrite), followed by the resynced `train_position`, followed by
live ticks continuing normally.

Alerts/notifications/delay notices are untouched by this — they were
never meant to be "caught up" on; they're discrete/rare events, not
continuously-updated state. The frontend's own REST-refetch-on-reconnect
(RTK Query tag invalidation) remains a valid complementary layer,
particularly for user-scoped data the WebSocket-level cache doesn't
cover.

## Frontend: two stability issues found under real multi-tab testing

### Backgrounded tabs silently stalled

`LiveSocketProvider.tsx` scheduled every incoming batch of messages to
flush via `requestAnimationFrame`. Most browsers fully suspend (not
just throttle) `requestAnimationFrame` callbacks in a tab that isn't
visible. With the dashboard open in two tabs, whichever one isn't
currently focused stopped flushing entirely — `pendingMessages` just
grew (unbounded for event types with no merge key) instead of updating
every ~5–10s, only catching up in one large jump when refocused.

Fixed: a hidden tab now schedules its flush with a plain timer instead
of `requestAnimationFrame`, and a `visibilitychange` listener flushes
any queued backlog immediately the moment the tab becomes visible
again. The WebSocket connection itself was never affected — browsers
don't suspend an open socket the same way — only the render-side flush
was.

### KPI cards had no fallback if the socket was ever down

`KPISection.tsx`'s `crowd`, `delayed`, and `predictions` RTK Query
hooks had no polling interval configured at all, unlike every other
live-data widget on the dashboard (`RecentAlerts.tsx`,
`TrainStatusTable.tsx`, `LiveTrainMap.tsx`, `ActivityTimeline.tsx`),
which all poll on a fallback cadence exactly while the socket is down
(`isConnected ? 0 : FALLBACK_POLL_MS`). If the socket was ever slow to
connect, dropped for an extended period, or never connects at all (a
proxy/firewall blocking the WebSocket upgrade), the headline cards had
no way to self-heal. Fixed by bringing those three hooks in line with
the same pattern already used elsewhere.

## Event names and payload shapes

None of the fixes above changed `/ws/monitor`'s event names or payload
shapes (`crowd_update`, `train_position`, `delay_alert`,
`station_alert`, `notification`, `notification_read`,
`notification_all_read`) — every frontend consumer of these events
needed no changes beyond the two stability fixes above.
