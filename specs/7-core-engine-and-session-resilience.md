# Technical Specification: Core Engine & Session Resilience (Group 1)

## 1. Overview, Scope and Requirements

### Problem Statement
The application core operates within a strict container-first paradigm (where all execution, testing, and migrations run inside Docker). Within this architecture, it faces two foundational resilience and security requirements:
1. **Container-Native File Secret Resolution** ([app/core/config.py](file:///home/rilay/rush/app/core/config.py#L15)): In line with project security principles, database credentials must never be passed in plaintext environment variables. Passwords must be sourced exclusively from in-memory tmpfs secret files (`POSTGRES_PASSWORD_FILE`, defaulting to `/run/secrets/db_user_password`). The configuration model must cleanly validate secret file presence and fail fast if missing or empty, preventing half-initialized application startup.
2. **`BaseException` & Task Cancellation Vulnerability** ([app/core/db.py](file:///home/rilay/rush/app/core/db.py#L18)): `get_db()` catches only `Exception`. In Python 3.8+, `asyncio.CancelledError` inherits from `BaseException`. When an HTTP client disconnects while waiting on a database lock (such as during row lock contention in pessimistic purchase), `CancelledError` bypasses the `await session.rollback()` block, leaving locked rows held in PostgreSQL until session teardown.

### Explicit Non-Goals
* Allowing plaintext database passwords in environment variables (`POSTGRES_PASSWORD`).
* Introducing host-filesystem fallbacks or path sniffing outside the container boundary.
* Switching to a synchronous database driver or replacing `asyncpg`.
* Reworking endpoint business logic (belongs in Group 3).

### Functional Requirements & Acceptance Criteria
* **AC-1.1**: Database credentials must be loaded exclusively via container secret files (`POSTGRES_PASSWORD_FILE`, defaulting to `/run/secrets/db_user_password`). Raw password environment variables (`POSTGRES_PASSWORD`) must be rejected to prevent credential leaks via `docker inspect`, `/proc/<pid>/environ`, child processes, or crash telemetry.
* **AC-1.2**: If the secret file at `POSTGRES_PASSWORD_FILE` is missing or empty, startup must fail fast with a descriptive `ValueError`.
* **AC-1.3**: In `get_db()`, any exception inheriting from `BaseException` (specifically `asyncio.CancelledError`) must trigger `await session.rollback()` before propagating.

---

## 2. Architecture and Execution Flow

```
HTTP Request / Test Client
           │
           ▼
FastAPI Dependency: get_db()
           │
    ┌──────┴─────────────────────────────────────────────┐
    │ async with AsyncSessionLocal() as session:         │
    │   try:                                             │
    │       yield session ──────────► [Endpoint Handler] │
    │   except BaseException:                            │
    │       await session.rollback()                     │
    │       raise                                        │
    └────────────────────────────────────────────────────┘
```

### Component Responsibilities
* `app.core.config.Settings`: Pydantic settings model resolving PostgreSQL connection parameters strictly via Docker secrets with fail-fast validation.
* `app.core.db`: Manages the shared `AsyncEngine` instance, the `async_sessionmaker`, and the `get_db()` generator dependency injected into FastAPI endpoints with cancellation rollback guarantees.

---

## 3. Interfaces, State and Contracts

### Settings Schema (`app/core/config.py`)
```python
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
```

### Database Dependency (`app/core/db.py`)
```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
```

---

## 4. Security and Trust Boundaries
* **Strict Secret Isolation**: Passwords never enter process environment blocks (`os.environ`). They reside exclusively on `tmpfs` mounts (`/run/secrets`), preventing leaks via `/proc/<pid>/environ`, `docker inspect`, and telemetry.
* **Credential Masking**: Password strings must never appear in raw application logs. `settings.postgres_url.render_as_string(hide_password=True)` must be preserved for all telemetry.
* **File Traversal Prevention**: `postgres_password_file` must strictly resolve to regular files (`is_file()`).
* **Connection Hygiene**: Rolling back on `BaseException` prevents dirty transaction state from returning to the connection pool upon client disconnection.

---

## 5. Performance, Scalability and Resource Budgets
* **Engine Lifecycle**: A single singleton `AsyncEngine` is created at application startup and disposed during FastAPI lifespan shutdown.
* **Pool Configuration**:
  * `pool_pre_ping=True` prevents stale connection errors after Postgres container restarts.
  * Default pool size (`5`) and max overflow (`10`) within Postgres max connection limits.

---

## 6. Failure Modes, Edge Cases and Observability
* **Task Cancellation during Lock Wait**: When a client terminates an HTTP connection during `SELECT ... FOR UPDATE`, FastAPI cancels the request task. `get_db()` catches `asyncio.CancelledError` via `BaseException`, executes `await session.rollback()`, releases the row lock in PostgreSQL immediately, and re-raises.
* **Missing Secret at Startup**: If the secret file is missing or unmounted, container startup fails fast with an explicit `ValueError` indicating the missing file path.

### 6.1 TDD Reproduction & Test Matrix (`tests/test_config.py`, `tests/test_db.py`)
To prevent regressions and verify container secret integrity and cancellation handling:
1. `test_settings_reads_secret_file`: Verify `/run/secrets/db_user_password` is read and populated in `postgres_url`.
2. `test_settings_missing_secret_file_raises`: Verify an explicit `ValueError` when `postgres_password_file` does not exist.
3. `test_settings_empty_secret_file_raises`: Verify an explicit `ValueError` when the secret file is empty.
4. `test_get_db_cancelled_error_rollback`: Verify in `tests/test_db.py` that simulating `asyncio.CancelledError` inside `get_db()` executes `await session.rollback()`.

---

## 7. Architecture Decisions and Trade-offs (ADR Log)

### ADR 1.1: Catching `BaseException` vs `Exception` in Generator Dependency
* **Context**: Client disconnects raise `asyncio.CancelledError`, which inherits from `BaseException` in Python 3.8+.
* **Decision**: Catch `BaseException` in `get_db()`.
* **Alternatives Rejected**:
  * Catching only `asyncio.CancelledError`: Fragile if other ASGI server cancellations or base system signals propagate.
  * Relying on `session.close()` inside context manager exit: While `close()` issues a rollback on checkout return, relying on it implicitly delays lock release and risks masked exceptions.
* **Trade-off**: Requires immediate re-raising to avoid swallowing interpreter exit signals (`KeyboardInterrupt`, `SystemExit`).

### ADR 1.2: Enforcing Container Secrets vs Plaintext Environment Variables
* **Context**: Application credentials can be configured via environment variables or file mounts.
* **Decision**: Enforce file-based secrets via `/run/secrets/db_user_password` (with configurable path via `POSTGRES_PASSWORD_FILE`). Prohibit raw password environment variables.
* **Alternatives Rejected**: Allowing `POSTGRES_PASSWORD` env var (rejected due to plaintext exposure in `docker inspect`, `/proc/environ`, and APM error logs).
* **Trade-off**: Requires test runners and development environments to use Docker Compose secret mounts (`docker-compose.yml`, `docker-compose.test.yml`), which matches the project's container-native requirement.
