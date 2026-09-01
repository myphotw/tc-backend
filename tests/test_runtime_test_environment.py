from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url

from app.common.config import settings
from app.common.database import DATABASE_URL


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RuntimeTestEnvironmentTests(unittest.TestCase):
    def test_required_runtime_settings_are_deterministic_and_test_only(self) -> None:
        self.assertEqual(settings.VERSION, "test")
        self.assertEqual(settings.POSTGRES_HOST, "127.0.0.1")
        self.assertEqual(settings.POSTGRES_PORT, 1)
        self.assertEqual(settings.POSTGRES_DB, "tc_backend_pytest")
        self.assertEqual(settings.POSTGRES_USER, "tc_backend_pytest")
        self.assertEqual(settings.POSTGRES_PASSWORD, "tc_backend_pytest_password")
        self.assertIsNone(settings.TC_BACKEND_AUTH_TOKEN)
        self.assertEqual(settings.GOOGLE_API_KEY, "")
        self.assertEqual(settings.GOOGLE_MAP_API_KEY, "")
        self.assertEqual(settings.GOOGLE_VISION_CREDENTIAL, "")
        self.assertEqual(settings.WEATHER_API_KEY, "")
        self.assertEqual(settings.ASTROMETRY_API_KEY, "")
        Fernet(settings.MASTER_KEY.encode("ascii"))

    def test_runtime_database_target_is_local_and_unambiguously_test_only(self) -> None:
        target = make_url(DATABASE_URL)

        self.assertEqual(target.host, "127.0.0.1")
        self.assertEqual(target.port, 1)
        self.assertEqual(target.database, "tc_backend_pytest")
        self.assertEqual(target.username, "tc_backend_pytest")
        self.assertEqual(target.password, "tc_backend_pytest_password")

    def test_local_dotenv_is_overridden_and_database_import_does_not_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text(
                "\n".join(
                    (
                        "POSTGRES_HOST=local-production.invalid",
                        "POSTGRES_PORT=5432",
                        "POSTGRES_DB=local_production",
                        "POSTGRES_USER=local_production_user",
                        "POSTGRES_PASSWORD=local-production-password",
                        "MASTER_KEY=not-a-valid-fernet-key",
                        "TC_BACKEND_AUTH_TOKEN=local-production-token",
                        "GOOGLE_API_KEY=local-production-google-key",
                        "WEATHER_API_KEY=local-production-weather-key",
                        "ASTROMETRY_API_KEY=local-production-astrometry-key",
                    )
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            script = "\n".join(
                (
                    "import sys",
                    f"sys.path.insert(0, {str(PROJECT_ROOT)!r})",
                    "from sqlalchemy.engine.base import Engine",
                    "def forbidden_connect(self, *args, **kwargs):",
                    "    raise AssertionError('database connection attempted during import')",
                    "Engine.connect = forbidden_connect",
                    "from app.common.config import settings",
                    "from app.common.database import DATABASE_URL",
                    "assert settings.POSTGRES_HOST == '127.0.0.1'",
                    "assert settings.POSTGRES_PORT == 1",
                    "assert settings.POSTGRES_DB == 'tc_backend_pytest'",
                    "assert settings.POSTGRES_USER == 'tc_backend_pytest'",
                    "assert settings.POSTGRES_PASSWORD == 'tc_backend_pytest_password'",
                    "assert settings.TC_BACKEND_AUTH_TOKEN is None",
                    "assert settings.GOOGLE_API_KEY == ''",
                    "assert settings.WEATHER_API_KEY == ''",
                    "assert settings.ASTROMETRY_API_KEY == ''",
                    "assert 'local-production.invalid' not in DATABASE_URL",
                    "assert 'local-production-password' not in DATABASE_URL",
                )
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
