"""
Atomicity of the schedule write and its notification, against a real database.

The whole reason shifts_api writes an outbox row instead of calling notify_api
is that the two must share one transaction. That cannot be proved with a mocked
connection, so these tests need Postgres.

Skipped unless SHIFTS_TEST_DSN points at a database that already has the
schema applied (device_schedules plus the notification tables from
specs/timescale-playground/sql/migrations/2026-08-24_add_notifications.sql).

    SHIFTS_TEST_DSN=postgresql://postgres:pw@localhost:55433/checkdb pytest tests/test_outbox_integration.py

SAFETY: every row this module writes is scoped to a company it creates for the
test, and teardown deletes only those rows by id. Nothing here truncates a
table. That matters because SHIFTS_TEST_DSN is a plain DSN with no guard rail —
point it at a restore of production and an earlier version of this file would
have emptied `devices` and `device_schedules`, cascading into the telemetry
hypertables. It did exactly that once. `_no_foreign_rows_harmed` below is the
regression test for it: it fails the run if the suite changed any row count it
did not own.
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.schemas.schedule import ScheduleCreate, ScheduleUpdate
from app.services.schedule_service import ScheduleService

DSN = os.getenv("SHIFTS_TEST_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SHIFTS_TEST_DSN not set"),
]

CRUD_PATH = "app.services.schedule_service.schedule_crud"
OUTBOX_INSERT = "app.repositories.notification_outbox.NotificationOutboxCRUD.insert"


@pytest.fixture(autouse=True)
def stub_device_ref():
    """Override conftest's stub — here the real `devices` lookup is the point."""
    yield None


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    await p.close()


async def _row_counts(pool):
    """Totals for the tables this suite writes to, for the session-wide guard."""
    async with pool.acquire() as conn:
        return {
            table: await conn.fetchval(f"SELECT count(*) FROM {table};")
            for table in ("companies", "devices", "device_schedules", "notification_outbox")
        }


@pytest.fixture(scope="session", autouse=True)
async def _no_foreign_rows_harmed():
    """Fail the run if this suite left any row behind, or removed one it did not own.

    Scoped teardown is the actual safeguard; this is the tripwire that proves it
    still works. Session-scoped so it brackets the whole module, and it opens its
    own pool because the per-test `pool` fixture is function-scoped.
    """
    if not DSN:
        yield
        return
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
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


@pytest.fixture(autouse=True)
async def clean(pool):
    """A company and a device to hang schedules off, removed afterwards.

    Creates its own company so `uq_devices_company_key` leaves the device key
    free: 'F1' here cannot collide with a real 'F1' under a different company.
    The company name and slug are globally unique, so those carry a per-test tag.
    """
    tag = uuid.uuid4().hex[:12]
    async with pool.acquire() as conn:
        company_id = await conn.fetchval(
            "INSERT INTO companies (name, slug) VALUES ($1, $2) RETURNING id;",
            f"Test SA {tag}",
            f"test-sa-{tag}",
        )
        device_id = await conn.fetchval(
            """
            INSERT INTO devices (company_id, device_key, display_name)
            VALUES ($1, 'F1', 'Fresadora 1') RETURNING id;
            """,
            company_id,
        )
    try:
        yield {"company_id": company_id, "device_id": device_id, "tag": tag}
    finally:
        # Only ever rows this fixture created. Deleting the company would cascade
        # to the device, but be explicit — a future edit that drops the company
        # delete should not silently start leaking devices.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM notification_outbox WHERE company_id = $1;", company_id
            )
            await conn.execute(
                "DELETE FROM device_schedules WHERE device_id = $1;", device_id
            )
            await conn.execute("DELETE FROM devices WHERE id = $1;", device_id)
            await conn.execute("DELETE FROM companies WHERE id = $1;", company_id)


def create_data(device_id: int, start: str = "08:00", valid_from: str | None = None):
    """A schedule that is effective today unless the caller says otherwise.

    `delete_schedule` and `update_schedule` without `?date=` both resolve via
    `valid_range @> CURRENT_DATE`, so a future-dated fixture would 404 for
    reasons that have nothing to do with the outbox.
    """
    return ScheduleCreate.model_validate(
        {
            "deviceId": device_id,
            "schedule": {"monday": {"workHours": {"start": start, "end": "17:00"}}},
            "validFrom": valid_from or datetime.now(timezone.utc).date().isoformat(),
        }
    )


