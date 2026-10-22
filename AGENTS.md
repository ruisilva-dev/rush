# Rush API - Architecture & Engineering Guidelines

## Project Overview
Rush is a terminal-first, high-concurrency inventory drop API built with FastAPI, SQLAlchemy 2.0 (asyncpg), and PostgreSQL 18.

## Essential Commands

### Testing Framework
We use Docker Compose for isolated testing. Do not run `pytest` directly on the host. Instead, use the test runner in `scripts/`:
- Run all tests: `./scripts/agent-test.sh all`
- Run a specific file: `./scripts/agent-test.sh file test_drops.py`
- Run a specific test function: `./scripts/agent-test.sh func test_drops.py test_create_drop_success`
- Clean up test containers: `./scripts/agent-test.sh clean`

### Sandboxed Agent Testing via MCP
If the agent runs in a sandbox where `/var/run/docker.sock` is blocked, register the custom test runner MCP server:
```bash
claude mcp add rush-tester -- uv run --with mcp python scripts/agent_tester_mcp.py
```
This grants the agent access to the `run_test_function`, `run_test_file`, `run_all_tests`, and `clean_test_environment` tools via host stdio.

### Code Quality & Migrations
- Lint & format check: `uv run ruff check app alembic tests && uv run ruff format --check app alembic tests`
- Auto-format: `uv run ruff format app alembic tests && uv run ruff check --fix app alembic tests`
- Apply migrations: `make migrate` (requires `make up`)
- Autogenerate migration: `make revision m="your_migration_name"` (requires `make up`)
- *Sandbox note*: If running in a restricted sandbox where default cache or root files trigger permissions errors, set `UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"`.

## Architectural Conventions

### 1. Database & SQLAlchemy 2.0
- Base Class: All models inherit from `Base` (`app.models.base.DeclarativeBase`).
- Typing: Use `Mapped[T]` and `mapped_column()` exclusively. Never use legacy `Column()` or `db.query()`.
- Type Inference: Rely on type annotations for primitives (e.g., `id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))`).
- Enums: Inherit from `enum.StrEnum`. Map to PostgreSQL using:
  `sa.Enum(MyEnum, name="my_enum_name", native_enum=True, values_callable=lambda x: [e.value for e in x])`.
  *Note*: `values_callable` is mandatory because Postgres enums store lowercase values (`'draft'`, `'available'`), while Python enum member names are uppercase (`'DRAFT'`, `'AVAILABLE'`). Omitting it causes SQLAlchemy to serialize member names, failing database constraints.
- Transactions:
  - Write endpoints must use `async with db.begin():` for atomic boundaries.
  - Read endpoints run directly via `await db.execute()` without calling `commit()`.
  - In `app/core/db.py`, `get_db()` must catch `BaseException` to safely roll back on `asyncio.CancelledError`.

### 2. FastAPI & Schemas
- Return typed Pydantic models explicitly from route handlers (e.g., `return PurchaseResponse.model_validate(unit)`).
- Never return raw ORM models from endpoints.
- Timestamps must be timezone-aware UTC (`DateTime(timezone=True)`).
- Export public interfaces in each package's `__init__.py` using `__all__`.

### 3. Alembic Migrations
- In `alembic/env.py`, import all models explicitly alongside `Base`:
  `from app.models import Base, Drop, DropUnit  # noqa: F401`
- Ensure `compare_type=True` and `compare_server_default=True` are in `context.configure()`.

### 4. Testing Guidelines
- Unit/Single-client: Use `client` and `db_session` fixtures (runs inside savepoint rollbacks).
- Concurrency: Use `concurrent_client` and `committed_session`. Any manually acquired `with_for_update` lock must roll back in a `finally` block before assertions finish.

---

## Bugfix & Implementation Protocol (TDD)
When tasked with resolving issues or bugs:
1. **Blast-Radius Containment**: Touch ONLY files and functions specified in the task or spec. Never refactor surrounding code or alter unrelated variable names.
2. **Reproduction Test First**: Before modifying application logic, write a minimal failing test under `tests/` reproducing the issue. Run the targeted Docker test command to confirm failure.
3. **Surgical Fix**: Apply the minimum changes required to pass the reproduction test while adhering to architectural conventions.
4. **Validation**: Run the targeted test to confirm green, run `ruff` checks, and inspect `git diff` before declaring completion.

---

## Agent Execution Rules (Token Safeguards)
- During iteration, test ONLY the modified function/file using:
  `./scripts/agent-test.sh func <test_file> <function_name>` (or via the MCP `run_test_function` tool).
  Do not run full `./scripts/agent-test.sh all` until the task is complete.
