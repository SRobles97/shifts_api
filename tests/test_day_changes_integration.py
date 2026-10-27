"""
"Cambiar un día" (preview and apply) against a real database.

Skipped unless SHIFTS_TEST_DSN is set. Every row written here belongs to a
company created per test and is deleted by id afterwards; nothing truncates.
"""

import json
import os
import uuid
from datetime import date

import asyncpg
import pytest

from app.models.notification_event import Actor, DeviceRef
from app.schemas.day_changes import DayChangeRequest
from app.services.day_change_service import apply_day_change, preview_day_change
from tests.integration_support import make_row_count_tripwire

DSN = os.getenv("SHIFTS_TEST_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SHIFTS_TEST_DSN not set"),
]

FRIDAY = date(2026, 10, 23)
SATURDAY = date(2026, 10, 24)
ACTOR = Actor(email="m@x.cl", name="Mia")

_no_foreign_rows_harmed = make_row_count_tripwire(
    DSN,
    (
        "companies",
        "devices",
        "device_schedules",
        "device_schedule_exceptions",
        "notification_outbox",
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
    refs = {
        d1: DeviceRef(id=d1, company_id=company_id, key="F1", display_name="Fresadora 1"),
        d2: DeviceRef(id=d2, company_id=company_id, key="F2", display_name="Fresadora 2"),
    }
    try:
        yield {"company_id": company_id, "d1": d1, "d2": d2, "refs": refs}
    finally:
        async with pool.acquire() as conn:
            ids = [d1, d2]
            await conn.execute("DELETE FROM notification_outbox WHERE company_id = $1;", company_id)
            # triggers log the delete itself, so the change log goes last
            await conn.execute("DELETE FROM device_schedules WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM device_schedule_exceptions WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM schedule_change_log WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM devices WHERE id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM companies WHERE id = $1;", company_id)


async def _schedule(pool, device_id, days, shift_type="day", source="ui", special_days=None):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO device_schedules
                (device_id, shift_type, day_schedules, special_days, valid_from, source)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, '2026-01-01', $5);
            """,
            device_id,
            shift_type,
            json.dumps({d: {"workHours": {"start": s, "end": e}} for d, (s, e) in days.items()}),
            json.dumps(special_days) if special_days is not None else None,
            source,
        )


def _req(ids, change, day=FRIDAY, shift_type="day"):
    return DayChangeRequest.model_validate(
        {"date": day.isoformat(), "deviceIds": ids, "shiftType": shift_type, "change": change}
    )


EXTRA_AFTER = {"kind": "extra_hours", "minutes": 120, "position": "after"}


async def _exceptions(pool, device_id):
    return await pool.fetch(
        "SELECT * FROM device_schedule_exceptions WHERE device_id = $1;", device_id
    )


async def _outbox(pool, company_id):
    return await pool.fetch(
        "SELECT * FROM notification_outbox WHERE company_id = $1;", company_id
    )


async def test_apply_extra_after_two_machines_one_event(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1, {"friday": ("08:00", "17:00")})
    await _schedule(pool, d2, {"friday": ("07:00", "16:00")})
    req = _req([d1, d2], EXTRA_AFTER)

    resp = await apply_day_change(pool, req, env["refs"], ACTOR)

    assert resp.operation_id is not None
    assert resp.skipped == []
    by_id = {r.device_id: r for r in resp.applied}
    assert by_id[d1].regular == "08:00–17:00"
    assert by_id[d1].result == "08:00–17:00 + 17:00–19:00"
    assert by_id[d2].result == "07:00–16:00 + 16:00–18:00"
    assert by_id[d1].label == "Fresadora 1" and by_id[d1].replaced is None

    rows = await _outbox(pool, env["company_id"])
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["operation"] == "day_change"
    assert payload["operation_id"] == str(resp.operation_id)
    assert rows[0]["dedupe_key"] == f"shift_change:{resp.operation_id}"
    assert len(payload["devices"]) == 2
    assert payload["change"] == req.change.model_dump(mode="json")
    assert "work_hours" in payload["change"] and "workHours" not in payload["change"]
    dev1 = next(d for d in payload["devices"] if d["id"] == d1)
    assert dev1 == {
        "id": d1,
        "label": "Fresadora 1",
        "shift_type": "day",
        "regular": "08:00–17:00",
        "result": "08:00–17:00 + 17:00–19:00",
        "replaced": None,
        "days": [],
        "validity_shortened": None,
    }
    assert payload["actor"] == {"email": "m@x.cl", "name": "Mia"}
    assert len(await _exceptions(pool, d1)) == 1
    assert len(await _exceptions(pool, d2)) == 1


async def test_machine_without_shift_that_day_is_skipped(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1, {"friday": ("08:00", "17:00"), "saturday": ("08:00", "13:00")})
    await _schedule(pool, d2, {"friday": ("08:00", "17:00")})

    resp = await apply_day_change(pool, _req([d1, d2], EXTRA_AFTER, day=SATURDAY), env["refs"], ACTOR)

    assert [r.device_id for r in resp.applied] == [d1]
    assert [(r.device_id, r.skipped) for r in resp.skipped] == [(d2, "no_shift_that_day")]
    assert await _exceptions(pool, d2) == []
    assert len(await _exceptions(pool, d1)) == 1
    payload = json.loads((await _outbox(pool, env["company_id"]))[0]["payload"])
    assert payload["skipped"] == [{"id": d2, "label": "Fresadora 2", "reason": "no_shift_that_day"}]


async def test_extra_before_overlapping_night_shift_is_skipped(pool, env):
    d1 = env["d1"]
    await _schedule(pool, d1, {"thursday": ("22:00", "06:00")}, shift_type="night")
    await _schedule(pool, d1, {"friday": ("06:30", "15:00")}, shift_type="day")
    change = {"kind": "extra_hours", "minutes": 120, "position": "before"}

    resp = await apply_day_change(pool, _req([d1], change), env["refs"], ACTOR)

    assert resp.applied == []
    assert [(r.device_id, r.skipped) for r in resp.skipped] == [(d1, "overlaps_other_shift")]
    assert await _exceptions(pool, d1) == []


async def test_custom_hours_overlapping_night_shift_is_skipped(pool, env):
    d1 = env["d1"]
    await _schedule(pool, d1, {"thursday": ("22:00", "06:00")}, shift_type="night")
    await _schedule(pool, d1, {"friday": ("06:30", "15:00")}, shift_type="day")
    change = {"kind": "custom_hours", "workHours": {"start": "05:00", "end": "12:00"}}

    resp = await apply_day_change(pool, _req([d1], change), env["refs"], ACTOR)

    assert [(r.device_id, r.skipped) for r in resp.skipped] == [(d1, "overlaps_other_shift")]
    assert await _exceptions(pool, d1) == []


async def test_pilot_is_skipped_not_error(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1, {"friday": ("08:00", "17:00")})
    await _schedule(pool, d2, {"friday": ("08:00", "17:00")}, source="pilot_mirror")

    resp = await apply_day_change(pool, _req([d1, d2], EXTRA_AFTER), env["refs"], ACTOR)

    assert [r.device_id for r in resp.applied] == [d1]
    assert [(r.device_id, r.skipped) for r in resp.skipped] == [(d2, "pilot")]
    assert resp.skipped[0].regular == "08:00–17:00"
    assert await _exceptions(pool, d2) == []


async def test_replace_reports_replaced_summary(pool, env):
    d1 = env["d1"]
    await _schedule(pool, d1, {"friday": ("08:00", "17:00")})
    await apply_day_change(pool, _req([d1], EXTRA_AFTER), env["refs"], ACTOR)

    change = {"kind": "custom_hours", "workHours": {"start": "09:00", "end": "13:00"}}
    resp = await apply_day_change(pool, _req([d1], change), env["refs"], ACTOR)

    [res] = resp.applied
    assert res.replaced == "+2 h extras después"
    # regular is the day without any per-date exception, not the replaced one
    assert res.regular == "08:00–17:00"
    assert res.result == "09:00–13:00"
    rows = await _exceptions(pool, d1)
    assert len(rows) == 1 and rows[0]["kind"] == "custom_hours"
    assert rows[0]["operation_id"] == resp.operation_id
    payload = json.loads(
        (await pool.fetchrow(
            "SELECT payload FROM notification_outbox WHERE dedupe_key = $1;",
            f"shift_change:{resp.operation_id}",
        ))["payload"]
    )
    assert payload["devices"][0]["replaced"] == "+2 h extras después"
    assert payload["change"]["work_hours"] == {"start": "09:00", "end": "13:00"}


async def test_replacing_migrated_strips_jsonb_key(pool, env):
    d1 = env["d1"]
    special = {
        FRIDAY.isoformat(): {"name": "Sin trabajo", "type": "holiday", "workHours": None},
        "2026-10-30": {"name": "Sin trabajo", "type": "holiday", "workHours": None},
    }
    await _schedule(pool, d1, {"friday": ("08:00", "17:00")}, special_days=special)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO device_schedule_exceptions
                (device_id, shift_type, date, kind, operation_id, source)
            VALUES ($1, 'day', $2, 'no_work', $3, 'migrated');
            """,
            d1,
            FRIDAY,
            uuid.uuid4(),
        )

    resp = await apply_day_change(pool, _req([d1], EXTRA_AFTER), env["refs"], ACTOR)

    [res] = resp.applied
    assert res.replaced == "Sin trabajo"
    keys = await pool.fetchval(
        "SELECT special_days FROM device_schedules WHERE device_id = $1;", d1
    )
    assert set(json.loads(keys)) == {"2026-10-30"}
    [row] = await _exceptions(pool, d1)
    assert row["source"] == "ui" and row["kind"] == "extra_hours"


