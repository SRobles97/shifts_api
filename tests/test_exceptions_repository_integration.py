"""
ExceptionsCRUD against a real database (device_schedule_exceptions and the
device_schedule_blocks / device_schedule_days functions).

Skipped unless SHIFTS_TEST_DSN is set. Every row written here belongs to a
company created per test and is deleted by id afterwards; nothing truncates.
"""

import json
import os
import uuid
from datetime import date
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from app.repositories.exceptions import exceptions_crud
from app.schemas.day_changes import ChangeSpec
from tests.integration_support import make_row_count_tripwire

DSN = os.getenv("SHIFTS_TEST_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SHIFTS_TEST_DSN not set"),
]

DAY = date(2031, 3, 10)  # a Monday, far from any real data

_no_foreign_rows_harmed = make_row_count_tripwire(
    DSN,
    (
        "companies",
        "devices",
        "device_schedules",
        "device_schedule_exceptions",
        "schedule_change_log",
    ),
)


@pytest.fixture(autouse=True)
def stub_device_ref():
    yield None


@pytest.fixture(autouse=True)
def stub_mirror_check():
    yield None


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    await p.close()


async def _make_device(conn, company_id, key, name):
    return await conn.fetchval(
        """
        INSERT INTO devices (company_id, device_key, display_name, timezone, is_active)
        VALUES ($1, $2, $3, 'America/Santiago', true) RETURNING id;
        """,
        company_id,
        key,
        name,
    )


@pytest.fixture
async def env(pool):
    tag = uuid.uuid4().hex[:12]
    async with pool.acquire() as conn:
        company_id = await conn.fetchval(
            "INSERT INTO companies (name, slug) VALUES ($1, $2) RETURNING id;",
            f"Test SA {tag}",
            f"test-sa-{tag}",
        )
        d1 = await _make_device(conn, company_id, "F1", "Fresadora 1")
        d2 = await _make_device(conn, company_id, "F2", "Fresadora 2")
    try:
        yield {"company_id": company_id, "d1": d1, "d2": d2}
    finally:
        async with pool.acquire() as conn:
            ids = [d1, d2]
            # triggers log the delete itself, so the change log goes last
            await conn.execute("DELETE FROM device_schedules WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM device_schedule_exceptions WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM schedule_change_log WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM devices WHERE id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM companies WHERE id = $1;", company_id)


def _custom(start="09:00", end="13:00", reason=""):
    return ChangeSpec.model_validate(
        {"kind": "custom_hours", "workHours": {"start": start, "end": end},
         "breaks": [{"start": "11:00", "durationMinutes": 30}], "reason": reason}
    )


async def test_upsert_inserts_then_replaces(pool, env):
    d = env["d1"]
    op1, op2 = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            id1 = await exceptions_crud.upsert(conn, d, "day", DAY, _custom(), op1)
        async with conn.transaction():
            id2 = await exceptions_crud.upsert(
                conn, d, "day", DAY,
                ChangeSpec(kind="extra_hours", minutes=60, position="after"), op2,
            )
    assert id1 == id2
    rows = await exceptions_crud.list(pool, [d], DAY, DAY)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "extra_hours"
    assert row["operation_id"] == op2
    assert row["source"] == "ui"
    assert row["extra_minutes"] == 60 and row["extra_position"] == "after"
    assert row["work_hours"] is None and row["breaks"] is None


async def test_upsert_writes_camelcase_jsonb(pool, env):
    async with pool.acquire() as conn:
        async with conn.transaction():
            eid = await exceptions_crud.upsert(conn, env["d1"], "day", DAY, _custom(), uuid.uuid4())
    row = await exceptions_crud.get(pool, eid)
    assert json.loads(row["work_hours"]) == {"start": "09:00", "end": "13:00"}
    assert json.loads(row["breaks"]) == [{"start": "11:00", "durationMinutes": 30}]


async def test_blocks_reflect_upsert_inside_transaction(pool, env):
    d = env["d1"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await exceptions_crud.upsert(conn, d, "day", DAY, _custom(), uuid.uuid4())
            inside = await exceptions_crud.blocks(conn, [d], DAY, DAY)
            days = await exceptions_crud.days(conn, [d], DAY, DAY)
            assert all(r["device_id"] == d for r in inside)
            tz = ZoneInfo("America/Santiago")
            spans = sorted((r["block_start"].astimezone(tz), r["block_end"].astimezone(tz)) for r in inside)
            assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in spans] == [
                ("09:00", "11:00"),
                ("11:30", "13:00"),
            ]
            assert [r["shift_date"] for r in days] == [DAY]
    # the pool form works as well, and the committed data is visible
    after = await exceptions_crud.blocks(pool, [d], DAY, DAY)
    assert len(after) == len(inside)


