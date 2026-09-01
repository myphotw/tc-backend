"""Deterministic runtime settings for the ordinary pytest suite.

This module is imported by pytest before test-module collection.  Keep all
values deliberately non-operational: ordinary tests use SQLite, fakes, or
mocks and must never inherit a developer's local runtime configuration.
"""

from __future__ import annotations

import base64
import os


_TEST_FERNET_MATERIAL = b"tc-backend-pytest-fernet-key-32!"
TEST_MASTER_KEY = base64.urlsafe_b64encode(_TEST_FERNET_MATERIAL).decode("ascii")


TEST_RUNTIME_ENV = {
    "VERSION": "test",
    # A local, privileged port is intentionally used as a fail-closed target.
    # SQLAlchemy engine construction is lazy; any accidental connection is
    # confined to localhost and should be refused rather than reach a real DB.
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "1",
    "POSTGRES_DB": "tc_backend_pytest",
    "POSTGRES_USER": "tc_backend_pytest",
    "POSTGRES_PASSWORD": "tc_backend_pytest_password",
    "MASTER_KEY": TEST_MASTER_KEY,
    "TC_BACKEND_AUTH_TOKEN": "",
    "PHOTO_PLATFORM_ROOT": "./watcher_data/pytest-runtime",
    "INCOMING_DIR": "./watcher_data/pytest-runtime/incoming",
    "ORIGINAL_DIR": "./watcher_data/pytest-runtime/original",
    "PREVIEW_DIR": "./watcher_data/pytest-runtime/preview",
    "THUMB_DIR": "./watcher_data/pytest-runtime/thumb",
    "EXPORT_DIR": "./watcher_data/pytest-runtime/export",
    "CACHE_DIR": "./watcher_data/pytest-runtime/cache",
    "TEMP_DIR": "./watcher_data/pytest-runtime/temp",
    "GOOGLE_API_KEY": "",
    "GOOGLE_MAP_API_KEY": "",
    "GOOGLE_VISION_CREDENTIAL": "",
    "WEATHER_API_KEY": "",
    "ASTROMETRY_API_KEY": "",
    "API_CLIENT_TIMEOUT": "30",
    "API_CLIENT_RETRY_COUNT": "3",
    "VISION_MONTHLY_LIMIT": "900",
    "GEOCODING_MONTHLY_LIMIT": "100000",
    "WEATHER_MONTHLY_LIMIT": "100000",
    "PLATESOLVE_MONTHLY_LIMIT": "100000",
}


# Do not use setdefault: environment variables have higher priority than the
# local .env file in pydantic-settings, so explicit replacement prevents
# ordinary pytest from inheriting developer or operational configuration.
os.environ.update(TEST_RUNTIME_ENV)
