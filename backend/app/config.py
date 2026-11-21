from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    app_name: str = "Disaster Safety API"
    environment: str = "development"
    db_host: str = Field(default="127.0.0.1", min_length=1)
    db_port: int = Field(default=5432, gt=0, le=65535)
    db_name: str = Field(default="disaster_safety", min_length=1)
    db_user: str = Field(default="postgres", min_length=1)
    db_password: SecretStr = SecretStr("replace_with_your_local_password")
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_echo: bool = False
    osrm_base_url: str = "http://router.project-osrm.org"
    osrm_timeout_seconds: float = 5.0
    alert_monitoring_enabled: bool = False
    alert_monitoring_interval_seconds: int = 300
    cors_allowed_origins: str = "*"
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    log_level: str = "INFO"
    server_host: str = "0.0.0.0"
    server_port: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_origins_list(self) -> list[str]:
        if not self.cors_allowed_origins or self.cors_allowed_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment.lower() == "production":
            pw = self.db_password.get_secret_value()
            if pw in ("replace_with_your_local_password", "postgres", "password", "admin", "123456"):
                raise ValueError("Insecure or default database password detected for production environment.")
            if self.db_echo:
                raise ValueError("Database SQL query echo (db_echo) must be False in production.")
        return self


settings = Settings()
