"""Day-change helpers and routes, with the database and service mocked."""

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.core import authz as authz_module
from app.models.notification_event import DeviceRef
from app.schemas.day_changes import DayChangeDeviceResult, DayChangeResponse
from app.services.day_change_service import describe_exception, format_blocks

SVC = "app.routers.schedules.day_change_service"
TZ = "America/Santiago"


def _utc(h, m=0, day=23):
    # America/Santiago is UTC-3 in October 2026 (DST)
    return datetime(2026, 10, day, h + 3, m, tzinfo=timezone.utc)


def _block(start, end, kind="regular", shift_type="day", shift_date=date(2026, 10, 23)):
    return {
        "shift_type": shift_type,
        "shift_date": shift_date,
        "block_start": start,
        "block_end": end,
        "kind": kind,
    }


# --- describe_exception ---------------------------------------------------


def test_describe_extra_whole_hours():
    row = {"kind": "extra_hours", "extra_minutes": 120, "extra_position": "after"}
    assert describe_exception(row) == "+2 h extras después"


def test_describe_extra_one_hour_before():
    row = {"kind": "extra_hours", "extra_minutes": 60, "extra_position": "before"}
    assert describe_exception(row) == "+1 h extra antes"


def test_describe_extra_minutes():
    row = {"kind": "extra_hours", "extra_minutes": 90, "extra_position": "after"}
    assert describe_exception(row) == "+90 min extra después"


def test_describe_custom_hours_from_jsonb_text():
    row = {"kind": "custom_hours", "work_hours": json.dumps({"start": "09:00", "end": "13:00"})}
    assert describe_exception(row) == "09:00–13:00"


def test_describe_custom_hours_from_dict():
    row = {"kind": "custom_hours", "work_hours": {"start": "22:00", "end": "06:00"}}
    assert describe_exception(row) == "22:00–06:00"


def test_describe_no_work():
    assert describe_exception({"kind": "no_work"}) == "Sin trabajo"


def test_describe_none():
    assert describe_exception(None) is None


# --- format_blocks --------------------------------------------------------


def test_format_regular_and_extra():
    rows = [
        _block(_utc(17), _utc(19), kind="extra"),
        _block(_utc(8), _utc(17)),
    ]
    assert format_blocks(rows, TZ) == "08:00–17:00 + 17:00–19:00"


def test_format_regular_pieces_joined_with_slash():
    rows = [_block(_utc(8), _utc(12)), _block(_utc(13), _utc(17))]
    assert format_blocks(rows, TZ) == "08:00–12:00 / 13:00–17:00"


def test_format_extra_before_is_chronological():
    rows = [_block(_utc(6, 30), _utc(15)), _block(_utc(4, 30), _utc(6, 30), kind="extra")]
    assert format_blocks(rows, TZ) == "04:30–06:30 + 06:30–15:00"


def test_format_mixed_separators_in_start_order():
    rows = [
        _block(_utc(13), _utc(17)),
        _block(_utc(17), _utc(19), kind="extra"),
        _block(_utc(6), _utc(8), kind="extra"),
        _block(_utc(8), _utc(12)),
    ]
    assert format_blocks(rows, TZ) == "06:00–08:00 + 08:00–12:00 / 13:00–17:00 + 17:00–19:00"


def test_format_consecutive_extras_use_plus():
    rows = [
        _block(_utc(8), _utc(17)),
        _block(_utc(17), _utc(18), kind="extra"),
        _block(_utc(19), _utc(20), kind="extra"),
    ]
    assert format_blocks(rows, TZ) == "08:00–17:00 + 17:00–18:00 + 19:00–20:00"


def test_format_empty_is_none():
    assert format_blocks([], TZ) is None


# --- routes ---------------------------------------------------------------


BODY = {
    "date": "2026-10-23",
    "deviceIds": [1, 2],
    "change": {"kind": "extra_hours", "minutes": 60, "position": "after"},
}
REFS = {
    1: DeviceRef(id=1, company_id=3, key="F1", display_name="Fresadora 1"),
    2: DeviceRef(id=2, company_id=3, key="F2", display_name="Fresadora 2"),
}


def _response(op=None):
    return DayChangeResponse(
        operation_id=op,
        date=date(2026, 10, 23),
        applied=[
            DayChangeDeviceResult(
                device_id=1, label="Fresadora 1", shift_type="day",
                regular="08:00–17:00", result="08:00–17:00 + 17:00–18:00",
            )
        ],
        skipped=[],
    )


