"""
Bulk regular schedule ("Editar turno habitual" for several machines) against a
real database.

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
from app.repositories.exceptions import exceptions_crud
from app.schemas.day_changes import BulkScheduleRequest, ChangeSpec
from app.services.bulk_schedule_service import apply_bulk, preview_bulk
from tests.integration_support import make_row_count_tripwire

DSN = os.getenv("SHIFTS_TEST_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SHIFTS_TEST_DSN not set"),
]

VALID_FROM = date(2026, 11, 3)
ACTOR = Actor(email="m@x.cl", name="Mia")
OLD_DAYS = {"monday": ("08:00", "17:00"), "tuesday": ("08:00", "17:00")}
NEW_SCHEDULE = {
    "monday": {"workHours": {"start": "07:00", "end": "16:00"}},
    "tuesday": {"workHours": {"start": "08:00", "end": "17:00"}},
    "wednesday": {
        "workHours": {"start": "08:00", "end": "17:00"},
        "breaks": [{"start": "12:00", "durationMinutes": 60}],
    },
}

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
    names = [("F1", "Fresadora 1"), ("F2", "Fresadora 2"), ("F3", "Fresadora 3")]
    async with pool.acquire() as conn:
        company_id = await conn.fetchval(
            "INSERT INTO companies (name, slug) VALUES ($1, $2) RETURNING id;",
            f"Test SA {tag}",
            f"test-sa-{tag}",
        )
        ids = [await _make_device(conn, company_id, k, n) for k, n in names]
    refs = {
        i: DeviceRef(id=i, company_id=company_id, key=k, display_name=n)
        for i, (k, n) in zip(ids, names)
    }
    try:
        yield {"company_id": company_id, "d1": ids[0], "d2": ids[1], "d3": ids[2], "refs": refs}
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM notification_outbox WHERE company_id = $1;", company_id)
            # triggers log the delete itself, so the change log goes last
            await conn.execute("DELETE FROM device_schedules WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM device_schedule_exceptions WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM schedule_change_log WHERE device_id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM devices WHERE id = ANY($1::bigint[]);", ids)
            await conn.execute("DELETE FROM companies WHERE id = $1;", company_id)


async def _schedule(
    pool,
    device_id,
    days=OLD_DAYS,
    valid_from=date(2026, 9, 1),
    valid_to=None,
    source="ui",
    special_days=None,
    extra_hours=None,
    shift_type="day",
):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO device_schedules
                (device_id, shift_type, day_schedules, special_days, extra_hours,
                 valid_from, valid_to, source)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7, $8)
            RETURNING id;
            """,
            device_id,
            shift_type,
            json.dumps({d: {"workHours": {"start": s, "end": e}} for d, (s, e) in days.items()}),
            json.dumps(special_days) if special_days is not None else None,
            json.dumps(extra_hours) if extra_hours is not None else None,
            valid_from,
            valid_to,
            source,
        )


async def _rows(pool, device_id):
    return await pool.fetch(
        """
        SELECT id, device_id, shift_type, day_schedules, extra_hours, special_days,
               valid_from, valid_to, version, source, updated_at
        FROM device_schedules WHERE device_id = $1 ORDER BY valid_from;
        """,
        device_id,
    )


async def _outbox(pool, company_id):
    return await pool.fetch("SELECT * FROM notification_outbox WHERE company_id = $1;", company_id)


def _req(ids, extra_hours=None, valid_from=VALID_FROM, schedule=NEW_SCHEDULE):
    body = {
        "deviceIds": ids,
        "shiftType": "day",
        "validFrom": valid_from.isoformat(),
        "schedule": schedule,
    }
    if extra_hours is not None:
        body["extraHours"] = extra_hours
    return BulkScheduleRequest.model_validate(body)


def _jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


