"""Tests for the day-change / bulk-schedule request and response schemas."""

import pytest
from pydantic import ValidationError

from app.schemas.day_changes import (
    BulkScheduleRequest,
    ChangeSpec,
    DayChangeRequest,
    DayChangeResponse,
)
from app.schemas.schedule import day_shift_overnight_message


def _custom(**over):
    data = {"kind": "custom_hours", "work_hours": {"start": "10:00", "end": "14:00"}}
    data.update(over)
    return data


def test_extra_hours_requires_minutes_and_position():
    with pytest.raises(ValidationError):
        ChangeSpec(kind="extra_hours")
    with pytest.raises(ValidationError):
        ChangeSpec(kind="extra_hours", minutes=60)
    with pytest.raises(ValidationError):
        ChangeSpec(kind="extra_hours", position="after")
    spec = ChangeSpec(kind="extra_hours", minutes=60, position="after")
    assert spec.minutes == 60


def test_extra_hours_rejects_work_hours_and_breaks():
    with pytest.raises(ValidationError):
        ChangeSpec(
            kind="extra_hours", minutes=60, position="after",
            work_hours={"start": "08:00", "end": "09:00"},
        )


@pytest.mark.parametrize("minutes,ok", [(14, False), (15, True), (720, True), (721, False)])
def test_minutes_bounds_15_720(minutes, ok):
    if ok:
        assert ChangeSpec(kind="extra_hours", minutes=minutes, position="before").minutes == minutes
    else:
        with pytest.raises(ValidationError):
            ChangeSpec(kind="extra_hours", minutes=minutes, position="before")


def test_custom_hours_requires_work_hours():
    with pytest.raises(ValidationError):
        ChangeSpec(kind="custom_hours")
    with pytest.raises(ValidationError):
        ChangeSpec(kind="custom_hours", minutes=30, position="after",
                   work_hours={"start": "10:00", "end": "14:00"})
    assert ChangeSpec(**_custom()).work_hours.start == "10:00"


def test_no_work_rejects_hours_and_minutes():
    assert ChangeSpec(kind="no_work").kind == "no_work"
    with pytest.raises(ValidationError):
        ChangeSpec(kind="no_work", minutes=30)
    with pytest.raises(ValidationError):
        ChangeSpec(kind="no_work", position="after")
    with pytest.raises(ValidationError):
        ChangeSpec(kind="no_work", work_hours={"start": "10:00", "end": "14:00"})
    with pytest.raises(ValidationError):
        ChangeSpec(kind="no_work", breaks=[{"start": "10:00", "duration_minutes": 15}])


def test_strict_hhmm_rejects_single_digit_hour():
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(work_hours={"start": "8:00", "end": "14:00"}))
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(work_hours={"start": "08:00", "end": "24:00"}))
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(work_hours={"start": "08:60", "end": "14:00"}))


def test_work_hours_end_must_differ_from_start():
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(work_hours={"start": "08:00", "end": "08:00"}))
    # overnight is fine
    ChangeSpec(**_custom(work_hours={"start": "20:00", "end": "02:00"}))


def test_breaks_must_fit_inside_work_hours():
    ChangeSpec(**_custom(breaks=[{"start": "11:00", "duration_minutes": 30}]))
    # starts before
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(breaks=[{"start": "09:30", "duration_minutes": 30}]))
    # ends after
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(breaks=[{"start": "13:45", "duration_minutes": 30}]))
    # overlapping breaks
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(breaks=[
            {"start": "11:00", "duration_minutes": 30},
            {"start": "11:15", "duration_minutes": 30},
        ]))
    # back to back is fine
    ChangeSpec(**_custom(breaks=[
        {"start": "11:00", "duration_minutes": 30},
        {"start": "11:30", "duration_minutes": 30},
    ]))


