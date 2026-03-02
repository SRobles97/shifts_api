"""
Unit tests for app.schemas.schedule API serialization schemas.

Validates camelCase ↔ snake_case alias mapping, field validation,
and model_dump(by_alias=True) output.
"""

from datetime import date

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
    def test_camel_input_breaks_list(self):
        ds = DayScheduleSchema.model_validate(
            {
                "workHours": {"start": "08:00", "end": "17:00"},
                "breaks": [{"start": "12:00", "durationMinutes": 60}],
            }
        )
        assert ds.work_hours.start == "08:00"
        assert len(ds.breaks) == 1
        assert ds.breaks[0].duration_minutes == 60

    def test_legacy_single_break_object(self):
        """Legacy format: 'break' key with a single object gets wrapped into a list."""
        ds = DayScheduleSchema.model_validate(
            {
                "workHours": {"start": "08:00", "end": "17:00"},
                "break": {"start": "12:00", "durationMinutes": 60},
            }
        )
        assert ds.breaks is not None
        assert len(ds.breaks) == 1
        assert ds.breaks[0].duration_minutes == 60

    def test_snake_input(self):
        ds = DayScheduleSchema.model_validate(
            {
                "work_hours": {"start": "08:00", "end": "17:00"},
                "break_time": [{"start": "12:00", "duration_minutes": 60}],
            }
        )
        assert ds.work_hours.end == "17:00"
        assert len(ds.breaks) == 1

    def test_multiple_breaks(self):
        ds = DayScheduleSchema.model_validate(
            {
                "workHours": {"start": "08:00", "end": "17:00"},
                "breaks": [
                    {"start": "10:00", "durationMinutes": 15},
                    {"start": "12:00", "durationMinutes": 60},
                ],
            }
        )
        assert len(ds.breaks) == 2

    def test_serialization(self):
        ds = DayScheduleSchema(
            work_hours=WorkHoursSchema(start="08:00", end="17:00"),
            breaks=[BreakSchema(start="12:00", duration_minutes=60)],
        )
        out = ds.model_dump(by_alias=True)
        assert "workHours" in out
        assert "breaks" in out
        assert len(out["breaks"]) == 1
        assert out["breaks"][0]["durationMinutes"] == 60

    def test_null_break(self):
        ds = DayScheduleSchema.model_validate(
            {"workHours": {"start": "08:00", "end": "17:00"}}
        )
        assert ds.breaks is None

    def test_null_break_serialization(self):
        ds = DayScheduleSchema(
            work_hours=WorkHoursSchema(start="08:00", end="17:00"),
        )
        out = ds.model_dump(by_alias=True)
        assert out["breaks"] is None


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

    def test_legacy_single_break(self):
        """Legacy format: 'break' key with single object gets wrapped."""
        sd = SpecialDaySchema.model_validate(
            {
                "name": "Medio día",
                "type": "special_event",
                "workHours": {"start": "08:00", "end": "13:00"},
                "break": {"start": "11:00", "durationMinutes": 30},
            }
        )
        assert sd.breaks is not None
        assert len(sd.breaks) == 1

    def test_breaks_list(self):
        sd = SpecialDaySchema.model_validate(
            {
                "name": "Training",
                "type": "training",
                "workHours": {"start": "08:00", "end": "17:00"},
                "breaks": [
                    {"start": "10:00", "durationMinutes": 15},
                    {"start": "12:00", "durationMinutes": 60},
                ],
            }
        )
        assert len(sd.breaks) == 2


# ==================== ScheduleCreate ====================