async def test_bulk_splits_open_vigencia(pool, env):
    d1 = env["d1"]
    old_id = await _schedule(pool, d1)

    resp = await apply_bulk(pool, _req([d1]), env["refs"], ACTOR)

    rows = await _rows(pool, d1)
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        (date(2026, 9, 1), date(2026, 11, 2)),
        (VALID_FROM, None),
    ]
    assert rows[0]["id"] == old_id
    assert _jsonb(rows[0]["day_schedules"])["monday"]["workHours"] == {"start": "08:00", "end": "17:00"}
    new = rows[1]
    assert new["source"] == "ui"
    stored = _jsonb(new["day_schedules"])
    assert stored["monday"] == {"workHours": {"start": "07:00", "end": "16:00"}}
    assert stored["wednesday"]["breaks"] == [{"start": "12:00", "durationMinutes": 60}]

    assert resp.operation_id is not None
    [result] = resp.applied
    assert result.device_id == d1 and result.label == "Fresadora 1"
    assert result.new_vigencia_kind == "split"
    assert result.new_valid_to is None
    assert [d["day"] for d in result.days] == ["monday", "wednesday"]
    assert result.days[0] == {
        "day": "monday",
        "before": "08:00–17:00",
        "after": "07:00–16:00",
        "change": "modified",
    }
    assert result.days[1]["change"] == "added"


async def test_bulk_same_valid_from_updates_in_place(pool, env):
    d1 = env["d1"]
    old_id = await _schedule(pool, d1, valid_from=VALID_FROM, valid_to=date(2026, 12, 31))

    resp = await apply_bulk(pool, _req([d1]), env["refs"], ACTOR)

    rows = await _rows(pool, d1)
    assert len(rows) == 1
    assert rows[0]["id"] == old_id
    assert (rows[0]["valid_from"], rows[0]["valid_to"]) == (VALID_FROM, date(2026, 12, 31))
    assert _jsonb(rows[0]["day_schedules"])["monday"]["workHours"]["start"] == "07:00"
    assert resp.applied[0].new_vigencia_kind == "split"
    assert resp.applied[0].new_valid_to == date(2026, 12, 31)


async def test_bulk_inherits_finite_valid_to(pool, env):
    d1 = env["d1"]
    await _schedule(pool, d1, valid_to=date(2026, 12, 31))

    resp = await apply_bulk(pool, _req([d1]), env["refs"], ACTOR)

    rows = await _rows(pool, d1)
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        (date(2026, 9, 1), date(2026, 11, 2)),
        (VALID_FROM, date(2026, 12, 31)),
    ]
    assert resp.applied[0].new_vigencia_kind == "split"
    assert resp.applied[0].new_valid_to == date(2026, 12, 31)


async def test_bulk_stops_before_future_vigencia(pool, env):
    d1, d2 = env["d1"], env["d2"]
    # d1: nothing covers the date, a vigencia starts later
    future_id = await _schedule(pool, d1, valid_from=date(2026, 12, 1))
    # d2: a cover that ends, a gap, then a later vigencia
    await _schedule(pool, d2, valid_to=date(2026, 11, 15))
    later_id = await _schedule(pool, d2, valid_from=date(2026, 12, 1))

    resp = await apply_bulk(pool, _req([d1, d2]), env["refs"], ACTOR)

    rows1 = await _rows(pool, d1)
    assert [(r["valid_from"], r["valid_to"]) for r in rows1] == [
        (VALID_FROM, date(2026, 11, 30)),
        (date(2026, 12, 1), None),
    ]
    assert rows1[1]["id"] == future_id
    assert _jsonb(rows1[1]["day_schedules"])["monday"]["workHours"]["start"] == "08:00"

    rows2 = await _rows(pool, d2)
    assert [(r["valid_from"], r["valid_to"]) for r in rows2] == [
        (date(2026, 9, 1), date(2026, 11, 2)),
        (VALID_FROM, date(2026, 11, 15)),
        (date(2026, 12, 1), None),
    ]
    assert rows2[2]["id"] == later_id

    by_id = {r.device_id: r for r in resp.applied}
    assert by_id[d1].new_vigencia_kind == "future_bounded"
    assert by_id[d1].new_valid_to == date(2026, 11, 30)
    assert by_id[d2].new_vigencia_kind == "split"
    assert by_id[d2].new_valid_to == date(2026, 11, 15)


