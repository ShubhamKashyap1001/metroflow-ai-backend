from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_NAME: str = "MetroFlow AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    SQL_ECHO: bool = False

    DATABASE_URL: str

    DB_POOL_SIZE: int = 3
    DB_MAX_OVERFLOW: int = 2
    DB_POOL_TIMEOUT: int = 15
    DB_POOL_RECYCLE: int = 1800

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

    MAX_REQUEST_BODY_BYTES: int = 1_048_576

    SENSOR_INGEST_API_KEY: str | None = None
    METRO_TRAIN_INGEST_API_KEY: str | None = None
    SENSOR_FEED_EVENT_RETENTION_DAYS: int = 30
    PUBLIC_ADDRESS_WEBHOOK_URL: str | None = None
    PUBLIC_ADDRESS_WEBHOOK_TOKEN: str | None = None
    MOBILE_PUSH_WEBHOOK_URL: str | None = None
    MOBILE_PUSH_WEBHOOK_TOKEN: str | None = None

    AI_MODELS_DIR: str = "app/ai_engine/saved_models"
    AI_DATASETS_DIR: str = "app/ai_engine/datasets"

    AI_EAGER_WARMUP: bool = False

    AI_MODEL_LIGHT_MODE: bool = True

    BUSINESS_TIMEZONE: str = "Asia/Kolkata"

    REDIS_URL: str | None = "redis://localhost:6379/0"
    CACHE_ENABLED: bool = True
                                                                       
    CACHE_TTL_SECONDS: int = 5

    ENABLE_SIMULATOR: bool = True

    SIMULATOR_INTERVAL_SECONDS: int = 60
    SIMULATOR_PASSENGER_POOL_SIZE: int = 40
    SIMULATOR_MAX_CHECKINS_PER_TICK: int = 6

    CROWD_HISTORY_INTERVAL_SECONDS: int = 60

    CROWD_LOG_ROLLUP_AFTER_DAYS: int = 2

    CROWD_LOG_RETENTION_DAYS: int = 30

    CROWD_LOG_HOURLY_RETENTION_DAYS: int = 400

    ENABLE_CROWD_RETENTION_JOB: bool = True
    CROWD_RETENTION_INTERVAL_SECONDS: int = 3600

    ENABLE_PEAK_HOUR_CACHE: bool = True
    PEAK_HOUR_LOOKBACK_DAYS: int = 30
    PEAK_HOUR_TOP_N: int = 10

    PEAK_HOUR_CACHE_TTL_SECONDS: int = 5400

    RETENTION_BATCH_SIZE: int = 5000

    CROWD_ROLLUP_BATCH_SIZE: int = 1000

    ENABLE_TRAIN_TRACKING: bool = True
    TRAIN_TRACK_INTERVAL_SECONDS: int = 60

    GEMINI_API_KEY: str | None = None
    CHATBOT_MODEL: str = "gemini-3.5-flash"
    CHATBOT_MAX_TOKENS: int = 1536

    CHATBOT_MAX_HISTORY_MESSAGES: int = 20

    NOTIFICATION_BIN_RETENTION_HOURS: int = 72
    ENABLE_NOTIFICATION_BIN_RETENTION_JOB: bool = True

    NOTIFICATION_BIN_RETENTION_INTERVAL_SECONDS: int = 300

    ENABLE_REPORT_SCHEDULER: bool = True
    REPORT_SCHEDULER_INTERVAL_SECONDS: int = 60
    REPORT_SCHEDULER_BATCH_SIZE: int = 20
    REPORT_SCHEDULE_MAX_ATTEMPTS: int = 3
    REPORT_SCHEDULE_RETRY_SECONDS: int = 300
    REPORT_SCHEDULE_STUCK_SECONDS: int = 1800
    CROWD_DATA_STALE_AFTER_SECONDS: int = 300

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

    NOTIFICATION_DISPATCH_WORKERS: int = 2

    NOTIFICATION_SEND_MAX_ATTEMPTS: int = 3
    NOTIFICATION_SEND_RETRY_BACKOFF_SECONDS: float = 0.5
    NOTIFICATION_SEND_RETRY_BACKOFF_CAP_SECONDS: float = 4.0

    NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS: int = 5

    WS_MAX_CONNECTIONS: int = 50

    WS_MAX_CONNECTIONS_PER_USER: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    @property
    def cors_origins_list(self) -> list[str]:
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
        return self.DEBUG and self.AUTH_DISABLED

settings = Settings()
