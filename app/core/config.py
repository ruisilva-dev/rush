from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from sqlalchemy.engine import URL
from pathlib import Path
from typing import Self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_user: str
    postgres_db: str
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_password_file: Path = Path("/run/secrets/db_user_password")
    postgres_password: str | None = None

    @property
    def postgres_url(self) -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @model_validator(mode="after")
    def read_password(self) -> Self:
        if not self.postgres_password_file.is_file():
            raise ValueError(
                f"Password file not found at: {self.postgres_password_file}"
            )

        password = self.postgres_password_file.read_text().strip()
        if not password:
            raise ValueError("Password file is empty.")

        self.postgres_password = password
        return self
