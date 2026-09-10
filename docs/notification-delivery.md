# Notification delivery — dispatch isolation, dedup, and retry

Covers `app/core/notification_executor.py`, `app/core/email.py`,
`app/core/sms.py`, and the resolve-alert path in
`app/services/alert_service.py`. This is the Alert & Notification
Module's outbound email/SMS pipeline: how it's kept from blocking
unrelated API requests, how it avoids sending the same resolution
notification twice, and how it survives a temporary provider outage.

## Dedicated thread pool: isolating slow sends from the rest of the API

Alert dispatch used to be scheduled with FastAPI's
`BackgroundTasks.add_task(...)`. That does return the *triggering*
request's response immediately, but Starlette runs a sync background
task via `run_in_threadpool`, which hands it to AnyIO's default worker
thread limiter — the exact same shared, capacity-limited thread pool
(`anyio.to_thread`, default 40 tokens) that nearly every plain `def`
route handler in this app also runs on (list_alerts, crowd reads,
check-ins, admin actions — almost nothing here is `async def`).

`app/core/email.py`/`app/core/sms.py` are deliberately slow: one
blocking SMTP send or Twilio HTTP POST per recipient, sequentially,
each with its own 15s timeout. A handful of alerts raised close
together (or one alert with many recipients) can occupy several of
those 40 shared slots for seconds at a time. Once enough are in
flight, *unrelated* requests — which also need a slot from that same
pool just to run their own fast handler — start queuing behind the
slow notification sends, even though the request that triggered the
dispatch already got its response. Measured locally: a burst of 45
concurrent alert dispatches drove concurrent `GET /alerts` latency
from ~10ms to over 20 seconds, with both endpoints eventually
returning 503 (the DB pool-exhaustion handler firing for reasons that
have nothing to do with the DB pool itself).

**Fix:** `app/core/notification_executor.py` gives notification
dispatch its own small, bounded `ThreadPoolExecutor`, completely
separate from AnyIO's worker limiter. `NOTIFICATION_DISPATCH_WORKERS`
(default 8) caps how many dispatch batches can run at once; further
submissions queue on this executor's own internal queue instead of
stealing capacity from anything else. A burst of slow notification
sends can now only ever compete with *other* notification dispatches
for a thread, never with the pool every other endpoint depends on.
This module has zero dependency on FastAPI/Starlette/AnyIO — it's a
plain `concurrent.futures.ThreadPoolExecutor`, submitted to directly
from the sync route handler instead of going through `BackgroundTasks`.

Session hygiene for the dispatch itself (not holding a DB connection
across the actual email/SMS send) is covered in
[database-sessions-and-connection-pooling.md](./database-sessions-and-connection-pooling.md).

## Preventing duplicate resolution notifications

`PATCH /alerts/{id}/resolve` used to gate its resolution email/SMS/bell
notification dispatch on `notify_on_resolve` alone — not on whether
the alert actually transitioned to resolved *in that call*. A retried
or duplicated request (a dropped response, a double-click, a client
that times out and resubmits) for an **already-resolved** alert
re-sent the resolution notification every single time.

**Fix:** `alert_service.resolve_alert()` now returns `(alert,
just_resolved)`. `just_resolved` is `True` only on the call that
actually flips `is_resolved` from `False` to `True`; a repeat call for
an already-resolved alert returns the same alert with
`just_resolved=False` and performs no further writes or broadcasts.
The route (`app/api/v1/alerts.py`) only submits the notification
dispatch when `just_resolved and payload.notify_on_resolve` — gating
on `notify_on_resolve` alone would make the endpoint non-idempotent;
gating on `just_resolved` too makes a retried PATCH a safe no-op for
notifications while still returning the correct (already-resolved)
alert.

## Retrying transient send failures

`send_alert_emails`/`send_alert_sms` used to have zero retry for a
transient failure — a dropped SMTP connection, a connect timeout, a
"try again later" response while the provider is temporarily
overloaded — was indistinguishable from a permanent failure (a bad
address, an auth error): one attempt, then a permanent `"failed: ..."`
log entry, even though the very next second might have succeeded. That
was real notification loss for exactly the case that's supposed to be
recoverable.

**Fix:** both `email.py` and `sms.py` now classify failures as
transient or permanent before deciding whether to retry:

- **Email** (`_is_transient_smtp_error()`): connection-level problems
  and 4xx SMTP codes are treated as worth retrying (the provider is
  saying "try again"); a 5xx code, a rejected recipient/sender, or an
  auth failure are permanent — retrying those would just burn the
  whole backoff window before failing anyway.
