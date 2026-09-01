from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REQUIRED_SETTINGS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "MASTER_KEY",
)


class ModelMetadataRegistryTests(unittest.TestCase):
    def test_registry_import_needs_no_runtime_settings(self) -> None:
        environment = os.environ.copy()
        for setting_name in RUNTIME_REQUIRED_SETTINGS:
            environment.pop(setting_name, None)

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "\n".join(
                    (
                        "import sys",
                        "from app.common.model_registry import Base",
                        "from migrations.baseline import BASELINE_REQUIRED_TABLES",
                        "assert 'app.common.config' not in sys.modules",
                        "assert BASELINE_REQUIRED_TABLES <= set(Base.metadata.tables)",
                    )
                ),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=result.stdout + result.stderr,
        )

    def test_model_base_and_registry_share_one_base(self) -> None:
        from app.common.model_base import Base as model_base
        from app.common.model_registry import Base as registry_base

        self.assertIs(model_base, registry_base)


if __name__ == "__main__":
    unittest.main()