async def test_routes_declared_before_device_id(client):
    op = uuid4()
    with patch.object(
        authz_module.exceptions_crud, "device_refs", AsyncMock(return_value=REFS)
    ), patch(f"{SVC}.apply_day_change", new_callable=AsyncMock, return_value=_response(op)) as apply_, patch(
        f"{SVC}.preview_day_change", new_callable=AsyncMock, return_value=_response()
    ) as preview:
        r_apply = await client.post("/schedules/day-changes", json=BODY)
        r_preview = await client.post("/schedules/day-changes/preview", json=BODY)

    assert r_apply.status_code == 200, r_apply.text
    assert r_apply.json()["operationId"] == str(op)
    assert r_apply.json()["applied"][0]["deviceId"] == 1
    assert r_preview.status_code == 200, r_preview.text
    assert r_preview.json()["operationId"] is None
    apply_.assert_awaited_once()
    preview.assert_awaited_once()
    args = apply_.await_args
    assert args.args[2] == REFS
    assert args.args[3].email == "test@x.cl"


async def test_mixed_companies_400(client):
    refs = {
        1: DeviceRef(id=1, company_id=3, key="F1"),
        2: DeviceRef(id=2, company_id=4, key="F2"),
    }
    with patch.object(
        authz_module.exceptions_crud, "device_refs", AsyncMock(return_value=refs)
    ), patch(f"{SVC}.apply_day_change", new_callable=AsyncMock) as apply_:
        resp = await client.post("/schedules/day-changes", json=BODY)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Todas las máquinas deben ser de la misma empresa"
    apply_.assert_not_awaited()


async def test_service_value_error_is_400(client):
    with patch.object(
        authz_module.exceptions_crud, "device_refs", AsyncMock(return_value=REFS)
    ), patch(f"{SVC}.apply_day_change", new_callable=AsyncMock, side_effect=ValueError("malo")):
        resp = await client.post("/schedules/day-changes", json=BODY)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "malo"


async def test_invalid_body_is_422(client):
    resp = await client.post("/schedules/day-changes", json={**BODY, "deviceIds": []})
    assert resp.status_code == 422


# --- list / remove routes ----------------------------------------------------


async def test_get_exceptions_route_parses_query(client):
    from app.schemas.day_changes import ExceptionRead

    row = ExceptionRead(
        id=7, device_id=1, shift_type="day", date=date(2026, 10, 23), kind="no_work", source="ui"
    )
    with patch(f"{SVC}.list_exceptions", new_callable=AsyncMock, return_value=[row]) as lst:
        resp = await client.get(
            "/schedules/exceptions", params={"deviceIds": "1,2", "from": "2026-10-01", "to": "2026-10-31"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["deviceId"] == 1 and resp.json()[0]["shiftType"] == "day"
    assert lst.await_args.args[1:] == ([1, 2], date(2026, 10, 1), date(2026, 10, 31))


async def test_get_exceptions_range_over_400_days_400(client):
    resp = await client.get(
        "/schedules/exceptions", params={"deviceIds": "1", "from": "2026-01-01", "to": "2027-03-01"}
    )
    assert resp.status_code == 400


async def test_get_exceptions_bad_ids_400(client):
    resp = await client.get(
        "/schedules/exceptions", params={"deviceIds": "a,b", "from": "2026-01-01", "to": "2026-01-02"}
    )
    assert resp.status_code == 400


async def test_delete_exception_route_not_shadowed_by_device_id(client):
    with patch(f"{SVC}.remove_exception", new_callable=AsyncMock) as rm:
        resp = await client.delete("/schedules/exceptions/5")
    assert resp.status_code == 200, resp.text
    rm.assert_awaited_once()
    assert rm.await_args.args[1] == 5


async def test_delete_exception_error_mapping(client):
    from app.services.schedule_service import MirroredScheduleError

    with patch(f"{SVC}.remove_exception", new_callable=AsyncMock, side_effect=LookupError("no")):
        assert (await client.delete("/schedules/exceptions/5")).status_code == 404
    with patch(f"{SVC}.remove_exception", new_callable=AsyncMock, side_effect=MirroredScheduleError("piloto")):
        assert (await client.delete("/schedules/exceptions/5")).status_code == 409
