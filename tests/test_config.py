from pathlib import Path

import pytest

from app.core.config import Settings


def test_settings_postgres_url() -> None:
    settings = Settings()

    # Verify the secret file was read successfully
    assert settings.postgres_password is not None

    # Verify the URL components constructed by SQLAlchemy
    assert settings.postgres_url.drivername == "postgresql+asyncpg"
    assert settings.postgres_url.username == settings.postgres_user
    assert settings.postgres_url.database == settings.postgres_db


def test_settings_reads_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "db_user_password"
    secret.write_text("s3cret\n")

    settings = Settings(
        postgres_user="u", postgres_db="d", postgres_password_file=secret
    )

    assert settings.postgres_password == "s3cret"
    assert settings.postgres_url.password == "s3cret"


def test_settings_missing_secret_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="Password file not found"):
        Settings(postgres_user="u", postgres_db="d", postgres_password_file=missing)


def test_settings_empty_secret_file_raises(tmp_path: Path) -> None:
    secret = tmp_path / "db_user_password"
    secret.write_text("  \n")

    with pytest.raises(ValueError, match="Password file is empty"):
        Settings(postgres_user="u", postgres_db="d", postgres_password_file=secret)