async def test_bulk_new_coverage_when_none(pool, env):
    d1 = env["d1"]

    resp = await apply_bulk(pool, _req([d1]), env["refs"], ACTOR)

    rows = await _rows(pool, d1)
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [(VALID_FROM, None)]
    assert rows[0]["special_days"] is None and rows[0]["extra_hours"] is None
    [result] = resp.applied
    assert result.new_vigencia_kind == "new_coverage"
    assert result.new_valid_to is None
    assert [d["change"] for d in result.days] == ["added", "added", "added"]


async def test_bulk_carries_recurring_holidays_and_extras_unless_replaced(pool, env):
    d1, d2 = env["d1"], env["d2"]
    holidays = {"12-25": {"name": "Navidad", "type": "holiday", "workHours": None}}
    extras = {"friday": [{"start": "17:00", "end": "19:00"}]}
    await _schedule(pool, d1, special_days=holidays, extra_hours=extras)
    await _schedule(pool, d2, special_days=holidays, extra_hours=extras)
    new_extras = {"monday": [{"start": "16:00", "end": "18:00"}]}

    await apply_bulk(pool, _req([d1]), env["refs"], ACTOR)
    await apply_bulk(pool, _req([d2], extra_hours=new_extras), env["refs"], ACTOR)

    kept = (await _rows(pool, d1))[1]
    assert _jsonb(kept["special_days"]) == holidays
    assert _jsonb(kept["extra_hours"]) == extras

    replaced = (await _rows(pool, d2))[1]
    assert _jsonb(replaced["special_days"]) == holidays
    assert _jsonb(replaced["extra_hours"]) == new_extras
    # the shrunk row keeps its own configuration
    old = (await _rows(pool, d2))[0]
    assert _jsonb(old["extra_hours"]) == extras


async def test_bulk_reports_kept_exceptions(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1)
    await _schedule(pool, d2)
    op = uuid.uuid4()
    async with pool.acquire() as conn:
        no_work = ChangeSpec(kind="no_work")
        extra = ChangeSpec(kind="extra_hours", minutes=120, position="after")
        # before valid_from: not reported
        await exceptions_crud.upsert(conn, d1, "day", date(2026, 10, 30), no_work, op)
        await exceptions_crud.upsert(conn, d1, "day", date(2026, 11, 6), no_work, op)
        await exceptions_crud.upsert(conn, d2, "day", VALID_FROM, extra, op)
        # another shift: the edit does not concern it
        await exceptions_crud.upsert(conn, d2, "night", date(2026, 11, 4), no_work, op)

    resp = await apply_bulk(pool, _req([d1, d2]), env["refs"], ACTOR)

    by_id = {r.device_id: r for r in resp.applied}
    assert by_id[d1].kept_exceptions == [
        {"device_label": "Fresadora 1", "date": "2026-11-06", "summary": "Sin trabajo"}
    ]
    assert by_id[d2].kept_exceptions == [
        {"device_label": "Fresadora 2", "date": "2026-11-03", "summary": "+2 h extras después"}
    ]
    [row] = await _outbox(pool, env["company_id"])
    payload = json.loads(row["payload"])
    assert payload["kept_exceptions"] == [
        {"device_label": "Fresadora 1", "date": "2026-11-06", "summary": "Sin trabajo"},
        {"device_label": "Fresadora 2", "date": "2026-11-03", "summary": "+2 h extras después"},
    ]
    # exceptions are left in place
    assert await pool.fetchval(
        "SELECT count(*) FROM device_schedule_exceptions WHERE device_id = ANY($1::bigint[]);",
        [d1, d2],
    ) == 4


