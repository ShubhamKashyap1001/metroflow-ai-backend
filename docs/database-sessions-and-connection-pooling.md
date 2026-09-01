# Database sessions & connection pooling

Covers SQLAlchemy `Session` lifecycle across request handlers,
background loops, and thread-pool code; connection-pool sizing; and
guarding against a worker×pool configuration that would overrun what
Postgres can actually serve.

## Request-scoped session lifecycle — reviewed, already correct

```python
def get_db():
    db = SessionLocal()
    try:
        yield db
        if db.dirty or db.new or db.deleted:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

One session per request, explicit rollback on any unhandled exception,
a defensive rollback if a route forgot to commit its own pending
changes, and `close()` always runs via `finally` regardless of
outcome. No module-level or otherwise shared `Session` object exists
anywhere in the app (verified via an AST scan of every module-level
assignment, not just a grep — a grep-based check can miss a session
built across two statements).

## Long-held connections during slow external I/O

### Bug: alert dispatch held a DB connection across email/SMS sends

`alert_service._dispatch()` runs as a background task with its own DB
session — correctly never the request's own session. But that session
stayed open for the function's **entire body**:

```python
db = SessionLocal()
try:
    alert = db.get(Alert, alert_id)              # connection checked out here
    ...
    results = send_alert_emails(...)              # blocking SMTP, connection still checked out
    _log_results(db, alert.id, ..., results)
    sms_results = send_alert_sms(...)              # blocking Twilio HTTP POST, connection STILL checked out
    _log_results(db, alert.id, ..., sms_results)
finally:
    db.close()