def test_breaks_inside_overnight_work_hours():
    wh = {"start": "20:00", "end": "02:00"}
    ChangeSpec(**_custom(work_hours=wh, breaks=[{"start": "23:30", "duration_minutes": 60}]))
    ChangeSpec(**_custom(work_hours=wh, breaks=[{"start": "00:30", "duration_minutes": 30}]))
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(work_hours=wh, breaks=[{"start": "03:00", "duration_minutes": 30}]))


def test_break_duration_bounds():
    for bad in (4, 481):
        with pytest.raises(ValidationError):
            ChangeSpec(**_custom(
                work_hours={"start": "06:00", "end": "23:00"},
                breaks=[{"start": "10:00", "duration_minutes": bad}],
            ))


def test_reason_max_200():
    ChangeSpec(kind="no_work", reason="x" * 200)
    with pytest.raises(ValidationError):
        ChangeSpec(kind="no_work", reason="x" * 201)


def test_device_ids_non_empty_unique_max_200():
    base = {"date": "2026-11-02", "change": {"kind": "no_work"}}
    with pytest.raises(ValidationError):
        DayChangeRequest(device_ids=[], **base)
    with pytest.raises(ValidationError):
        DayChangeRequest(device_ids=[1, 2, 1], **base)
    with pytest.raises(ValidationError):
        DayChangeRequest(device_ids=list(range(1, 202)), **base)
    assert len(DayChangeRequest(device_ids=list(range(1, 201)), **base).device_ids) == 200
    assert DayChangeRequest(device_ids=[1], **base).shift_type == "day"


def test_camel_case_aliases_round_trip():
    req = DayChangeRequest.model_validate({
        "date": "2026-11-02",
        "deviceIds": [3, 4],
        "shiftType": "night",
        "change": {
            "kind": "custom_hours",
            "workHours": {"start": "20:00", "end": "02:00"},
            "breaks": [{"start": "23:00", "durationMinutes": 30}],
        },
    })
    assert req.device_ids == [3, 4]
    assert req.shift_type == "night"
    assert req.change.work_hours.end == "02:00"
    assert req.change.breaks[0].duration_minutes == 30
    out = req.model_dump(by_alias=True, mode="json")
    assert out["deviceIds"] == [3, 4]
    assert out["shiftType"] == "night"
    assert out["change"]["workHours"] == {"start": "20:00", "end": "02:00"}
    assert out["change"]["breaks"] == [{"start": "23:00", "durationMinutes": 30}]
    assert "device_ids" not in out and "work_hours" not in out["change"]


def test_change_spec_dump_is_snake_case():
    spec = ChangeSpec.model_validate({
        "kind": "custom_hours",
        "workHours": {"start": "10:00", "end": "14:00"},
        "breaks": [{"start": "11:00", "durationMinutes": 15}],
    })
    assert spec.model_dump(mode="json") == {
        "kind": "custom_hours",
        "minutes": None,
        "position": None,
        "work_hours": {"start": "10:00", "end": "14:00"},
        "breaks": [{"start": "11:00", "duration_minutes": 15}],
        "reason": "",
    }


def test_response_serializes_camel_case_and_none_operation():
    resp = DayChangeResponse(
        operation_id=None, date="2026-11-02",
        applied=[{"device_id": 1, "label": "M1", "shift_type": "day",
                  "regular": "08:00–17:00", "result": "Sin trabajo", "replaced": None}],
        skipped=[{"device_id": 2, "label": "M2", "shift_type": "day",
                  "regular": None, "result": None, "replaced": None, "skipped": "pilot"}],
    )
    out = resp.model_dump(by_alias=True, mode="json")
    assert out["operationId"] is None
    assert out["applied"][0]["deviceId"] == 1
    assert out["skipped"][0]["skipped"] == "pilot"


def _bulk(**over):
    data = {
        "deviceIds": [1],
        "schedule": {"monday": {"workHours": {"start": "08:00", "end": "17:00"}}},
        "validFrom": "2026-11-02",
    }
    data.update(over)
    return data