class TestScheduleCreate:
    def test_valid_camel(self):
        sc = ScheduleCreate.model_validate(
            {
                "deviceId": 5,
                "schedule": {
                    "monday": {
                        "workHours": {"start": "08:00", "end": "17:00"},
                        "breaks": [{"start": "12:00", "durationMinutes": 60}],
                    }
                },
                "validFrom": "2025-01-01",
            }
        )
        assert sc.device_id == 5
        assert sc.valid_from == date(2025, 1, 1)
        assert sc.valid_to is None

    def test_with_valid_to(self):
        sc = ScheduleCreate.model_validate(
            {
                "deviceId": 5,
                "schedule": {
                    "monday": {
                        "workHours": {"start": "08:00", "end": "17:00"},
                        "breaks": [{"start": "12:00", "durationMinutes": 60}],
                    }
                },
                "validFrom": "2025-01-01",
                "validTo": "2025-06-30",
            }
        )
        assert sc.valid_to == date(2025, 6, 30)

    def test_missing_valid_from(self):
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(
                {
                    "deviceId": 5,
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "breaks": [{"start": "12:00", "durationMinutes": 60}],
                        }
                    },
                }
            )

    def test_invalid_device_id(self):
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(
                {
                    "deviceId": -1,
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "breaks": [{"start": "12:00", "durationMinutes": 60}],
                        }
                    },
                    "validFrom": "2025-01-01",
                }
            )

    def test_extra_hours_camel(self):
        sc = ScheduleCreate.model_validate(
            {
                "deviceId": 1,
                "schedule": {
                    "monday": {
                        "workHours": {"start": "08:00", "end": "17:00"},
                        "breaks": [{"start": "12:00", "durationMinutes": 60}],
                    }
                },
                "extraHours": {"monday": [{"start": "18:00", "end": "20:00"}]},
                "validFrom": "2025-01-01",
            }
        )
        assert sc.extra_hours is not None
        assert len(sc.extra_hours["monday"]) == 1

    def test_legacy_break_format(self):
        """Legacy single-object 'break' should still work in ScheduleCreate."""
        sc = ScheduleCreate.model_validate(
            {
                "deviceId": 1,
                "schedule": {
                    "monday": {
                        "workHours": {"start": "08:00", "end": "17:00"},
                        "break": {"start": "12:00", "durationMinutes": 60},
                    }
                },
                "validFrom": "2025-01-01",
            }
        )
        assert sc.schedule["monday"].breaks is not None
        assert len(sc.schedule["monday"].breaks) == 1


# ==================== ScheduleUpdate ====================


class TestScheduleUpdate:
    def test_no_device_id(self):
        su = ScheduleUpdate.model_validate(
            {
                "schedule": {
                    "monday": {
                        "workHours": {"start": "09:00", "end": "18:00"},
                        "breaks": [{"start": "13:00", "durationMinutes": 45}],
                    }
                }
            }
        )
        assert "monday" in su.schedule
        assert su.valid_from is None
        assert su.valid_to is None

    def test_with_valid_from(self):
        su = ScheduleUpdate.model_validate(
            {
                "schedule": {
                    "monday": {
                        "workHours": {"start": "09:00", "end": "18:00"},
                        "breaks": [{"start": "13:00", "durationMinutes": 45}],
                    }
                },
                "validFrom": "2025-07-01",
            }
        )
        assert su.valid_from == date(2025, 7, 1)


# ==================== SchedulePatch ====================


class TestSchedulePatch:
    def test_all_optional(self):
        sp = SchedulePatch.model_validate({})
        assert sp.schedule is None
        assert sp.extra_hours is None
        assert sp.metadata is None
        assert sp.valid_from is None
        assert sp.valid_to is None

    def test_partial_metadata(self):
        sp = SchedulePatch.model_validate({"metadata": {"version": "2.0"}})
        assert sp.metadata.version == "2.0"
        assert sp.metadata.source == "ui"  # default

    def test_patch_valid_from(self):
        sp = SchedulePatch.model_validate({"validFrom": "2025-03-01"})
        assert sp.valid_from == date(2025, 3, 1)


# ==================== ScheduleRead ====================


class TestScheduleRead:
    def test_serialization(self):
        sr = ScheduleRead(
            id="1",
            device_id=5,
            schedule={
                "monday": DayScheduleSchema(
                    work_hours=WorkHoursSchema(start="08:00", end="17:00"),
                    breaks=[BreakSchema(start="12:00", duration_minutes=60)],
                )
            },
            valid_from=date(2025, 1, 1),
            metadata=MetadataSchema(version="1.0", source="ui"),
        )
        out = sr.model_dump(by_alias=True)
        assert out["deviceId"] == 5
        assert "monday" in out["schedule"]
        assert out["metadata"]["source"] == "ui"
        assert out["validFrom"] == date(2025, 1, 1)
        assert out["validTo"] is None

    def test_serialization_with_valid_to(self):
        sr = ScheduleRead(
            id="1",
            device_id=5,
            schedule={
                "monday": DayScheduleSchema(
                    work_hours=WorkHoursSchema(start="08:00", end="17:00"),
                    breaks=[BreakSchema(start="12:00", duration_minutes=60)],
                )
            },
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 6, 30),
            metadata=MetadataSchema(version="1.0", source="ui"),
        )
        out = sr.model_dump(by_alias=True)
        assert out["validTo"] == date(2025, 6, 30)


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