```

SQLAlchemy's `Session` checks out an actual pooled connection lazily,
on its first query — and doesn't release it until the transaction ends
(commit/rollback), which here didn't happen until after both email and
SMS sends returned. For any station with more than a handful of active
users, or a slow/degraded provider endpoint, that's realistically
seconds spent holding one pool slot that could otherwise be serving a
request — not a long-running *query*, but a long-held *connection*
across unrelated slow I/O.

**Fix:** restructured into three independent steps, each with its own
short-lived session — read (fetch everything needed into plain Python
values, close immediately), send (no DB session open at all), log
(open a second short session purely to write the result rows and
commit). See
[notification-delivery.md](./notification-delivery.md) for how this
interacts with the dedicated notification thread pool added
separately.

Measured with a concurrency harness against a small pool (size 5,
timeout 2s), 20 concurrent simulated alert dispatches: before the fix,
10/20 succeeded and 10/20 hit a controlled timeout; after, 20/20
succeeded, 0/20 timed out.

### Investigated and ruled out: JWKS fetch during `get_current_user`

`get_current_user` calls `_decode_supabase_token(token)`, which does a
blocking JWKS refetch (`urllib.request.urlopen(timeout=5)`) whenever
its 600s cache is stale — the same *shape* of bug as the alert-dispatch
one, and on the hot path of nearly every authenticated request, so it
was investigated closely. It isn't a live bug: SQLAlchemy's `Session`
acquires its pooled connection lazily on its first *query*, not at
construction — `Depends(get_db)` becoming available happens before any
query has run, and the JWKS fetch runs before the first query
(`_get_or_create_profile`). No pool connection is actually checked out
during the JWKS fetch. Recorded here so this doesn't get
re-investigated from scratch later.

## No server-side statement timeout

`pool_pre_ping=True` catches a *dead* connection before handing it
out, and `pool_recycle` retires old connections proactively — neither
protects against a connection that's perfectly healthy but stuck
mid-query (a lock wait, an unindexed scan on a large table, a network
blip partway through a round trip). `pool_timeout` doesn't help
either: it only bounds how long a *new* request waits to *acquire* a
connection, not how long an already-checked-out connection is allowed
to keep running one statement.

Fixed with `DB_STATEMENT_TIMEOUT_MS` (default 30000 = 30s;
`app/core/config.py`), passed to Postgres via psycopg2's `options`
connect arg (`-c statement_timeout=<ms>`) in
`app/database/database.py`, applied only when `DATABASE_URL` is a
`postgresql` URL. A statement that runs longer than this is now
cancelled by the server itself, raising a normal, catchable
`sqlalchemy.exc.OperationalError` — protecting the connection from
ever getting stuck, one layer earlier than `pool_timeout` alone would.

## Pool sizing — audited, deliberately left unchanged

`DB_POOL_SIZE=15`, `DB_MAX_OVERFLOW=25` → 40 connections per process.
**Do not blindly increase these** — raising them without fixing the
actual bug above (long connection holds during alert dispatch) would
only mask the symptom, consume more of Postgres's own connection
budget, and make the same bug harder to notice next time it mattered.

`pool_timeout=10s` bounds how long a request waits for a connection
before failing controllably (503 via `db_pool_exhausted_handler`).
`pool_recycle=300s` is reasonable for hosted Postgres providers that
drop idle connections on their own schedule. `pool_pre_ping=True` is
already correct.

## Worker × pool capacity: refusing to boot an unsafe configuration

Under leader-election-based multi-worker deployment (see
[background-jobs-and-leader-election.md](./background-jobs-and-leader-election.md)),
the real cluster-wide connection ceiling is `(DB_POOL_SIZE +
DB_MAX_OVERFLOW) × worker count`, not just the per-process number — a
fact that wasn't visible anywhere. A first pass added an informational
log line stating the formula, but it never knew the real worker count
or Postgres's real `max_connections`, so it could only log a formula,
never actually catch a genuinely unsafe configuration before it failed
under load.

Fixed with:

- **`WEB_CONCURRENCY`** (new setting, default `1`) — the number of
  worker processes this deployment runs, named to match the
  gunicorn/Heroku-style env var convention so it's set once wherever
  the process manager is configured.
- **`DB_CONNECTION_RESERVE`** (new setting, default `10`) — connections
  Postgres needs to have spare outside this app's own pool ceiling,
  for migrations, admin sessions, and monitoring tools.
- `app/database/database.py`:
  - `_fetch_postgres_max_connections()` — a single ad-hoc, 5s-timeout
    `psycopg2` connection (deliberately *not* through the pooled
    engine, so this check can never itself consume a pool slot), runs
    `SHOW max_connections;`. Returns `None` (never raises) if Postgres
    isn't reachable right now, so an availability problem degrades
    this to "unverified" instead of blocking startup for an unrelated
    reason.
  - `check_pool_capacity(...)` — pure arithmetic, no I/O: computes
    `(pool_size + max_overflow) × web_concurrency` and compares it to
    `max_connections - reserve`. Raises `RuntimeError` with an
    actionable message (names the exact settings to change) if the
    ceiling would exceed safe capacity.
  - `_verify_pool_capacity_or_raise()` — calls both at import time
    (app startup, and any script that imports the engine) and refuses
    to boot if the config is unsafe. If `max_connections` couldn't be
    determined, falls back to logging the formula instead of blocking.

No default numeric value was changed — at the shipped defaults
(`WEB_CONCURRENCY=1`) against a typical `max_connections=100`
Postgres, the check passes with room to spare (`40 ≤ 90`); it only
refuses to start when an operator's own scaling choice (more workers,
or a smaller Postgres tier) would actually be unsafe.

## Simulator loop session hygiene

`csv_replay_simulator.run_forever()`, `train_simulator.run_forever()`,
and `retention.run_forever()` each open a session per tick and close
it in `finally`. If the tick function raised after staging changes but
before its own commit, `close()` alone happens to roll back the open
transaction (documented SQLAlchemy behavior), so this was never a live
bug — but relying on that implicitly is fragile against a future
refactor. An explicit `db.rollback()` was added in the `except` block
of all three, before `close()`, so a dirty session is never handed
back to the pool by accident. `retention.run_forever()` particularly
benefits, since one pass does three separate query+commit steps on the
same session.

## Row-level locking for check-in/check-out — reviewed, already correct

Covered in
[crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md) —
`apply_live_state_delta()` uses `SELECT ... FOR UPDATE` to serialize
concurrent same-station updates, and its caller commits and broadcasts
promptly, with no lock held across anything slow.
