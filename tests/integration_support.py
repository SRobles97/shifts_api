"""Helpers shared by the integration test modules (real Postgres, SHIFTS_TEST_DSN)."""

import asyncpg
import pytest


def make_row_count_tripwire(dsn, tables):
    """Build the session-wide fixture that fails a run which leaked or removed rows.

    Scoped teardown is the actual safeguard; this proves it still works by
    comparing row counts of `tables` before and after the module. Assign the
    result to a module-level name so pytest collects it as an autouse fixture.
    """

    async def _row_counts(pool):
        async with pool.acquire() as conn:
            return {
                table: await conn.fetchval(f"SELECT count(*) FROM {table};")
                for table in tables
            }

    @pytest.fixture(scope="session", autouse=True)
    async def _no_foreign_rows_harmed():
        if not dsn:
            yield
            return
        p = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            before = await _row_counts(p)
            yield
            after = await _row_counts(p)
        finally:
            await p.close()
        assert after == before, (
            "This suite must only touch rows it created. Row counts changed: "
            f"{ {t: (before[t], after[t]) for t in before if before[t] != after[t]} }"
        )

    return _no_foreign_rows_harmed
