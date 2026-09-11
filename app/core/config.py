from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_NAME: str = "MetroFlow AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    SQL_ECHO: bool = False

    DATABASE_URL: str

    # Lowered from 15/25 -> 5/5: on a 512MB free-tier instance (e.g.
    # Render's free plan) a 40-connection ceiling is far more than one
    # worker process needs and each connection carries its own client-
    # side buffers, so this trims idle memory overhead. Raise these
    # back up via env vars once you're on a bigger instance / see pool
    # -exhaustion 503s under real load.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 10
    DB_POOL_RECYCLE: int = 300

    DB_STATEMENT_TIMEOUT_MS: int = 30_000


    WEB_CONCURRENCY: int = 1

    DB_CONNECTION_RESERVE: int = 10

    SCHEDULE_CACHE_TTL_SECONDS: int = 30

    AUTH_USER_CACHE_TTL_SECONDS: int = 30

    JWKS_CACHE_TTL_SECONDS: int = 600

    SUPABASE_URL: str
    SUPABASE_KEY: str

    SUPABASE_JWT_SECRET: str | None = None

    SUPABASE_SERVICE_ROLE_KEY: str | None = None

    AUTH_DISABLED: bool = False

    CORS_ORIGINS: str = "http://localhost:3000"

    AI_MODELS_DIR: str = "app/ai_engine/saved_models"
    AI_DATASETS_DIR: str = "app/ai_engine/datasets"

    # --- Free-tier / low-memory deployment knobs ---------------------
    # AI_EAGER_WARMUP: when True, all 3 model bundles (crowd/delay/
    # frequency) are loaded during startup (app/ai_engine/warmup.py) so
    # the first real request never pays the joblib.load() cost. On a
    # 512MB instance, loading all 3 up front - on top of importing
    # numpy/pandas/scipy/scikit-learn/xgboost - can push the process
    # over the memory limit before it even starts accepting traffic.
    # Defaulting this to False makes each predictor load lazily on its
    # own first use instead (still cached after that via lru_cache),
    # spreading the same total memory cost out over the first few
    # requests/simulator ticks rather than paying it all at once during
    # boot. Set to True once you're on an instance with more headroom.
    AI_EAGER_WARMUP: bool = False

    # AI_MODEL_LIGHT_MODE: each saved model bundle actually contains
    # TWO fully-trained models (random_forest and xgboost) so the
    # dashboard can show both predictions side by side. When True,
    # each bundle is pruned down to just its winning (lowest-MAE)
    # model right after loading, dropping the other from `models`.
    #
    # Measured cost of keeping both candidates loaded (single worker,
    # all 3 bundles): only ~17.6MB total, of which pruning to one
    # candidate each saves ~3.7MB - xgboost's own library import
    # (~150MB) is paid regardless, since frequency_model.pkl's winner
    # is xgboost either way. That's not worth losing the side-by-side
    # comparison feature over, so this defaults to False. Only flip it
    # to True if you're chasing every last MB on an instance that's
    # still right at the OOM line even after WEB_CONCURRENCY=1.
    AI_MODEL_LIGHT_MODE: bool = False

    # BUGFIX (naive datetime / timezone handling): every train_schedule
    # arrival/departure time, "peak hour" window, and day-of-week
    # calculation in this app is meant to reflect the metro network's
    # own local wall-clock time - not whatever timezone the server
    # process happens to be running in, and not UTC either. All 12
    # seeded cities (see app/utils/geo.py) sit in India, a single
    # timezone with no DST, so one setting covers the whole network.
    # Persisted timestamps stay UTC (DateTime(timezone=True) columns,
    # `datetime.now(timezone.utc)` at write time) - only the
    # date/peak-hour/schedule *calculations* derived from "now" are
    # resolved against this timezone. See app/utils/timezone.py.
    BUSINESS_TIMEZONE: str = "Asia/Kolkata"

    REDIS_URL: str | None = "redis://localhost:6379/0"
    CACHE_ENABLED: bool = True
                                                                       
    CACHE_TTL_SECONDS: int = 5

    ENABLE_SIMULATOR: bool = False
    # Each tick advances every station one row forward through its own
    # CSV history (app/simulator/csv_replay_simulator.py) - the dataset
    # has 18 hourly rows/station/day (5 AM-10 PM), so at the old 10s
    # default a whole simulated "day" (and the net-occupancy reset at
    # its boundary) cycled in just 18 x 10s = 3 minutes, making the
    # Live Passengers KPI swing from empty to peak and back on a
    # 3-minute clock - real, correct behaviour, just uncomfortably
    # fast to watch. 60s stretches that same cycle to 18 minutes,

    # SIMULATOR_INTERVAL_SECONDS: int = 20
    SIMULATOR_INTERVAL_SECONDS: int = 10
    SIMULATOR_PASSENGER_POOL_SIZE: int = 40
    SIMULATOR_MAX_CHECKINS_PER_TICK: int = 6

    # --- Phase 2: crowd DB load / retention -------------------------
    # Live state (station_crowd_state) is upserted EVERY simulator tick
    # (SIMULATOR_INTERVAL_SECONDS) so the dashboard/heatmap/WebSocket
    # push stay real-time. Historical rows (crowd_logs) are sampled at
    # a coarser, independent interval - the dashboard doesn't need
    # every tick preserved forever, only enough resolution for
    # trend analytics (inflow/outflow, 24h avg/peak).
    CROWD_HISTORY_INTERVAL_SECONDS: int = 60

    # Raw crowd_logs rows older than this are rolled up into
    # crowd_logs_hourly (avg/max/min/sample_count per station-hour)
    # and then deleted, keeping the high-resolution table small while
    # preserving long-range analytics in the rollup table.
    CROWD_LOG_ROLLUP_AFTER_DAYS: int = 2

    # Safety-net hard delete for raw crowd_logs, in case the rollup job
    # is ever disabled/behind - independent upper bound on raw retention.
    CROWD_LOG_RETENTION_DAYS: int = 30

    # How long crowd_logs_hourly rollups themselves are kept before
    # being hard-deleted (long-range trend history).
    CROWD_LOG_HOURLY_RETENTION_DAYS: int = 400

    ENABLE_CROWD_RETENTION_JOB: bool = True
    CROWD_RETENTION_INTERVAL_SECONDS: int = 3600

    # Retention/rollup jobs (crowd_logs safety-net + rollup deletes,
    # crowd_logs_hourly deletes, notification bin deletes) never issue
    # one unbounded DELETE for the whole backlog - they delete this
    # many rows per batch/commit instead, so memory use stays bounded
    # and no single transaction holds locks for the entire backlog's
    # worth of rows. See app/utils/db_batch.py::batched_delete.
    RETENTION_BATCH_SIZE: int = 5000

    # Rollup aggregation (raw crowd_logs -> crowd_logs_hourly) is
    # paginated at this many (station, hour) buckets per page/commit,
    # for the same reason - a job that's fallen behind can otherwise
    # aggregate a very large backlog's worth of buckets into memory
    # (and one transaction) in a single pass.
    CROWD_ROLLUP_BATCH_SIZE: int = 1000

    ENABLE_TRAIN_TRACKING: bool = False
    TRAIN_TRACK_INTERVAL_SECONDS: int = 10

    # --- AI Chatbot (floating assistant widget) ---------------------
    # Server-side only - never exposed to the frontend. If unset, the
    # /chatbot endpoint returns 503 rather than failing at import
    # time, so the rest of the app still boots fine without it.
    GEMINI_API_KEY: str | None = None
    CHATBOT_MODEL: str = "gemini-3.5-flash"
    CHATBOT_MAX_TOKENS: int = 1536
    # Hard cap on turns accepted from the client per request, so one
    # request can't be used to smuggle an unbounded prompt in.
    CHATBOT_MAX_HISTORY_MESSAGES: int = 20

    # --- Notification Bin (Phase 12) ---------------------------------
    # A "mark all as read" sweep stamps binned_at on every row it
    # touches (see notification_service.mark_all_read), which pulls it
    # out of the normal Inbox feed and into the Bin tab. This job then
    # hard-deletes anything that's been sitting in the Bin longer than
    # NOTIFICATION_BIN_RETENTION_HOURS - see
    # app/simulator/notification_bin_retention.py.
    NOTIFICATION_BIN_RETENTION_HOURS: int = 72
    ENABLE_NOTIFICATION_BIN_RETENTION_JOB: bool = True
    # Checked far more often than the retention window itself (unlike
    # the day-granularity crowd-log retention job) so a binned row
    # doesn't linger for up to a few hours past its 72h mark before
    # it's swept.
    NOTIFICATION_BIN_RETENTION_INTERVAL_SECONDS: int = 300

    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str = "alerts@metroflow.app"
    SMTP_FROM_NAME: str = "MetroFlow Alerts"
    SMTP_USE_TLS: bool = True

    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_FROM_NUMBER: str | None = None

    # Phase 8: alert email/SMS dispatch runs on its OWN small thread
    # pool instead of FastAPI BackgroundTasks' shared AnyIO worker
    # pool - see app/core/notification_executor.py. This bounds how
    # many notification batches can be sending at once; it is
    # deliberately NOT the same knob as DB_POOL_SIZE/DB_MAX_OVERFLOW
    # or AnyIO's thread limiter. See docs/notification-delivery.md.
    NOTIFICATION_DISPATCH_WORKERS: int = 8

    # Phase 10: per-recipient send retry for TRANSIENT provider errors
    # only (a dropped SMTP connection, a connection-refused/timeout to
    # Twilio, a 4xx/5xx "try again" response) - see app/core/email.py /
    # app/core/sms.py's `_is_transient_*` classifiers and
    # docs/notification-delivery.md. A PERMANENT error (bad address, invalid
    # phone number, auth failure) is never retried - retrying those
    # would just waste the whole backoff window before failing anyway.
    # NOTIFICATION_SEND_MAX_ATTEMPTS counts the first attempt itself,
    # so the default of 3 means "1 initial try + up to 2 retries".
    NOTIFICATION_SEND_MAX_ATTEMPTS: int = 3
    NOTIFICATION_SEND_RETRY_BACKOFF_SECONDS: float = 0.5
    NOTIFICATION_SEND_RETRY_BACKOFF_CAP_SECONDS: float = 4.0

    # Phase 11: durable dispatch queue (app/models/notification_dispatch_job.py,
    # app/services/notification_dispatch_queue.py) - a row is written and
    # committed to Postgres BEFORE the job is handed to
    # notification_executor's in-memory ThreadPoolExecutor, so a process
    # restart (deploy, crash, hard kill) never silently drops a
    # notification that was queued or mid-flight. This caps how many
    # times a single job is re-attempted across restarts, so a job that
    # deterministically crashes the process it runs on can't loop
    # forever - it's marked permanently failed instead. This is
    # independent of NOTIFICATION_SEND_MAX_ATTEMPTS, which retries a
    # single transient per-recipient send failure within one already-
    # running job.
    NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Explicit allowlist ONLY. CORS_ORIGINS is a comma-separated
        list of exact origins (scheme+host+port), e.g.
        "https://app.example.com,https://admin.example.com".

        A literal "*" is never honored, on purpose: this API always
        sends allow_credentials=True (see the CORSMiddleware setup in
        app/main.py), and per the CORS spec a wildcard origin can't
        legally be combined with credentials - browsers reject the
        literal string "*" there, but a naive implementation that
        instead reflects back whatever Origin the request actually
        sent (e.g. via CORSMiddleware's allow_origin_regex=".*") gets
        around that protection and lets ANY origin make authenticated,
        credentialed requests. So "*" is dropped here rather than
        expanded to mean "everything" - a misconfigured CORS_ORIGINS
        fails safe (no origins allowed) instead of failing open (every
        origin allowed).
        """
        origins = [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]
        if "*" in origins:
            import logging

            logging.getLogger(__name__).warning(
                "CORS_ORIGINS contains '*', which is ignored - combined with "
                "allow_credentials=True that would let any site make "
                "authenticated requests. List explicit origins instead, e.g. "
                "CORS_ORIGINS=https://app.example.com,https://admin.example.com"
            )
            origins = [o for o in origins if o != "*"]
        return origins

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.SUPABASE_URL}/auth/v1/.well-known/jwks.json"

    @property
    def dev_auth_bypass_enabled(self) -> bool:
        """Whether the mock-auth dev bypass in app/core/security.py is
        actually honored. Requires BOTH AUTH_DISABLED=True AND
        DEBUG=True - not just AUTH_DISABLED alone - so a stray/
        leftover AUTH_DISABLED=true in a production .env can't skip
        real Supabase JWT verification there, since DEBUG defaults to
        False (production) unless explicitly turned on."""
        return self.DEBUG and self.AUTH_DISABLED

settings = Settings()