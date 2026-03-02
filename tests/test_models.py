"""
Unit tests for app.models.schedule business logic models.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.models.schedule import (
    Break,
    DaySchedule,
    ExtraHour,
    RecurrencePattern,
    Schedule,
    ScheduleEntity,
    SpecialDay,
    SpecialDayType,
    WorkHours,
)


# ==================== WorkHours ====================


class TestWorkHours:
    def test_valid(self):
        wh = WorkHours(start="08:00", end="17:00")
        assert wh.start == "08:00"
        assert wh.end == "17:00"

    def test_duration_minutes(self):
        wh = WorkHours(start="08:00", end="17:00")
        assert wh.duration_minutes() == 540

    def test_invalid_format(self):
        with pytest.raises(ValidationError):
            WorkHours(start="8am", end="17:00")

    def test_end_before_start(self):
        with pytest.raises(ValidationError):
            WorkHours(start="17:00", end="08:00")

    def test_end_equal_start(self):
        with pytest.raises(ValidationError):
            WorkHours(start="12:00", end="12:00")


# ==================== Break ====================


class TestBreak:
    def test_valid(self):
        b = Break(start="12:00", duration_minutes=60)
        assert b.end_time() == "13:00"

    def test_duration_too_short(self):
        with pytest.raises(ValidationError):
            Break(start="12:00", duration_minutes=2)

    def test_duration_too_long(self):
        with pytest.raises(ValidationError):
            Break(start="12:00", duration_minutes=500)

    def test_invalid_time_format(self):
        with pytest.raises(ValidationError):
            Break(start="noon", duration_minutes=30)


# ==================== DaySchedule ====================


class TestDaySchedule:
    def test_valid_single_break(self):
        ds = DaySchedule(
            work_hours=WorkHours(start="08:00", end="17:00"),
            breaks=[Break(start="12:00", duration_minutes=60)],
        )
        assert ds.total_work_minutes() == 480

    def test_multiple_breaks(self):
        ds = DaySchedule(
            work_hours=WorkHours(start="08:00", end="17:00"),
            breaks=[
                Break(start="10:00", duration_minutes=15),
                Break(start="12:00", duration_minutes=60),
                Break(start="15:00", duration_minutes=15),
            ],
        )
        # 540 - 15 - 60 - 15 = 450
        assert ds.total_work_minutes() == 450

    def test_break_outside_work_hours(self):
        with pytest.raises(ValidationError):
            DaySchedule(
                work_hours=WorkHours(start="08:00", end="12:00"),
                breaks=[Break(start="13:00", duration_minutes=30)],
            )

    def test_break_ends_after_work(self):
        with pytest.raises(ValidationError):
            DaySchedule(
                work_hours=WorkHours(start="08:00", end="12:00"),
                breaks=[Break(start="11:00", duration_minutes=120)],
            )

    def test_overlapping_breaks(self):
        with pytest.raises(ValidationError, match="Overlapping breaks"):
            DaySchedule(
                work_hours=WorkHours(start="08:00", end="17:00"),
                breaks=[
                    Break(start="12:00", duration_minutes=60),
                    Break(start="12:30", duration_minutes=30),
                ],
            )

    def test_no_break(self):
        ds = DaySchedule(
            work_hours=WorkHours(start="08:00", end="17:00"),
        )
        assert ds.breaks is None
        assert ds.total_work_minutes() == 540

    def test_explicit_null_break(self):
        ds = DaySchedule(
            work_hours=WorkHours(start="08:00", end="17:00"),
            breaks=None,
        )
        assert ds.breaks is None
        assert ds.total_work_minutes() == 540


# ==================== ExtraHour ====================


class TestExtraHour:
    def test_valid(self):
        eh = ExtraHour(start="18:00", end="20:00")
        assert eh.duration_minutes() == 120

    def test_end_before_start(self):
        with pytest.raises(ValidationError):
            ExtraHour(start="20:00", end="18:00")


# ==================== SpecialDay ====================


class TestSpecialDay:
    def test_holiday_no_work(self):
        sd = SpecialDay(name="Navidad", type=SpecialDayType.HOLIDAY)
        assert sd.total_work_minutes() == 0

    def test_special_with_work(self):
        sd = SpecialDay(
            name="Medio día",
            type=SpecialDayType.SPECIAL_EVENT,
            work_hours=WorkHours(start="08:00", end="13:00"),
            breaks=[Break(start="11:00", duration_minutes=30)],
        )
        assert sd.total_work_minutes() == 270

    def test_special_with_multiple_breaks(self):
        sd = SpecialDay(
            name="Training day",
            type=SpecialDayType.TRAINING,
            work_hours=WorkHours(start="08:00", end="17:00"),
            breaks=[
                Break(start="10:00", duration_minutes=15),
                Break(start="12:00", duration_minutes=60),
            ],
        )
        # 540 - 15 - 60 = 465
        assert sd.total_work_minutes() == 465

    def test_break_without_work_hours_fails(self):
        with pytest.raises(ValidationError):
            SpecialDay(
                name="Bad",
                type=SpecialDayType.HOLIDAY,
                breaks=[Break(start="12:00", duration_minutes=30)],
            )

    def test_recurring_requires_pattern(self):
        """recurrence_pattern validator fires when value is explicitly provided."""
        with pytest.raises(ValidationError):
            SpecialDay(
                name="Missing pattern",
                type=SpecialDayType.HOLIDAY,
                is_recurring=False,
                recurrence_pattern=RecurrencePattern.YEARLY,
            )

    def test_recurring_valid(self):
        sd = SpecialDay(
            name="Yearly",
            type=SpecialDayType.HOLIDAY,
            is_recurring=True,
            recurrence_pattern=RecurrencePattern.YEARLY,
        )
        assert sd.is_recurring is True


# ==================== Schedule ====================


class TestSchedule:
    def test_valid_single_day(self):
        s = Schedule(
            day_schedules={
                "monday": DaySchedule(
                    work_hours=WorkHours(start="08:00", end="17:00"),
                    breaks=[Break(start="12:00", duration_minutes=60)],
                )
            }
        )
        assert s.active_days == ["monday"]
        assert s.is_work_day("monday") is True
        assert s.is_work_day("sunday") is False

    def test_empty_day_schedules(self):
        with pytest.raises(ValidationError):
            Schedule(day_schedules={})

    def test_invalid_day_name(self):
        with pytest.raises(ValidationError):
            Schedule(
                day_schedules={
                    "notaday": DaySchedule(
                        work_hours=WorkHours(start="08:00", end="17:00"),
                        breaks=[Break(start="12:00", duration_minutes=60)],
                    )
                }
            )

    def test_day_name_normalized(self):
        s = Schedule(
            day_schedules={
                "Monday": DaySchedule(
                    work_hours=WorkHours(start="08:00", end="17:00"),
                    breaks=[Break(start="12:00", duration_minutes=60)],
                )
            }
        )
        assert "monday" in s.day_schedules

    def test_total_work_minutes_specific_day(self):
        s = Schedule(
            day_schedules={
                "monday": DaySchedule(
                    work_hours=WorkHours(start="08:00", end="17:00"),
                    breaks=[Break(start="12:00", duration_minutes=60)],
                )
            }
        )
        assert s.total_work_minutes("monday") == 480
        assert s.total_work_minutes("sunday") == 0


# ==================== ScheduleEntity ====================


class TestScheduleEntity:
    @pytest.fixture
    def base_schedule(self):
        return Schedule(
            day_schedules={
                "monday": DaySchedule(
                    work_hours=WorkHours(start="08:00", end="17:00"),
                    breaks=[Break(start="12:00", duration_minutes=60)],
                ),
                "tuesday": DaySchedule(
                    work_hours=WorkHours(start="08:00", end="17:00"),
                    breaks=[Break(start="12:00", duration_minutes=60)],
                ),
            }
        )

    def test_valid_entity(self, base_schedule):
        entity = ScheduleEntity(device_id=1, schedule=base_schedule, valid_from=date(2025, 1, 1))
        assert entity.device_id == 1
        assert entity.source == "ui"
        assert entity.valid_from == date(2025, 1, 1)
        assert entity.valid_to is None

    def test_invalid_device_id(self, base_schedule):
        with pytest.raises(ValidationError):
            ScheduleEntity(device_id=0, schedule=base_schedule, valid_from=date(2025, 1, 1))
        with pytest.raises(ValidationError):
            ScheduleEntity(device_id=-1, schedule=base_schedule, valid_from=date(2025, 1, 1))

    def test_valid_to_before_valid_from(self, base_schedule):
        with pytest.raises(ValidationError):
            ScheduleEntity(
                device_id=1, schedule=base_schedule,
                valid_from=date(2025, 6, 1), valid_to=date(2025, 1, 1),
            )

    def test_valid_to_equal_valid_from(self, base_schedule):
        entity = ScheduleEntity(
            device_id=1, schedule=base_schedule,
            valid_from=date(2025, 6, 1), valid_to=date(2025, 6, 1),
        )
        assert entity.valid_to == date(2025, 6, 1)

    def test_is_effective_on(self, base_schedule):
        entity = ScheduleEntity(
            device_id=1, schedule=base_schedule,
            valid_from=date(2025, 1, 1), valid_to=date(2025, 6, 30),
        )
        assert entity.is_effective_on(date(2025, 3, 15)) is True
        assert entity.is_effective_on(date(2024, 12, 31)) is False
        assert entity.is_effective_on(date(2025, 7, 1)) is False
        assert entity.is_effective_on(date(2025, 1, 1)) is True
        assert entity.is_effective_on(date(2025, 6, 30)) is True

    def test_is_effective_on_open_ended(self, base_schedule):
        entity = ScheduleEntity(
            device_id=1, schedule=base_schedule, valid_from=date(2025, 1, 1),
        )
        assert entity.is_effective_on(date(2030, 1, 1)) is True
        assert entity.is_effective_on(date(2024, 12, 31)) is False

    def test_extra_hours_on_inactive_day_fails(self, base_schedule):
        with pytest.raises(ValidationError):
            ScheduleEntity(
                device_id=1,
                schedule=base_schedule,
                valid_from=date(2025, 1, 1),
                extra_hours={
                    "sunday": [ExtraHour(start="10:00", end="12:00")]
                },
            )

    def test_extra_hours_overlap_fails(self, base_schedule):
        with pytest.raises(ValidationError):
            ScheduleEntity(
                device_id=1,
                schedule=base_schedule,
                valid_from=date(2025, 1, 1),
                extra_hours={
                    "monday": [
                        ExtraHour(start="18:00", end="20:00"),
                        ExtraHour(start="19:00", end="21:00"),
                    ]
                },
            )

    def test_total_work_minutes_with_extras(self, base_schedule):
        entity = ScheduleEntity(
            device_id=1,
            schedule=base_schedule,
            valid_from=date(2025, 1, 1),
            extra_hours={
                "monday": [ExtraHour(start="18:00", end="20:00")]
            },
        )
        assert entity.get_total_work_minutes_for_day("monday") == 600
        assert entity.get_total_work_minutes_for_day("tuesday") == 480

    def test_weekly_work_minutes(self, base_schedule):
        entity = ScheduleEntity(device_id=1, schedule=base_schedule, valid_from=date(2025, 1, 1))
        assert entity.get_weekly_work_minutes() == 960  # 480 * 2

    def test_special_days_invalid_date_format(self, base_schedule):
        with pytest.raises(ValidationError):
            ScheduleEntity(
                device_id=1,
                schedule=base_schedule,
                valid_from=date(2025, 1, 1),
                special_days={
                    "25-12-2025": SpecialDay(
                        name="Bad format", type=SpecialDayType.HOLIDAY
                    )
                },
            )

    def test_effective_schedule_regular_day(self, base_schedule):
        entity = ScheduleEntity(device_id=1, schedule=base_schedule, valid_from=date(2025, 1, 1))
        # 2025-01-13 is a Monday
        eff = entity.get_effective_schedule_for_date(date(2025, 1, 13))
        assert eff is not None
        assert eff.work_hours.start == "08:00"

    def test_effective_schedule_non_work_day(self, base_schedule):
        entity = ScheduleEntity(device_id=1, schedule=base_schedule, valid_from=date(2025, 1, 1))
        # 2025-01-12 is a Sunday
        eff = entity.get_effective_schedule_for_date(date(2025, 1, 12))
        assert eff is None

    def test_effective_schedule_special_day_no_work(self, base_schedule):
        entity = ScheduleEntity(
            device_id=1,
            schedule=base_schedule,
            valid_from=date(2025, 1, 1),
            special_days={
                "2025-01-13": SpecialDay(
                    name="Feriado",
                    type=SpecialDayType.HOLIDAY,
                )
            },
        )
        eff = entity.get_effective_schedule_for_date(date(2025, 1, 13))
        assert eff is None

    def test_effective_schedule_special_day_with_work(self, base_schedule):
        entity = ScheduleEntity(
            device_id=1,
            schedule=base_schedule,
            valid_from=date(2025, 1, 1),
            special_days={
                "2025-01-13": SpecialDay(
                    name="Medio día",
                    type=SpecialDayType.SPECIAL_EVENT,
                    work_hours=WorkHours(start="08:00", end="13:00"),
                    breaks=[Break(start="11:00", duration_minutes=30)],
                )
            },
        )
        eff = entity.get_effective_schedule_for_date(date(2025, 1, 13))
        assert eff is not None
        assert eff.work_hours.end == "13:00"

    def test_effective_schedule_recurring(self, base_schedule):
        entity = ScheduleEntity(
            device_id=1,
            schedule=base_schedule,
            valid_from=date(2025, 1, 1),
            special_days={
                "2024-12-25": SpecialDay(
                    name="Navidad",
                    type=SpecialDayType.HOLIDAY,
                    is_recurring=True,
                    recurrence_pattern=RecurrencePattern.YEARLY,
                )
            },
        )
        # 2025-12-25 should match the recurring pattern (month-day match)
        eff = entity.get_effective_schedule_for_date(date(2025, 12, 25))
        assert eff is None  # holiday = no work
