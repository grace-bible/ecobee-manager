from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]  # "a,b,c" in env → ["a", "b", "c"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"  # "production" disables the dev auth bypass
    database_url: str = "sqlite:///./ecobee-manager.db"
    app_secret_key: str = ""  # Fernet key; encrypts stored ecobee tokens
    default_timezone: str = "America/Chicago"

    # --- ecobee (dedicated org-owned account) ---
    ecobee_backend: str = "cloud"  # "cloud" or "fake" (local dev, no thermostats touched)
    ecobee_username: str = ""
    ecobee_password: str = ""
    ecobee_totp_secret: str = ""  # base32 secret from the account's authenticator setup
    ecobee_poll_seconds: int = 60

    # --- Google Workspace ---
    google_service_account_file: str = ""  # path to SA JSON (domain-wide delegation)
    google_service_account_json: str = ""  # …or the JSON itself (Komodo env); wins if both set
    google_impersonate: str = ""  # Workspace user the SA acts as
    google_customer_id: str = "my_customer"
    calendar_sync_seconds: int = 120
    calendar_window_days: int = 14

    # --- Cloudflare Access ---
    cf_access_team_domain: str = ""  # e.g. "gracebible.cloudflareaccess.com"
    cf_access_aud: str = ""  # Application Audience (AUD) tag
    allowed_email_domains: CsvList = ["grace-bible.org"]
    admin_emails: CsvList = []
    dev_auth_email: str = ""  # local development only

    # --- Behavior ---
    run_jobs: bool = True  # background poll/sync/reconcile (off in tests)
    reconcile_seconds: int = 60
    hold_horizon_minutes: int = 240  # every hold the app sets expires within this
    min_deadband_f: float = 4.0
    dry_run: bool = True  # log writes instead of sending them until you flip this

    # --- Alerts (docs/SPEC.md §9) ---
    google_chat_webhook: str = ""
    alert_offline_minutes: int = 15
    alert_setpoint_minutes: int = 60  # occupied this long and still off target
    alert_setpoint_tolerance_f: float = 3.0
    alert_calendar_minutes: int = 15

    @field_validator("allowed_email_domains", "admin_emails", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [x.strip().lower() for x in v.split(",") if x.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
