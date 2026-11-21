import os
import subprocess
import sys
import unittest
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


BACKEND_DIR = Path(__file__).resolve().parents[1]


class AlembicConfigurationTests(unittest.TestCase):
    def test_migration_directory_is_configured(self) -> None:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        script_directory = ScriptDirectory.from_config(config)
        self.assertEqual(
            Path(script_directory.dir).resolve(),
            (BACKEND_DIR / "alembic").resolve(),
        )
        self.assertTrue((BACKEND_DIR / "alembic" / "versions").is_dir())

    def test_offline_migration_environment_loads(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "DB_HOST": "127.0.0.1",
                "DB_PORT": "5432",
                "DB_NAME": "schema_test",
                "DB_USER": "schema_test_user",
                "DB_PASSWORD": "temporary-test-password",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "upgrade",
                "head",
                "--sql",
            ],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BEGIN;", result.stdout)
        self.assertIn("COMMIT;", result.stdout)


if __name__ == "__main__":
    unittest.main()