- If a test fails twice on the same error, stop immediately, explain the blockage, and wait for user input. Do not loop.
- Do not edit files outside the requested scope.
- In sandboxed environments where `.env` or `./secrets` are denied, always pass `--env-file /dev/null` to Docker Compose commands.

---

## Appendix: Open Issues & Tech Debt Backlog (Dependency-Ordered)

### Group 1: Core Engine & Session Resilience (Foundation)
*Specification*: [specs/01-core-engine-and-session-resilience.md](file:///home/rilay/rush/specs/01-core-engine-and-session-resilience.md)
1. **Top-Level Settings Import Side-Effect (`app/core/db.py`)**:
   - `settings = Settings()` runs at module import time in `app/core/db.py`. Because `Settings` requires `/run/secrets/db_user_password` by default, importing `app.main` or `app.core.db` outside Docker without environment overrides fails immediately with a `ValidationError`.
2. **`BaseException` & Cancellation Vulnerability in `get_db()` (`app/core/db.py`)**:
   - In `get_db()`, `except Exception:` catches standard exceptions but fails to catch `BaseException`. In Python 3.8+, `asyncio.CancelledError` inherits from `BaseException`. If a client disconnects while waiting on a row lock in `purchase_pessimistic`, `CancelledError` bypasses `await session.rollback()`.

### Group 2: Test Harness & Concurrency Reliability (Testing Safety Net)
*Specification*: [specs/02-test-harness-and-concurrency-reliability.md](file:///home/rilay/rush/specs/02-test-harness-and-concurrency-reliability.md)
3. **Dead Connection & Transaction Overhead in `committed_session` (`tests/conftest.py`)**:
   - `committed_session` runs `async with engine.connect() as connection: async with connection.begin():`, but binds `session_factory` to `engine` instead of `connection`. This opens an idle connection that is never used and leaks it for the duration of the test.
4. **Dirty State Vulnerability & Lock Hangs in Concurrency Tests (`tests/conftest.py`, `tests/test_purchase_pessimistic.py`)**:
   - Concurrency tests acquiring explicit row locks (`with_for_update`) that fail assertions before manual rollback leave open transactions. Fixture cleanup then invokes `delete(Drop)`, triggering potential deadlocks or `InFailedSqlTransactionError`.
   - Deleting only `Drop` assumes cascade delete behavior for all future tables, with no database truncation or verification hook.

### Group 3: API Transaction & Serialization Contracts (HTTP Layer)
*Specification*: [specs/03-api-transaction-and-serialization-contracts.md](file:///home/rilay/rush/specs/03-api-transaction-and-serialization-contracts.md)
5. **Divergent Transaction Boundary Ownership Across Endpoints (`app/api/drops.py`)**:
   - `create_drop` wraps operations inside `async with db.begin():` (explicit transaction block auto-committing on exit).
   - `purchase_naive` and `purchase_pessimistic` rely on implicit SQLAlchemy autobegin upon the first `select()`, and manually call `await db.commit()`.
   - Read-only endpoints (`list_drops`, `get_drop`) autobegin a transaction upon `await db.execute()` that is never explicitly closed or committed, dangling until session teardown.
   - Mixing `async with db.begin():` with pre-existing queries raises `InvalidRequestError: A transaction is already begun on this Session`.
6. **Inconsistent Response Serialization Strategy (`app/api/drops.py`)**:
   - `create_drop`, `list_drops`, and `get_drop` manually construct Pydantic `DropResponse` instances field-by-field.
   - `purchase_naive` and `purchase_pessimistic` return the raw SQLAlchemy ORM model directly (`return unit`), relying on `response_model=PurchaseResponse` and `expire_on_commit=False`.
   - All endpoints should standardize on returning validated Pydantic models (e.g., `return PurchaseResponse.model_validate(unit)`).

### Group 4: Domain Schema & State Machine Evolution (Business Logic)
*Specification*: [specs/04-domain-schema-and-state-machine-evolution.md](file:///home/rilay/rush/specs/04-domain-schema-and-state-machine-evolution.md)
7. **Half-Implemented Hold Reservation & Semantic Field Conflation (`app/models/drop_unit.py`, `app/schemas/purchase.py`)**:
   - `DropUnit` defines `held_by_user_id`, `hold_expires_at`, and status `DropUnitStatus.HELD`, but no hold logic exists. Purchases store buyer IDs into `held_by_user_id` alongside `status = PURCHASED`, aliased as `user_id` in `PurchaseResponse`. Storing purchase ownership in a hold field conflates temporary reservations with final purchases.
8. **Static Drop Lifecycle State Machine (`app/models/drop.py`)**:
   - `DropStatus` defines `ACTIVE` and `COMPLETED`, but the application never transitions drops into these states. Drops remain `SCHEDULED` or `DRAFT` permanently, even when `scheduled_at` has passed or available inventory reaches zero.