def test_bulk_request_reuses_day_schema_and_aliases():
    req = BulkScheduleRequest.model_validate(_bulk(
        extraHours={"monday": [{"start": "17:00", "end": "18:00"}]}))
    assert req.shift_type == "day"
    assert req.schedule["monday"].work_hours.start == "08:00"
    assert req.extra_hours["monday"][0].end == "18:00"
    assert BulkScheduleRequest.model_validate(_bulk()).extra_hours is None


def test_bulk_request_device_ids_rules():
    with pytest.raises(ValidationError):
        BulkScheduleRequest.model_validate(_bulk(deviceIds=[]))
    with pytest.raises(ValidationError):
        BulkScheduleRequest.model_validate(_bulk(deviceIds=[1, 1]))


@pytest.mark.parametrize("bad", ["08:00\n", "08:00 ", " 08:00", "\uff10\uff18:\uff10\uff10"])
def test_hhmm_rejects_trailing_whitespace(bad):
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(work_hours={"start": bad, "end": "14:00"}))
    with pytest.raises(ValidationError):
        ChangeSpec(**_custom(breaks=[{"start": bad, "duration_minutes": 15}]))


def test_exception_read_accepts_legacy_values():
    from app.schemas.day_changes import ExceptionRead

    row = ExceptionRead.model_validate({
        "id": 1, "device_id": 2, "shift_type": "day", "date": "2026-11-02",
        "kind": "custom_hours",
        "work_hours": {"start": "8:00", "end": "8:00"},
        "breaks": [{"start": "9:5", "duration_minutes": 1}],
        "extra_minutes": None, "extra_position": None, "reason": "", "source": "migrated",
    })
    out = row.model_dump(by_alias=True, mode="json")
    assert out["workHours"] == {"start": "8:00", "end": "8:00"}
    assert out["breaks"] == [{"start": "9:5", "durationMinutes": 1}]


def test_exception_read_accepts_spec_instances():
    from app.schemas.day_changes import BreakSpec, ExceptionRead, HoursSpec

    row = ExceptionRead(
        id=1, device_id=2, shift_type="day", date="2026-11-02", kind="custom_hours",
        work_hours=HoursSpec(start="10:00", end="14:00"),
        breaks=[BreakSpec(start="11:00", duration_minutes=15)],
        extra_minutes=None, extra_position=None, reason="", source="api",
    )
    assert row.work_hours.start == "10:00"
    assert row.breaks[0].duration_minutes == 15


# --- day shift custom hours may not cross midnight ---------------------------


def _day_change(shift_type, start, end):
    return DayChangeRequest.model_validate({
        "date": "2026-10-23",
        "deviceIds": [1],
        "shiftType": shift_type,
        "change": {"kind": "custom_hours", "workHours": {"start": start, "end": end}},
    })


def test_day_shift_custom_hours_crossing_midnight_rejected():
    with pytest.raises(ValidationError) as exc:
        _day_change("day", "22:00", "06:00")
    # same wording the schedule writers use for a day shift
    assert day_shift_overnight_message("workHours", "22:00", "06:00") in str(exc.value)


def test_night_shift_custom_hours_may_cross_midnight():
    req = _day_change("night", "22:00", "06:00")
    assert req.change.work_hours.end == "06:00"


def test_day_shift_custom_hours_within_day_ok():
    assert _day_change("day", "08:00", "17:00").change.work_hours.start == "08:00"


def test_schedule_writers_use_the_same_overnight_message():
    from app.schemas.schedule import DayScheduleSchema
    from app.services.schedule_service import _assert_day_shift_within_day

    day = DayScheduleSchema.model_validate({"workHours": {"start": "22:00", "end": "06:00"}})
    with pytest.raises(ValueError) as exc:
        _assert_day_shift_within_day("day", {"monday": day}, None)
    assert str(exc.value) == day_shift_overnight_message("workHours for monday", "22:00", "06:00")
