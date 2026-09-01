import logging

import psycopg2
from sqlalchemy import create_engine, event

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

# Phase 6: Postgres-side statement timeout, passed via libpq's `options`
# connect arg (works with psycopg2, no extra dependency). Bounds any
# single query to DB_STATEMENT_TIMEOUT_MS so a stuck/slow statement gets
# cancelled by the SERVER and raises a normal catchable error, instead
# of holding its connection (and therefore one pool slot) forever -
# pool_timeout/pool_recycle/pool_pre_ping below all protect against a
# BROKEN connection, none of them protect against a connection that's
# perfectly healthy but wedged mid-query. See docs/database-sessions-and-connection-pooling.md.
_connect_args = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "connect_timeout": 10,
}
if settings.DATABASE_URL.startswith("postgresql"):
    _connect_args["options"] = f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS}"

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    future=True,
    # Cheaply pings a pooled connection before handing it out, so a
    # connection the DB (or an intermediary proxy) silently dropped
    # while idle is replaced instead of failing the first real query
    # that uses it.
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # How long a request will wait for a free connection before
    # SQLAlchemy raises sqlalchemy.exc.TimeoutError - caught by
    # app/main.py's db_pool_exhausted_handler and turned into a fast,
    # clean 503 instead of a hang. This value alone is NOT "the" pool
    # sizing knob - see the audit note below.
    pool_timeout=settings.DB_POOL_TIMEOUT,
    # Recycle pooled connections older than this so a managed
    # Postgres's own idle/max-lifetime connection killer (common on
    # hosted providers) can never be the thing that surfaces as a
    # broken connection to application code.
    pool_recycle=settings.DB_POOL_RECYCLE,
    connect_args=_connect_args,
)

@event.listens_for(engine, "handle_error")
def _record_db_failure(context):
    """PostgreSQL failure metric (see app/core/metrics.py). SQLAlchemy
    fires this ConnectionEvents.handle_error event for any error
    raised by the DBAPI (psycopg2) during a connection or cursor
    operation - a dropped/refused connection, an auth failure, a
    statement-timeout cancellation, a constraint violation, etc. This
    is the single place that covers all of them, without threading a
    metrics call through every individual `except Exception` in the
    codebase that happens to be catching a DB error.

    Deliberately does NOT cover a connection-pool checkout timeout
    (sqlalchemy.exc.TimeoutError, DB_POOL_TIMEOUT exceeded): that's
    SQLAlchemy's own pool giving up before it ever attempts a DBAPI
    connection, so this event never fires for it - see
    app/main.py's db_pool_exhausted_handler, which records that case
    separately under reason="pool_exhausted".

    Only the exception's CLASS NAME is used as the metric label -
    never str()/repr() of the exception itself, which for a connection
    failure can contain the DSN (host, port, database name, sometimes
    the username). Returning None (the implicit result of not
    returning context.chained_exception) leaves SQLAlchemy's own error
    handling/propagation completely untouched - this listener only
    observes, it never suppresses or replaces the original error.
    """
    reason = type(context.original_exception).__name__
    try:
        metrics.record_db_failure(reason)
    except Exception:
        # A metrics-recording bug must never be able to swallow or
        # alter the real database error this listener was observing.
        logger.warning("[metrics] failed to record db failure metric for %s", reason)

# Phase 6 (unchanged): DB_POOL_SIZE + DB_MAX_OVERFLOW is the connection
# ceiling for ONE process. Under the Phase 4 leader-election
# architecture the API may run as N worker processes, each with its
# OWN independent engine/pool - so the real cluster-wide ceiling is
# this number times N (N = settings.WEB_CONCURRENCY), not this number
# alone.
_per_process_ceiling = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
_cluster_ceiling = _per_process_ceiling * settings.WEB_CONCURRENCY