async def test_bulk_one_event_three_devices(pool, env):
    d1, d2, d3 = env["d1"], env["d2"], env["d3"]
    await _schedule(pool, d1)
    await _schedule(pool, d2, valid_to=date(2026, 12, 31))

    resp = await apply_bulk(pool, _req([d1, d2, d3]), env["refs"], ACTOR)

    assert [r.device_id for r in resp.applied] == [d1, d2, d3]
    rows = await _outbox(pool, env["company_id"])
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == f"shift_change:{resp.operation_id}"
    assert rows[0]["type_key"] == "shift_change"
    payload = json.loads(rows[0]["payload"])
    assert payload["version"] == 2
    assert payload["operation"] == "recurring_change"
    assert payload["operation_id"] == str(resp.operation_id)
    assert payload["valid_from"] == "2026-11-03"
    assert payload["actor"] == {"email": "m@x.cl", "name": "Mia"}
    assert payload["skipped"] == [] and payload["kept_exceptions"] == []
    assert [d["id"] for d in payload["devices"]] == [d1, d2, d3]
    dev1 = payload["devices"][0]
    assert dev1 == {
        "id": d1,
        "label": "Fresadora 1",
        "shift_type": "day",
        "regular": None,
        "result": None,
        "replaced": None,
        "days": resp.applied[0].days,
        "validity_shortened": None,
    }
    assert dev1["days"][0]["day"] == "monday"
    assert [d["change"] for d in payload["devices"][2]["days"]] == ["added", "added", "added"]


async def test_bulk_preview_leaves_no_rows(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1)
    await _schedule(pool, d2, valid_from=date(2026, 12, 1))
    before = [dict(r) for r in await _rows(pool, d1)] + [dict(r) for r in await _rows(pool, d2)]

    resp = await preview_bulk(pool, _req([d1, d2]), env["refs"])

    after = [dict(r) for r in await _rows(pool, d1)] + [dict(r) for r in await _rows(pool, d2)]
    assert after == before
    assert await _outbox(pool, env["company_id"]) == []
    assert resp.operation_id is None
    by_id = {r.device_id: r for r in resp.applied}
    assert by_id[d1].new_vigencia_kind == "split"
    assert by_id[d1].days[0]["day"] == "monday"
    assert by_id[d2].new_vigencia_kind == "future_bounded"
    assert by_id[d2].new_valid_to == date(2026, 11, 30)


async def test_bulk_pilot_skipped(pool, env):
    d1, d2 = env["d1"], env["d2"]
    await _schedule(pool, d1)
    pilot_id = await _schedule(pool, d2, source="pilot_mirror")

    resp = await apply_bulk(pool, _req([d1, d2]), env["refs"], ACTOR)

    assert [r.device_id for r in resp.applied] == [d1]
    [skipped] = resp.skipped
    assert skipped.device_id == d2 and skipped.skipped == "pilot"
    assert skipped.label == "Fresadora 2"
    rows2 = await _rows(pool, d2)
    assert [(r["id"], r["valid_from"], r["valid_to"]) for r in rows2] == [
        (pilot_id, date(2026, 9, 1), None)
    ]
    [row] = await _outbox(pool, env["company_id"])
    payload = json.loads(row["payload"])
    assert payload["skipped"] == [{"id": d2, "label": "Fresadora 2", "reason": "pilot"}]
    assert [d["id"] for d in payload["devices"]] == [d1]


async def test_bulk_all_pilots_writes_nothing(pool, env):
    d1 = env["d1"]
    await _schedule(pool, d1, source="pilot_mirror")

    resp = await apply_bulk(pool, _req([d1]), env["refs"], ACTOR)

    assert resp.operation_id is None and resp.applied == []
    assert [r.skipped for r in resp.skipped] == ["pilot"]
    assert len(await _rows(pool, d1)) == 1
    assert await _outbox(pool, env["company_id"]) == []


async def test_bulk_rejects_day_shift_crossing_midnight(pool, env):
    d1 = env["d1"]
    bad = {"monday": {"workHours": {"start": "22:00", "end": "06:00"}}}
    with pytest.raises(ValueError):
        await apply_bulk(pool, _req([d1], schedule=bad), env["refs"], ACTOR)
    assert await _rows(pool, d1) == []