async def test_get_many_delete_and_exceptions_from(pool, env):
    d1, d2 = env["d1"], env["d2"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await exceptions_crud.upsert(conn, d1, "day", DAY, _custom(), uuid.uuid4())
            found = await exceptions_crud.get_many(conn, [d1, d2], "day", DAY)
            assert set(found) == {d1}
            later = await exceptions_crud.exceptions_from(conn, [d1, d2], DAY)
            assert [r["device_id"] for r in later] == [d1]
            assert await exceptions_crud.exceptions_from(conn, [d1], date(2031, 3, 11)) == []
            deleted = await exceptions_crud.delete(conn, found[d1]["id"])
            assert deleted["device_id"] == d1
            assert await exceptions_crud.delete(conn, found[d1]["id"]) is None
    assert await exceptions_crud.get(pool, deleted["id"]) is None


async def test_strip_jsonb_key_removes_only_that_date(pool, env):
    d1, d2 = env["d1"], env["d2"]
    special = json.dumps({
        "2031-03-10": {"workHours": {"start": "08:00", "end": "12:00"}},
        "2031-03-11": {"workHours": {"start": "08:00", "end": "12:00"}},
    })
    async with pool.acquire() as conn:
        for dev in (d1, d2):
            await conn.execute(
                """
                INSERT INTO device_schedules (device_id, shift_type, day_schedules, special_days, valid_from)
                VALUES ($1, 'day', '{}'::jsonb, $2::jsonb, '2031-01-01');
                """,
                dev,
                special,
            )
        async with conn.transaction():
            touched = await exceptions_crud.strip_jsonb_key(conn, d1, DAY)
        assert touched == 1
        r1 = await conn.fetchval("SELECT special_days FROM device_schedules WHERE device_id = $1;", d1)
        r2 = await conn.fetchval("SELECT special_days FROM device_schedules WHERE device_id = $1;", d2)
    assert set(json.loads(r1)) == {"2031-03-11"}
    assert set(json.loads(r2)) == {"2031-03-10", "2031-03-11"}


async def test_mirrored_ids(pool, env):
    d1, d2 = env["d1"], env["d2"]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO device_schedules (device_id, shift_type, day_schedules, valid_from, source)
            VALUES ($1, 'day', '{}'::jsonb, '2031-01-01', 'pilot_mirror');
            """,
            d1,
        )
        await conn.execute(
            """
            INSERT INTO device_schedules (device_id, shift_type, day_schedules, valid_from, source)
            VALUES ($1, 'day', '{}'::jsonb, '2031-01-01', 'ui');
            """,
            d2,
        )
        assert await exceptions_crud.mirrored_ids(conn, [d1, d2]) == {d1}
    assert await exceptions_crud.mirrored_ids(pool, [d1, d2]) == {d1}
    assert await exceptions_crud.mirrored_ids(pool, []) == set()


async def test_device_refs_one_query(pool, env):
    refs = await exceptions_crud.device_refs(pool, [env["d1"], env["d2"], -1])
    assert set(refs) == {env["d1"], env["d2"]}
    assert refs[env["d1"]].company_id == env["company_id"]
    assert refs[env["d1"]].label == "Fresadora 1"
    assert refs[env["d2"]].key == "F2"


async def test_upsert_extra_hours_then_no_work_resets_extra_fields(pool, env):
    d = env["d1"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await exceptions_crud.upsert(
                conn, d, "day", DAY,
                ChangeSpec(kind="extra_hours", minutes=60, position="after"), uuid.uuid4(),
            )
        async with conn.transaction():
            await exceptions_crud.upsert(conn, d, "day", DAY, ChangeSpec(kind="no_work"), uuid.uuid4())
    rows = await exceptions_crud.list(pool, [d], DAY, DAY)
    assert len(rows) == 1
    assert rows[0]["kind"] == "no_work"
    assert rows[0]["extra_minutes"] is None and rows[0]["extra_position"] is None


async def test_strip_jsonb_key_keeps_recurring_entry(pool, env):
    d1 = env["d1"]
    special = json.dumps({
        DAY.isoformat(): {"workHours": None, "isRecurring": True, "recurrencePattern": "yearly"},
    })
    async with pool.acquire() as conn:
        for shift in ("day", "night"):
            await conn.execute(
                """
                INSERT INTO device_schedules (device_id, shift_type, day_schedules, special_days, valid_from)
                VALUES ($1, $2, '{}'::jsonb, $3::jsonb, '2031-01-01');
                """,
                d1,
                shift,
                special,
            )
        async with conn.transaction():
            touched = await exceptions_crud.strip_jsonb_key(conn, d1, DAY)
        rows = await conn.fetch("SELECT special_days FROM device_schedules WHERE device_id = $1;", d1)
    assert touched == 0
    assert all(set(json.loads(r["special_days"])) == {DAY.isoformat()} for r in rows)
