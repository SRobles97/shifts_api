"""
Atomicity of the schedule write and its notification, against a real database.

The whole reason shifts_api writes an outbox row instead of calling notify_api
is that the two must share one transaction. That cannot be proved with a mocked
connection, so these tests need Postgres.

Skipped unless SHIFTS_TEST_DSN points at a database that already has the
schema applied (device_schedules plus the notification tables from
specs/timescale-playground/sql/migrations/2026-08-24_add_notifications.sql).

    SHIFTS_TEST_DSN=postgresql://postgres:pw@localhost:55433/checkdb pytest tests/test_outbox_integration.py
"""

import os
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


@pytest.fixture(autouse=True)
async def clean(pool):
    """A company and a device to hang schedules off, torn down afterwards."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM notification_outbox;")
        await conn.execute("DELETE FROM device_schedules;")
        await conn.execute("DELETE FROM devices;")
        await conn.execute("DELETE FROM companies;")
        company_id = await conn.fetchval(
            "INSERT INTO companies (name, slug) VALUES ('Test SA','test-sa') RETURNING id;"
        )
        device_id = await conn.fetchval(
            """
            INSERT INTO devices (company_id, device_key, display_name)
            VALUES ($1, 'F1', 'Fresadora 1') RETURNING id;
            """,
            company_id,
        )
    yield {"company_id": company_id, "device_id": device_id}


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


async def counts(pool):
    async with pool.acquire() as conn:
        return {
            "schedules": await conn.fetchval("SELECT count(*) FROM device_schedules;"),
            "outbox": await conn.fetchval("SELECT count(*) FROM notification_outbox;"),
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
        assert await counts(pool) == {"schedules": 1, "outbox": 1}

    async def test_event_lands_with_the_right_company_and_payload(self, pool, clean):
        await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT type_key, company_id, status, payload FROM notification_outbox;"
            )
        assert row["type_key"] == "shift_change"
        assert row["company_id"] == clean["company_id"]
        assert row["status"] == "pending"

    async def test_a_failing_event_rolls_the_schedule_back(self, pool, clean):
        """The guarantee that matters: no silent hour change without its notice."""
        with patch(OUTBOX_INSERT, new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             pytest.raises(RuntimeError):
            await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        assert await counts(pool) == {"schedules": 0, "outbox": 0}

    async def test_a_failing_schedule_write_leaves_no_event(self, pool, clean):
        """Overlapping ranges trip the exclusion constraint after the event is built."""
        today = datetime.now(timezone.utc).date().isoformat()
        await ScheduleService.create_schedule(
            pool, create_data(clean["device_id"], valid_from=today)
        )
        before = await counts(pool)

        # Same valid_from → the auto-close cannot shift it out of the way.
        with pytest.raises(asyncpg.exceptions.ExclusionViolationError):
            await ScheduleService.create_schedule(
                pool, create_data(clean["device_id"], start="06:00", valid_from=today)
            )
        assert await counts(pool) == before

    async def test_update_enqueues_exactly_one_event(self, pool, clean):
        await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        await ScheduleService.update_schedule(
            pool,
            clean["device_id"],
            ScheduleUpdate.model_validate(
                {"schedule": {"monday": {"workHours": {"start": "07:00", "end": "16:00"}}}}
            ),
        )
        assert (await counts(pool))["outbox"] == 2

    async def test_delete_enqueues_and_removes_in_one_transaction(self, pool, clean):
        await ScheduleService.create_schedule(pool, create_data(clean["device_id"]))
        await ScheduleService.delete_schedule(pool, clean["device_id"], shift_type="day")
        assert await counts(pool) == {"schedules": 0, "outbox": 2}

    async def test_replayed_event_is_absorbed_by_the_dedupe_key(self, pool, clean):
        """ON CONFLICT DO NOTHING: a repeated dedupe_key must not fail the write."""
        from app.models.notification_event import OutboxEvent
        from app.repositories.notification_outbox import notification_outbox_crud

        event = OutboxEvent(
            type_key="shift_change",
            company_id=clean["company_id"],
            dedupe_key="shift_change:1:day:created:1",
            payload={"action": "created"},
        )
        async with pool.acquire() as conn, conn.transaction():
            await notification_outbox_crud.insert(conn, event)
            await notification_outbox_crud.insert(conn, event)
        assert (await counts(pool))["outbox"] == 1
