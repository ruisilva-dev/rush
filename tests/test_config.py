from app.core.config import Settings


def test_settings_postgres_url() -> None:
    settings = Settings()

    # Verify the secret file was read successfully
    assert settings.postgres_password is not None

    # Verify the URL components constructed by SQLAlchemy
    assert settings.postgres_url.drivername == "postgresql+asyncpg"
    assert settings.postgres_url.username == settings.postgres_user
    assert settings.postgres_url.database == settings.postgres_db