def _fetch_postgres_max_connections() -> int | None:
    """Read the server's REAL `max_connections`, via a single ad-hoc,
    short-timeout connection made and closed immediately - deliberately
    NOT through `engine`/the pool above, so this startup check can
    never itself consume a pool slot or be affected by pool sizing.
    Returns None (never raises) if Postgres can't be reached right now
    - a dev box without Postgres running yet, or a container-startup
    race - so this check degrades to "unverified", not "app won't
    boot", when the problem is availability rather than a genuine
    sizing mistake.
    """
    if not settings.DATABASE_URL.startswith("postgresql"):
        return None
    conn = None
    try:
        conn = psycopg2.connect(settings.DATABASE_URL, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SHOW max_connections;")
            return int(cur.fetchone()[0])
    except Exception as exc:
        logger.warning(
            "[db] could not read Postgres max_connections to verify pool "
            "capacity (%s) - skipping the startup capacity check. This "
            "does not mean capacity is safe, only that it couldn't be "
            "checked right now.",
            exc,
        )
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def check_pool_capacity(
    pool_size: int,
    max_overflow: int,
    web_concurrency: int,
    reserve: int,
    max_connections: int,
) -> tuple[int, int]:
    """Pure arithmetic, no I/O - kept separate from
    `_verify_pool_capacity_or_raise` below so this check's actual logic
    can be unit-tested (tests/test_db_pool_capacity.py) without needing
    a real Postgres connection or psycopg2 in the test environment.

    Returns `(cluster_ceiling, safe_capacity)` if the configuration is
    safe. Raises `RuntimeError` (never anything more exotic - a plain,
    catchable, clearly-worded error) if `(pool_size + max_overflow) *
    web_concurrency` - the most connections every worker process could
    simultaneously hold open - would exceed `max_connections - reserve`.
    """
    cluster_ceiling = (pool_size + max_overflow) * web_concurrency
    safe_capacity = max_connections - reserve
    if cluster_ceiling > safe_capacity:
        raise RuntimeError(
            f"[db] refusing to start: DB_POOL_SIZE ({pool_size}) + "
            f"DB_MAX_OVERFLOW ({max_overflow}) x WEB_CONCURRENCY "
            f"({web_concurrency}) = {cluster_ceiling} possible concurrent "
            f"connections, which exceeds the safe capacity of "
            f"{safe_capacity} (Postgres max_connections={max_connections} "
            f"minus DB_CONNECTION_RESERVE={reserve} reserved for "
            f"migrations/admin/monitoring). All N worker processes sharing "
            f"one Postgres instance can exhaust its entire connection "
            f"budget - starving every other client, not just this app. "
            f"Fix this by LOWERING DB_POOL_SIZE, DB_MAX_OVERFLOW, or "
            f"WEB_CONCURRENCY (or raising Postgres's own max_connections, "
            f"if that's actually safe for this server's resources) - do "
            f"not paper over this by increasing the pool without checking "
            f"the server can sustain it."
        )
    return cluster_ceiling, safe_capacity


def _verify_pool_capacity_or_raise() -> None:
    """Phase 17E: turn the Phase 6 audit note above from an
    informational log line into an actual, enforced check. Per the
    brief - do NOT blindly raise DB_POOL_SIZE/DB_MAX_OVERFLOW to "fix"
    a capacity problem; verify the real ceiling against Postgres's
    real max_connections and refuse to boot into a configuration that
    would let concurrent worker processes collectively exhaust the
    server's own connection budget (which doesn't just 503 this app -
    it starves every other client of that Postgres instance too, and
    is far harder to diagnose after the fact than at startup).
    """
    max_connections = _fetch_postgres_max_connections()
    if max_connections is None:
        logger.info(
            "[db] cluster-wide connection ceiling = %d (pool_size=%d + "
            "max_overflow=%d) x WEB_CONCURRENCY=%d. Could not verify this "
            "against Postgres's real max_connections (see warning above) - "
            "make sure it comfortably exceeds this number, with headroom "
            "for migrations, admin scripts and other clients.",
            _cluster_ceiling, settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW,
            settings.WEB_CONCURRENCY,
        )
        return

    cluster_ceiling, safe_capacity = check_pool_capacity(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        web_concurrency=settings.WEB_CONCURRENCY,
        reserve=settings.DB_CONNECTION_RESERVE,
        max_connections=max_connections,
    )
    logger.info(
        "[db] verified: cluster-wide connection ceiling = %d "
        "(pool_size=%d + max_overflow=%d x WEB_CONCURRENCY=%d) fits within "
        "safe capacity of %d (Postgres max_connections=%d - "
        "DB_CONNECTION_RESERVE=%d).",
        cluster_ceiling, settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW,
        settings.WEB_CONCURRENCY, safe_capacity, max_connections,
        settings.DB_CONNECTION_RESERVE,
    )


_verify_pool_capacity_or_raise()
