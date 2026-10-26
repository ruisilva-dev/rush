# Technical Specification: Test Harness & Concurrency Reliability (Group 2)

## 1. Overview, Scope and Requirements

### Problem Statement
The test suite in [tests/conftest.py](file:///home/rilay/rush/tests/conftest.py) features two severe concurrency testing defects:
1. **Dead Connection & Transaction Leak in `committed_session`** ([tests/conftest.py#L39-L45](file:///home/rilay/rush/tests/conftest.py#L39-L45)):
   `committed_session` opens a connection and transaction (`async with engine.connect() as connection: async with connection.begin():`), but configures `async_sessionmaker(bind=engine)`. The outer `connection` is completely unused, remaining open and idle during tests, burning PostgreSQL connection pool slots.
2. **Dirty State Vulnerability & Lock Hangs on Assertion Failures** ([tests/conftest.py#L50-L51](file:///home/rilay/rush/tests/conftest.py#L50-L51), [tests/test_purchase_pessimistic.py#L216-L230](file:///home/rilay/rush/tests/test_purchase_pessimistic.py#L216-L230)):
   Concurrency tests (e.g., `test_purchase_pessimistic_lock_held_deterministic`) acquire explicit `FOR UPDATE` row locks on `committed_session`. If an assertion fails prior to `await committed_session.rollback()`, the session remains trapped inside an active transaction. The fixture teardown attempts `await session.execute(delete(Drop))`, which triggers deadlocks or `sqlalchemy.exc.PendingRollbackError` / `InFailedSqlTransactionError`, leaking dirty state into subsequent test runs.

### Explicit Non-Goals
* Converting concurrency tests to savepoint rollbacks (concurrency testing requires real committed data and distinct physical connections to test Postgres row locking).
* Replacing `pytest-asyncio` with another runner.

### Functional Requirements & Acceptance Criteria
* **AC-2.1**: `committed_session` must eliminate the redundant `engine.connect()` outer context, binding its session factory directly to the engine without leasing unused connections.
* **AC-2.2**: In `committed_session` fixture teardown, an explicit `await session.rollback()` must always precede cleanup commands to clear any active or failed transaction blocks.
* **AC-2.3**: Manual row locks in concurrency tests must be guarded with `try ... finally` blocks to ensure locks are released even if test assertions fail.
* **AC-2.4**: Teardown database cleanup must execute `TRUNCATE TABLE drops CASCADE;` guarded by a local lock timeout (`SET LOCAL lock_timeout = '2s'`) to eliminate foreign key constraints and prevent indefinite test hangs.
* **AC-2.5**: Both `committed_session` and `concurrent_client` fixtures must configure `poolclass=NullPool` on their test engines to guarantee immediate connection release.

---

## 2. Architecture and Execution Flow

```
+--------------------------------------------------------------------------------+
| pytest Test Session (Docker Container)                                         |
|                                                                                |
|  [Single-Client Tests]                           [Concurrency Tests]           |
|  Fixture: db_session / client                    Fixture: committed_session    |
|                                                           concurrent_client    |
|  1. Lease 1 Connection                           1. NullPool Engines Bind      |
|  2. connection.begin() [Root Tx]                 2. Seed Drop & Units (commit) |
|  3. join_transaction_mode="create_savepoint"     3. Concurrent Clients Contend |
|  4. Yield Session to Test                        4. Test Finishes / Fails      |
|  5. connection.rollback() [Zero Dirty State]     5. Teardown:                  |
|                                                     - session.rollback()       |
|                                                     - SET lock_timeout = '2s'  |
|                                                     - TRUNCATE drops CASCADE   |
|                                                     - engine.dispose()         |
+--------------------------------------------------------------------------------+
```

### Component Responsibilities
* `db_session` & `client`: Savepoint rollback harness for single-client, non-concurrent integration tests.
* `committed_session` & `concurrent_client`: Real commit harness for testing multi-connection race conditions and PostgreSQL row-level locks (`SELECT ... FOR UPDATE`).

---

## 3. Interfaces, State and Contracts

### Canonical `committed_session` Fixture (`tests/conftest.py`)
```python
@pytest_asyncio.fixture
async def committed_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(settings.postgres_url, poolclass=NullPool)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        try:
            yield session
        finally:
            # Clear any dangling or failed transaction state
            await session.rollback()
            # Clean up all seeded data cleanly with lock timeout safeguard
            await session.execute(text("SET LOCAL lock_timeout = '2s';"))
            await session.execute(text("TRUNCATE TABLE drops CASCADE;"))
            await session.commit()

    await engine.dispose()
```

### Canonical `concurrent_client` Fixture (`tests/conftest.py`)
```python
@pytest_asyncio.fixture
async def concurrent_client() -> AsyncGenerator[AsyncClient, None]:
    engine = create_async_engine(settings.postgres_url, poolclass=NullPool)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    await engine.dispose()
```

### Safe Lock Acquisition in Concurrency Tests (`tests/test_purchase_pessimistic.py`)
```python
# Acquire lock inside try/finally
lock_stmt = select(DropUnit).where(DropUnit.id == unit.id).with_for_update()
try:
    locked_unit = await committed_session.scalar(lock_stmt)
    assert locked_unit is not None

    # Assert concurrent request is blocked or skipped
    res_blocked = await concurrent_client.post(url, json={"user_id": user})
    assert res_blocked.status_code == 409
finally:
    # Guarantee lock release before subsequent test assertions or teardown
    await committed_session.rollback()
```

---

## 4. Security and Trust Boundaries
* **Test Database Isolation**: Tests connect strictly to `rush_test` (port `5432` on `test_postgres`), ensuring dev/prod data is never reachable from test fixtures.
* **Tmpfs Storage**: `test_postgres` operates on `tmpfs`, ensuring data is volatile and isolated to test container lifecycles.

---

## 5. Performance, Scalability and Resource Budgets
* **NullPool Usage**: Fixtures using `NullPool` ensure connections close immediately upon session exit, avoiding pool saturation during high-concurrency tests.
* **Fast Cleanup**: `TRUNCATE TABLE drops CASCADE;` executes in <5ms, significantly faster than recursive ORM `delete(Drop)`.

---

## 6. Failure Modes, Edge Cases and Observability
* **Assertion Failure Mid-Lock**: If `assert res_blocked.status_code == 409` fails, the `finally` block issues `await committed_session.rollback()`. PostgreSQL releases the row lock, preventing teardown deadlocks.
* **Lock Contention on Teardown**: `SET LOCAL lock_timeout = '2s'` ensures that if an external query remains active, `TRUNCATE` aborts after 2 seconds rather than hanging the test process indefinitely.
* **Test Interruption**: If a test run is killed via SIGINT, running `./scripts/agent-test.sh clean` purges the test container and tmpfs volume.

---

## 7. Architecture Decisions and Trade-offs (ADR Log)

### ADR 2.1: Elimination of Outer `engine.connect()` in `committed_session`
* **Context**: `committed_session` opened a connection with `async with engine.connect():` but bound `session_factory` to `engine`.
* **Decision**: Remove the outer `connection` context manager entirely. Bind `session_factory` directly to `engine`.
* **Trade-off**: None; eliminates a leaked idle connection.

### ADR 2.2: `TRUNCATE TABLE drops CASCADE;` with Lock Timeout vs ORM `delete(Drop)`
* **Context**: `await session.execute(delete(Drop))` assumes all child tables cascade on delete and adds query planning overhead. Uncommitted client locks can cause teardowns to hang indefinitely.
* **Decision**: Use `SET LOCAL lock_timeout = '2s'` followed by `TRUNCATE TABLE drops CASCADE;`.
* **Alternatives Rejected**: `DELETE FROM drops` (leaves foreign key constraint edge cases and is significantly slower).
* **Trade-off**: Requires PostgreSQL `CASCADE` support, which is standard for foreign-key-linked relational models.

### ADR 2.3: `NullPool` in `concurrent_client`
* **Context**: `concurrent_client` previously used default `AsyncAdaptedQueuePool`, holding idle connections open across requests.
* **Decision**: Configure `concurrent_client` with `poolclass=NullPool`.
* **Trade-off**: New physical connection per request, ensuring clean isolation during concurrency testing without pool exhaustion.
