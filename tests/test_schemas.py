"""
Unit tests for app.schemas.schedule API serialization schemas.

Validates camelCase ↔ snake_case alias mapping, field validation,
and model_dump(by_alias=True) output.
"""

import pytest
from pydantic import ValidationError

from app.schemas.schedule import (
    BreakSchema,
    DayScheduleSchema,
    ExtraHourSchema,
    MetadataSchema,
    ScheduleCreate,
    SchedulePatch,
    ScheduleRead,
    ScheduleStatsSchema,
    ScheduleUpdate,
    SpecialDaySchema,
    WorkHoursSchema,
)


# ==================== WorkHoursSchema ====================


class TestWorkHoursSchema:
    def test_basic(self):
        wh = WorkHoursSchema(start="08:00", end="17:00")
        assert wh.start == "08:00"


# ==================== BreakSchema ====================


class TestBreakSchema:
    def test_camel_alias(self):
        b = BreakSchema.model_validate({"start": "12:00", "durationMinutes": 60})
        assert b.duration_minutes == 60

    def test_snake_alias(self):
        b = BreakSchema.model_validate({"start": "12:00", "duration_minutes": 60})
        assert b.duration_minutes == 60

    def test_serialization_alias(self):
        b = BreakSchema(start="12:00", duration_minutes=60)
        out = b.model_dump(by_alias=True)
        assert "durationMinutes" in out


# ==================== DayScheduleSchema ====================


class TestDayScheduleSchema:
    def test_camel_input(self):
        ds = DayScheduleSchema.model_validate(
            {
                "workHours": {"start": "08:00", "end": "17:00"},
                "break": {"start": "12:00", "durationMinutes": 60},
            }
        )
        assert ds.work_hours.start == "08:00"
        assert ds.break_time.duration_minutes == 60

    def test_snake_input(self):
        ds = DayScheduleSchema.model_validate(
            {
                "work_hours": {"start": "08:00", "end": "17:00"},
                "break_time": {"start": "12:00", "duration_minutes": 60},
            }
        )
        assert ds.work_hours.end == "17:00"

    def test_serialization(self):
        ds = DayScheduleSchema(
            work_hours=WorkHoursSchema(start="08:00", end="17:00"),
            break_time=BreakSchema(start="12:00", duration_minutes=60),
        )
        out = ds.model_dump(by_alias=True)
        assert "workHours" in out
        assert "break" in out
        assert out["break"]["durationMinutes"] == 60


# ==================== SpecialDaySchema ====================


class TestSpecialDaySchema:
    def test_camel_aliases(self):
        sd = SpecialDaySchema.model_validate(
            {
                "name": "Navidad",
                "type": "holiday",
                "isRecurring": True,
                "recurrencePattern": "yearly",
            }
        )
        assert sd.is_recurring is True
        assert sd.recurrence_pattern == "yearly"

    def test_serialization(self):
        sd = SpecialDaySchema(
            name="Navidad",
            type="holiday",
            is_recurring=True,
            recurrence_pattern="yearly",
        )
        out = sd.model_dump(by_alias=True)
        assert out["isRecurring"] is True
        assert out["recurrencePattern"] == "yearly"


# ==================== ScheduleCreate ====================


class TestScheduleCreate:
    def test_valid_camel(self):
        sc = ScheduleCreate.model_validate(
            {
                "deviceId": 5,
                "schedule": {
                    "monday": {
                        "workHours": {"start": "08:00", "end": "17:00"},
                        "break": {"start": "12:00", "durationMinutes": 60},
                    }
                },
            }
        )
        assert sc.device_id == 5

    def test_invalid_device_id(self):
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(
                {
                    "deviceId": -1,
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "break": {"start": "12:00", "durationMinutes": 60},
                        }
                    },
                }
            )

    def test_extra_hours_camel(self):
        sc = ScheduleCreate.model_validate(
            {
                "deviceId": 1,
                "schedule": {
                    "monday": {
                        "workHours": {"start": "08:00", "end": "17:00"},
                        "break": {"start": "12:00", "durationMinutes": 60},
                    }
                },
                "extraHours": {"monday": [{"start": "18:00", "end": "20:00"}]},
            }
        )
        assert sc.extra_hours is not None
        assert len(sc.extra_hours["monday"]) == 1


# ==================== ScheduleUpdate ====================


class TestScheduleUpdate:
    def test_no_device_id(self):
        su = ScheduleUpdate.model_validate(
            {
                "schedule": {
                    "monday": {
                        "workHours": {"start": "09:00", "end": "18:00"},
                        "break": {"start": "13:00", "durationMinutes": 45},
                    }
                }
            }
        )
        assert "monday" in su.schedule


# ==================== SchedulePatch ====================


class TestSchedulePatch:
    def test_all_optional(self):
        sp = SchedulePatch.model_validate({})
        assert sp.schedule is None
        assert sp.extra_hours is None
        assert sp.metadata is None

    def test_partial_metadata(self):
        sp = SchedulePatch.model_validate({"metadata": {"version": "2.0"}})
        assert sp.metadata.version == "2.0"
        assert sp.metadata.source == "ui"  # default


# ==================== ScheduleRead ====================


class TestScheduleRead:
    def test_serialization(self):
        sr = ScheduleRead(
            id="1",
            device_id=5,
            schedule={
                "monday": DayScheduleSchema(
                    work_hours=WorkHoursSchema(start="08:00", end="17:00"),
                    break_time=BreakSchema(start="12:00", duration_minutes=60),
                )
            },
            metadata=MetadataSchema(version="1.0", source="ui"),
        )
        out = sr.model_dump(by_alias=True)
        assert out["deviceId"] == 5
        assert "monday" in out["schedule"]
        assert out["metadata"]["source"] == "ui"


# ==================== MetadataSchema ====================


class TestMetadataSchema:
    def test_defaults(self):
        m = MetadataSchema()
        assert m.version == "1.0"
        assert m.source == "ui"
        assert m.created_at is None


# ==================== ScheduleStatsSchema ====================


class TestScheduleStatsSchema:
    def test_serialization(self):
        ss = ScheduleStatsSchema(
            device_id=1,
            schedule_start="08:00",
            schedule_end="17:00",
            current_time="10:30",
            hours_used=2.5,
            total_work_hours=8.0,
            usage_percentage=31.25,
        )
        out = ss.model_dump(by_alias=True)
        assert out["deviceId"] == 1
        assert out["scheduleStart"] == "08:00"
        assert out["usagePercentage"] == 31.25