- **SMS** (`_is_transient_sms_error()`): a network-level failure to
  reach Twilio at all (`urllib.error.URLError` — connection refused,
  DNS failure, timeout) or a 5xx/429 HTTP response is transient; any
  other 4xx (bad auth, invalid number, malformed request) is
  permanent.

Only the transient bucket is retried, with a small bounded number of
attempts and exponential backoff
(`NOTIFICATION_SEND_MAX_ATTEMPTS`/`NOTIFICATION_SEND_RETRY_BACKOFF_*`
in `app/core/config.py`); each retry reconnects from scratch, since a
dropped/broken connection can't just resume. This never sends the same
recipient's email/SMS twice for one call: each recipient gets **at
most one** outcome (`"sent"` or `"failed: ..."`), retried in place
before moving on — `send_alert_emails`/`send_alert_sms` still return
exactly one result per input recipient.

## Surviving a process restart: the durable dispatch queue

Everything above still lived entirely inside
`notification_executor.py`'s in-memory `ThreadPoolExecutor`: a job
handed to it existed only in that pool's internal queue or on one of
its worker threads. A process restart at the wrong moment (a deploy, a
crash, a hard kill) silently dropped it - no exception, no log, the
email/SMS just never went out - whether it was still waiting in the
queue or already partway through sending.

**Fix:** `app/models/notification_dispatch_job.py` +
`app/services/notification_dispatch_queue.py`. `app/api/v1/alerts.py`
now calls `notification_dispatch_queue.enqueue_and_submit(...)`
instead of `notification_executor.submit(...)` directly. That writes
and commits a `NotificationDispatchJob` row (`status=QUEUED`) *before*
the job ever reaches the in-memory pool. The pool callback
(`run_job`) marks the row `IN_PROGRESS` - incrementing `attempts` -
before doing any real work, then `DONE` or `FAILED` once the
underlying dispatch call returns or raises.

On startup, `app/main.py`'s lifespan calls
`notification_dispatch_queue.recover_pending_jobs()` before the app
serves traffic. Any row still `QUEUED` or `IN_PROGRESS` was, by
definition, owned by a process that's no longer running - it gets
resubmitted, unless its `attempts` already hit
`NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS` (default 5), in which case
it's marked permanently `FAILED` instead of retried forever. That
`attempts` counter, persisted to the row on every attempt before the
work runs, is what makes retry state survive a crash - it's how
recovery tells "just queued, try it" apart from "we've already tried
this repeatedly and it keeps dying".

Recovery re-runs the whole dispatch from scratch rather than resuming
mid-recipient-list, but each `NotificationLog` row now records the
`job_id` it was sent for, and `alert_service._dispatch` checks that
before ever calling out to the email/SMS provider: a recipient who
already has a SENT log row for THIS job/channel is skipped, so a job
resumed after a crash cannot send that recipient a duplicate
email/SMS. Recipients never reached, or that failed, are not skipped -
retries are unaffected. This is orthogonal to the transient-send-
failure retry described above: that retries one recipient's send
within a single already-running job; this is about the job itself
surviving the process it's running in going away.

New deployment: `python -m app.database.migrate_notification_dispatch_jobs`
creates the `notification_dispatch_jobs` table on an existing database
(a fresh `init_db.py` run picks it up automatically), and
`python -m app.database.migrate_notification_log_job_id` adds the
`notification_logs.job_id` column used by the idempotency check above.

## Tests

`tests/test_notification_dedup.py` covers both fixes in
isolation (no live Postgres needed — pure logic/mocked transport):

1. Duplicate-notification prevention: a repeated `PATCH
   /alerts/{id}/resolve` for an already-resolved alert dispatches no
   further notifications, and `resolve_alert` correctly reports
   `just_resolved=False` on the repeat.
2. Transient-failure retry: a simulated dropped-connection/5xx/429
   failure is retried and eventually succeeds within the configured
   attempt budget; a simulated permanent failure (bad auth, invalid
   recipient) fails on the first attempt with no wasted retries; each
   recipient still gets exactly one final outcome.

`tests/test_notification_dispatch_recovery.py` covers the durable
queue against a real (in-memory SQLite) database: a job is persisted
before being handed to the executor; a job stuck `IN_PROGRESS`/`QUEUED`
by a simulated crash is found and resubmitted by
`recover_pending_jobs()` and, on its resumed run, completes with
`attempts` reflecting both the lost attempt and the successful one; a
job that has exhausted its attempt budget is failed permanently
instead of resubmitted; and a job that finished normally is never
re-run if resubmitted again.

`scripts/verify_notification_dispatch_isolation.py` proves the thread
pool isolation directly: it drives a burst of slow simulated
dispatches through the real `notification_executor` and confirms
concurrent unrelated requests are not starved of AnyIO worker threads.