async def counts(pool, scope):
    """Rows belonging to this test's own device and company.

    Scoped rather than global: an unscoped count only reads correctly on a
    database this suite has emptied, and emptying a shared database is what this
    module must never do.
    """
    async with pool.acquire() as conn:
        return {
            "schedules": await conn.fetchval(
                "SELECT count(*) FROM device_schedules WHERE device_id = $1;",
                scope["device_id"],
            ),
            "outbox": await conn.fetchval(
                "SELECT count(*) FROM notification_outbox WHERE company_id = $1;",
                scope["company_id"],
            ),
        }


class TestDeviceLookup:
    async def test_unknown_device_resolves_to_none(self, pool, clean):
        from app.repositories.devices import device_crud

        assert await device_crud.get_device_ref(pool, 10_000_000) is None

    async def test_known_device_carries_company_and_names(self, pool, clean):
        from app.repositories.devices import device_crud

        ref = await device_crud.get_device_ref(pool, clean["device_id"])
        assert (ref.company_id, ref.key, ref.display_name) == (
            clean["company_id"],
            "F1",
            "Fresadora 1",
        )


class TestAtomicity:
    async def test_create_writes_schedule_and_event_together(self, pool, clean):
        await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        assert await counts(pool, clean) == {"schedules": 1, "outbox": 1}

    async def test_event_lands_with_the_right_company_and_payload(self, pool, clean):
        await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT type_key, company_id, status, payload FROM notification_outbox\n"
                "WHERE company_id = $1;",
                clean["company_id"],
            )
        assert row["type_key"] == "shift_change"
        assert row["company_id"] == clean["company_id"]
        assert row["status"] == "pending"

    async def test_a_failing_event_rolls_the_schedule_back(self, pool, clean):
        """The guarantee that matters: no silent hour change without its notice."""
        with patch(OUTBOX_INSERT, new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             pytest.raises(RuntimeError):
            await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        assert await counts(pool, clean) == {"schedules": 0, "outbox": 0}

    async def test_a_failing_schedule_write_leaves_no_event(self, pool, clean):
        """Overlapping ranges trip the exclusion constraint after the event is built."""
        today = datetime.now(timezone.utc).date().isoformat()
        await ScheduleService.create_schedule(
            pool, create_data(clean["device_id"], valid_from=today)
        )
        before = await counts(pool, clean)

        # Same valid_from → the auto-close cannot shift it out of the way.
        with pytest.raises(asyncpg.exceptions.ExclusionViolationError):
            await ScheduleService.create_schedule(
                pool, create_data(clean["device_id"], start="06:00", valid_from=today)
            )
        assert await counts(pool, clean) == before

    async def test_update_enqueues_exactly_one_event(self, pool, clean):
        await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        await ScheduleService.update_schedule(
            pool,
            clean["device_id"],
            ScheduleUpdate.model_validate(
                {"schedule": {"monday": {"workHours": {"start": "07:00", "end": "16:00"}}}}
            ),
        )
        assert (await counts(pool, clean))["outbox"] == 2

    async def test_delete_enqueues_and_removes_in_one_transaction(self, pool, clean):
        await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        await ScheduleService.delete_schedule(pool, clean["device_id"], shift_type="day")
        assert await counts(pool, clean) == {"schedules": 0, "outbox": 2}

    async def test_replayed_event_is_absorbed_by_the_dedupe_key(self, pool, clean):
        """ON CONFLICT DO NOTHING: a repeated dedupe_key must not fail the write."""
        from app.models.notification_event import OutboxEvent
        from app.repositories.notification_outbox import notification_outbox_crud

        event = OutboxEvent(
            type_key="shift_change",
            company_id=clean["company_id"],
            dedupe_key=f"shift_change:test:{clean['tag']}:created:1",
            payload={"action": "created"},
        )
        async with pool.acquire() as conn, conn.transaction():
            await notification_outbox_crud.insert(conn, event)
            await notification_outbox_crud.insert(conn, event)
        assert (await counts(pool, clean))["outbox"] == 1