async def _scoped_counts(pool, env):
    ids = [env["d1"], env["d2"]]
    return {
        "exceptions": await pool.fetchval(
            "SELECT count(*) FROM device_schedule_exceptions WHERE device_id = ANY($1::bigint[]);", ids
        ),
        "outbox": await pool.fetchval(
            "SELECT count(*) FROM notification_outbox WHERE company_id = $1;", env["company_id"]
        ),
        "change_log": await pool.fetchval(
            "SELECT count(*) FROM schedule_change_log WHERE device_id = ANY($1::bigint[]);", ids
        ),
    }


async def test_preview_matches_apply_and_leaves_no_rows(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1, {"friday": ("08:00", "17:00")})
    await _schedule(pool, d2, {"monday": ("08:00", "17:00")})
    req = _req([d1, d2], EXTRA_AFTER)
    before = await _scoped_counts(pool, env)

    preview = await preview_day_change(pool, req, env["refs"])

    assert await _scoped_counts(pool, env) == before
    assert preview.operation_id is None
    applied = await apply_day_change(pool, req, env["refs"], ACTOR)
    assert applied.operation_id is not None
    assert preview.applied == applied.applied
    assert preview.skipped == applied.skipped
    assert [r.device_id for r in preview.applied] == [d1]
    assert [(r.device_id, r.skipped) for r in preview.skipped] == [(d2, "no_shift_that_day")]


async def test_all_skipped_writes_nothing(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1, {"monday": ("08:00", "17:00")})
    await _schedule(pool, d2, {"friday": ("08:00", "17:00")}, source="pilot_mirror")
    before = await _scoped_counts(pool, env)

    resp = await apply_day_change(pool, _req([d1, d2], EXTRA_AFTER), env["refs"], ACTOR)

    assert resp.applied == []
    assert resp.operation_id is None
    assert {(r.device_id, r.skipped) for r in resp.skipped} == {
        (d1, "no_shift_that_day"),
        (d2, "pilot"),
    }
    assert await _scoped_counts(pool, env) == before


async def test_no_work_result_says_sin_trabajo(pool, env):
    d1 = env["d1"]
    await _schedule(pool, d1, {"friday": ("08:00", "17:00")})

    resp = await apply_day_change(pool, _req([d1], {"kind": "no_work"}), env["refs"], ACTOR)

    [res] = resp.applied
    assert res.regular == "08:00–17:00"
    assert res.result == "Sin trabajo"
