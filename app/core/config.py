from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_NAME: str = "MetroFlow AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True

    SQL_ECHO: bool = False

    DATABASE_URL: str

    DB_POOL_SIZE: int = 15
    DB_MAX_OVERFLOW: int = 25
    DB_POOL_TIMEOUT: int = 10
    DB_POOL_RECYCLE: int = 300

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

    REDIS_URL: str | None = "redis://localhost:6379/0"
    CACHE_ENABLED: bool = True
                                                                       
    CACHE_TTL_SECONDS: int = 5

    ENABLE_SIMULATOR: bool = True
    SIMULATOR_INTERVAL_SECONDS: int = 10
    SIMULATOR_PASSENGER_POOL_SIZE: int = 40
    SIMULATOR_MAX_CHECKINS_PER_TICK: int = 6

    ENABLE_TRAIN_TRACKING: bool = True
    TRAIN_TRACK_INTERVAL_SECONDS: int = 10

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

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    @property
    def cors_origins_list(self) -> list[str]:
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.SUPABASE_URL}/auth/v1/.well-known/jwks.json"

settings = Settings()