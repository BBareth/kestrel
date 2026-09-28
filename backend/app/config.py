"""Process configuration (infrastructure + secrets) loaded from environment variables.

Everything a user tunes at runtime (strategy parameters, risk limits, AI policy,
notification preferences) lives in the database and is edited through the UI.
This module only holds what must exist before the database can be reached, and
the secrets, which never leave the process environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # --- core ---------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://kestrel:kestrel@db:5432/kestrel",
        description="SQLAlchemy async URL",
    )
    auth_secret: SecretStr = Field(default=SecretStr(""), description="Signs CSRF tokens and encrypts TOTP secrets")
    log_level: str = "INFO"
    environment: Literal["production", "development", "test"] = "production"

    # --- market ---------------------------------------------------------------
    symbol: str = "BTCUSDT"

    # --- Binance --------------------------------------------------------------
    binance_api_key: SecretStr = Field(default=SecretStr(""))
    binance_api_secret: SecretStr = Field(default=SecretStr(""))
    # Execution venue for LIVE mode. Market data for the strategy always comes from
    # the production public endpoints (no key needed) so paper trading is realistic.
    binance_testnet: bool = False
    binance_rest_url: str = "https://fapi.binance.com"
    binance_testnet_rest_url: str = "https://demo-fapi.binance.com"
    binance_spot_url: str = "https://api.binance.com"  # only for API-key permission checks
    binance_ws_market_url: str = "wss://fstream.binance.com/market"
    binance_ws_public_url: str = "wss://fstream.binance.com/public"
    binance_recv_window_ms: int = 5000

    # --- OpenAI ---------------------------------------------------------------
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_base_url: str | None = None

    # --- Web push -------------------------------------------------------------
    vapid_public_key: str = ""
    vapid_private_key: SecretStr = Field(default=SecretStr(""))
    vapid_subject: str = "mailto:admin@localhost"

    # --- web / auth -----------------------------------------------------------
    # Comma-separated list of origins allowed for state-changing requests,
    # e.g. "https://192.168.1.178:8443,https://localhost:8443".
    allowed_origins: str = ""
    session_ttl_hours: int = 24 * 14
    session_idle_hours: int = 24 * 3
    reauth_window_seconds: int = 300
    cookie_secure: bool = True

    # --- paper ----------------------------------------------------------------
    paper_starting_balance: float = 10_000.0

    # --- engine ---------------------------------------------------------------
    # Container-private tmpfs (read-only root FS); holds only a timestamp for the healthcheck.
    engine_heartbeat_file: str = "/tmp/kestrel-engine.heartbeat"  # nosec B108

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # Convenience ---------------------------------------------------------------
    @property
    def binance_configured(self) -> bool:
        return bool(self.binance_api_key.get_secret_value() and self.binance_api_secret.get_secret_value())

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key.get_secret_value())

    @property
    def vapid_configured(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key.get_secret_value())

    @property
    def execution_rest_url(self) -> str:
        return self.binance_testnet_rest_url if self.binance_testnet else self.binance_rest_url

    @property
    def origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]

    def secret_values(self) -> list[str]:
        """All secret strings, used by the log redactor. Never log the return value."""
        vals = [
            self.auth_secret.get_secret_value(),
            self.binance_api_key.get_secret_value(),
            self.binance_api_secret.get_secret_value(),
            self.openai_api_key.get_secret_value(),
            self.vapid_private_key.get_secret_value(),
        ]
        # DB password embedded in the URL
        if "@" in self.database_url and ":" in self.database_url.split("@")[0]:
            pw = self.database_url.split("@")[0].rsplit(":", 1)[-1]
            vals.append(pw)
        return [v for v in vals if v and len(v) >= 6]

    def validate_for_production(self) -> list[str]:
        problems = []
        if len(self.auth_secret.get_secret_value()) < 32:
            problems.append("AUTH_SECRET must be at least 32 characters")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
