import os
import tempfile

# Configure before any app module reads settings.
_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update({
    # Explicit, because a developer's .env (read by pydantic-settings) may say production.
    "ENVIRONMENT": "test",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "",
    "GOOGLE_SERVICE_ACCOUNT_FILE": "",
    "GOOGLE_CHAT_WEBHOOK": "",
    "DATABASE_URL": f"sqlite:///{_db}",
    "ECOBEE_BACKEND": "fake",
    "DRY_RUN": "false",
    "RUN_JOBS": "false",
    "DEV_AUTH_EMAIL": "admin@example.org",
    "ADMIN_EMAILS": "admin@example.org",
    "ALLOWED_EMAIL_DOMAINS": "example.org",
    "APP_SECRET_KEY": "8m2WkS3vJ1nqv2kBv9jXf4dP0yq6m5pYJ3r9VQ7b0cE=",
})

import pytest


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from app.db import init_db

    init_db()  # run the real migrations once, so tests exercise them too
