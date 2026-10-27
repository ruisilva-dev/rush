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

## Known Limitations (Accepted)

### Stale `SCHEDULED` Status in Read Endpoints
- **Behavior**: A drop moves `SCHEDULED` -> `ACTIVE` only inside the purchase endpoints. Until its first purchase, a drop whose `scheduled_at` has passed is still returned as `scheduled` by `GET /drops` and `GET /drops/{id}`.
- **Impact**: Display only. Purchasing is unaffected: purchase endpoints compare `scheduled_at` to the DB clock and never read the stored `status`. `COMPLETED` is unaffected (set transactionally on the last purchase).
- **Why accepted**: The project is backend-only and API consumers call endpoints directly. Do not "fix" this without a consumer that needs it.
- **Required fix if a frontend (or any consumer that reads `status` from listings) is added**: derive the effective status at read time in `_drop_with_available_count_query()` (`CASE WHEN status = 'scheduled' AND scheduled_at <= now() THEN 'active' ELSE status END`) and use it in the `DropResponse` mapping. Write failing tests first.
