"""
Business logic models for schedule management.

These models represent the core business entities and logic,
separate from API serialization concerns.
"""

from pydantic import BaseModel, Field, field_validator
from pydantic_core.core_schema import ValidationInfo
from typing import List, Optional, Dict, ClassVar
from datetime import datetime, timedelta, date as date_type
from enum import Enum
import re


_TIME_RE = re.compile(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_hhmm(value: str) -> datetime:
    """Parse 'HH:MM' to a datetime (today's date)."""
    return datetime.strptime(value, "%H:%M")


class RecurrencePattern(str, Enum):
    """Enumeration for special day recurrence patterns."""
    YEARLY = "yearly"
    NONE = "none"


class SpecialDayType(str, Enum):
    """Enumeration for special day types."""
    HOLIDAY = "holiday"
    MAINTENANCE = "maintenance"
    SPECIAL_EVENT = "special_event"
    CLOSURE = "closure"
    TRAINING = "training"


class WorkHours(BaseModel):
    """
    Business model for work hours.

    Handles validation and business logic for regular work hours.
    """

    start: str = Field(..., description="Start time in HH:MM format")
    end: str = Field(..., description="End time in HH:MM format")

    @field_validator("start", "end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time format is HH:MM."""
        if not _TIME_RE.match(v):
            raise ValueError("Time must be in HH:MM format")
        return v

    @field_validator("end")
    @classmethod
    def end_after_start(cls, v: str, info: ValidationInfo) -> str:
        """Ensure end time is after start time."""
        start = info.data.get("start")
        if start:
            start_time = _parse_hhmm(start).time()
            end_time = _parse_hhmm(v).time()
            if end_time <= start_time:
                raise ValueError("End time must be after start time")
        return v

    def duration_minutes(self) -> int:
        """Calculate work duration in minutes."""
        start_dt = _parse_hhmm(self.start)
        end_dt = _parse_hhmm(self.end)
        return int((end_dt - start_dt).total_seconds() // 60)


class Break(BaseModel):
    """
    Business model for break periods.

    Handles validation and business logic for work breaks.
    """

    start: str = Field(..., description="Break start time in HH:MM format")
    duration_minutes: int = Field(..., description="Break duration in minutes")

    @field_validator("start")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time format is HH:MM."""
        if not _TIME_RE.match(v):
            raise ValueError("Time must be in HH:MM format")
        return v

    @field_validator("duration_minutes")
    @classmethod
    def validate_duration(cls, v: int) -> int:
        """Ensure break duration is reasonable."""
        if v < 5 or v > 480:  # 5 minutes to 8 hours
            raise ValueError("Break duration must be between 5 and 480 minutes")
        return v

    def end_time(self) -> str:
        """Calculate break end time."""
        start_dt = _parse_hhmm(self.start)
        end_dt = start_dt + timedelta(minutes=self.duration_minutes)
        return end_dt.strftime("%H:%M")


class ExtraHour(BaseModel):
    """
    Business model for extra work hours.

    Represents additional work periods beyond regular hours.
    """

    start: str = Field(..., description="Extra hour start time in HH:MM format")
    end: str = Field(..., description="Extra hour end time in HH:MM format")

    @field_validator("start", "end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time format is HH:MM."""
        if not _TIME_RE.match(v):
            raise ValueError("Time must be in HH:MM format")
        return v

    @field_validator("end")
    @classmethod
    def end_after_start(cls, v: str, info: ValidationInfo) -> str:
        """Ensure end time is after start time."""
        start = info.data.get("start")
        if start:
            start_time = _parse_hhmm(start).time()
            end_time = _parse_hhmm(v).time()
            if end_time <= start_time:
                raise ValueError("End time must be after start time")
        return v

    def duration_minutes(self) -> int:
        """Calculate extra hour duration in minutes."""
        start_dt = _parse_hhmm(self.start)
        end_dt = _parse_hhmm(self.end)
        return int((end_dt - start_dt).total_seconds() // 60)


class DaySchedule(BaseModel):
    """
    Business model for a single day's schedule.

    Represents work hours and break configuration for one specific day.
    """

    work_hours: WorkHours = Field(..., description="Work hours for this day")
    breaks: Optional[List[Break]] = Field(None, description="Break configurations for this day")

    @field_validator("breaks")
    @classmethod
    def validate_breaks(cls, v: Optional[List[Break]], info: ValidationInfo) -> Optional[List[Break]]:
        """Ensure all breaks are within work hours and don't overlap."""
        if not v:
            return v
        work_hours: WorkHours = info.data.get("work_hours")
        if work_hours:
            work_start_dt = _parse_hhmm(work_hours.start)
            work_end_dt = _parse_hhmm(work_hours.end)

            for b in v:
                break_start_dt = _parse_hhmm(b.start)
                break_end_dt = break_start_dt + timedelta(minutes=b.duration_minutes)

                if not (work_start_dt.time() <= break_start_dt.time() <= work_end_dt.time()):
                    raise ValueError("Break must start within work hours")
                if break_end_dt > work_end_dt:
                    raise ValueError("Break must end within work hours")

            # Check for overlaps (same logic as extra_hours)
            sorted_breaks = sorted(v, key=lambda b: _parse_hhmm(b.start))
            last_end: Optional[datetime] = None
            for b in sorted_breaks:
                start_dt = _parse_hhmm(b.start)
                end_dt = start_dt + timedelta(minutes=b.duration_minutes)
                if last_end and start_dt < last_end:
                    raise ValueError(f"Overlapping breaks: {b.start}")
                last_end = end_dt

        return v

    def total_work_minutes(self) -> int:
        """Calculate total work minutes excluding breaks."""
        total = self.work_hours.duration_minutes()
        if self.breaks:
            total -= sum(b.duration_minutes for b in self.breaks)
        return total


class SpecialDay(BaseModel):
    """
    Business model for special day configuration.

    Represents special days (holidays, maintenance, etc.) that override
    regular weekday schedules on specific dates.
    """

    name: str = Field(..., min_length=1, max_length=100, description="Name of the special day")
    type: SpecialDayType = Field(..., description="Type of special day")
    work_hours: Optional[WorkHours] = Field(
        None,
        description="Work hours for this special day (None = no work)"
    )
    breaks: Optional[List[Break]] = Field(
        None,
        description="Break configurations for this special day"
    )
    is_recurring: bool = Field(
        default=False,
        description="Whether this special day recurs annually"
    )
    recurrence_pattern: Optional[RecurrencePattern] = Field(
        None,
        description="Recurrence pattern if recurring"
    )

    @field_validator("breaks")
    @classmethod
    def validate_breaks_require_work_hours(cls, v: Optional[List[Break]], info: ValidationInfo) -> Optional[List[Break]]:
        """Breaks require work_hours to be set."""
        work_hours = info.data.get("work_hours")
        if v and not work_hours:
            raise ValueError("Breaks cannot be set without work hours")

        if v and work_hours:
            work_start_dt = _parse_hhmm(work_hours.start)
            work_end_dt = _parse_hhmm(work_hours.end)

            for b in v:
                break_start_dt = _parse_hhmm(b.start)
                break_end_dt = break_start_dt + timedelta(minutes=b.duration_minutes)

                if not (work_start_dt.time() <= break_start_dt.time() <= work_end_dt.time()):
                    raise ValueError("Break must start within work hours")
                if break_end_dt > work_end_dt:
                    raise ValueError("Break must end within work hours")

            # Check for overlaps
            sorted_breaks = sorted(v, key=lambda b: _parse_hhmm(b.start))
            last_end: Optional[datetime] = None
            for b in sorted_breaks:
                start_dt = _parse_hhmm(b.start)
                end_dt = start_dt + timedelta(minutes=b.duration_minutes)
                if last_end and start_dt < last_end:
                    raise ValueError(f"Overlapping breaks: {b.start}")
                last_end = end_dt

        return v

    @field_validator("recurrence_pattern")
    @classmethod
    def validate_recurrence_pattern(cls, v: Optional[RecurrencePattern], info: ValidationInfo) -> Optional[RecurrencePattern]:
        """Validate recurrence pattern consistency with is_recurring."""
        is_recurring = info.data.get("is_recurring")

        if v and v != RecurrencePattern.NONE and not is_recurring:
            raise ValueError("recurrence_pattern requires is_recurring=True")
        if is_recurring and not v:
            raise ValueError("is_recurring=True requires recurrence_pattern to be set")

        return v

    def total_work_minutes(self) -> int:
        """Calculate total work minutes for this special day (excluding breaks)."""
        if not self.work_hours:
            return 0

        work_mins = self.work_hours.duration_minutes()
        if self.breaks:
            work_mins -= sum(b.duration_minutes for b in self.breaks)

        return work_mins


class Schedule(BaseModel):
    """
    Core business model for work schedules.

    Represents the complete schedule configuration with business logic
    for validation and calculations. Each day can have different work hours.
    """

    VALID_DAYS: ClassVar[List[str]] = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]

    day_schedules: Dict[str, DaySchedule] = Field(
        ..., description="Schedule configuration for each active day"
    )

    @field_validator("day_schedules")
    @classmethod
    def validate_day_schedules(cls, v: Dict[str, DaySchedule]) -> Dict[str, DaySchedule]:
        """Validate day schedules have valid day keys and at least one day."""
        if not v:
            raise ValueError("At least one day schedule is required")

        normalized = {}
        for day, schedule in v.items():
            day_lower = day.lower()
            if day_lower not in cls.VALID_DAYS:
                raise ValueError(f"Invalid day: {day}")
            normalized[day_lower] = schedule

        return normalized

    @property
    def active_days(self) -> List[str]:
        """Get list of active work days from day_schedules."""
        return list(self.day_schedules.keys())

    def total_work_minutes(self, day: Optional[str] = None) -> int:
        """Calculate total work minutes excluding break."""
        if day:
            day_lower = day.lower()
            if day_lower in self.day_schedules:
                return self.day_schedules[day_lower].total_work_minutes()
            return 0
        if self.day_schedules:
            first_day = list(self.day_schedules.keys())[0]
            return self.day_schedules[first_day].total_work_minutes()
        return 0

    def is_work_day(self, day: str) -> bool:
        """Check if a given day is a work day."""
        return day.lower() in self.day_schedules


class ScheduleEntity(BaseModel):
    """
    Complete business entity for a device schedule.

    Includes all schedule information plus metadata, extra hours, and special days.
    Supports N schedules per device with valid_from/valid_to date ranges.
    """

    id: Optional[int] = None
    device_id: int = Field(..., description="Device identifier (FK to devices table)")
    schedule: Schedule = Field(..., description="Basic schedule configuration")
    extra_hours: Optional[Dict[str, List[ExtraHour]]] = Field(
        None, description="Extra hours by day of week"
    )
    special_days: Optional[Dict[str, SpecialDay]] = Field(
        None, description="Special day overrides keyed by ISO date (YYYY-MM-DD)"
    )
    valid_from: date_type = Field(..., description="Start date of schedule validity")
    valid_to: Optional[date_type] = Field(None, description="End date of schedule validity (None = open-ended)")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    version: str = Field(default="1.0", description="Schedule version")
    source: str = Field(default="ui", description="Schedule source")

    @field_validator("valid_to")
    @classmethod
    def valid_to_after_valid_from(cls, v: Optional[date_type], info: ValidationInfo) -> Optional[date_type]:
        """Ensure valid_to >= valid_from when both are set."""
        if v is not None:
            valid_from = info.data.get("valid_from")
            if valid_from and v < valid_from:
                raise ValueError("valid_to must be >= valid_from")
        return v

    def is_effective_on(self, target_date: date_type) -> bool:
        """Check if this schedule is effective on the given date."""
        if target_date < self.valid_from:
            return False
        if self.valid_to is not None and target_date > self.valid_to:
            return False
        return True

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: int) -> int:
        """Validate device_id is positive."""
        if v <= 0:
            raise ValueError("device_id must be a positive integer")
        return v

    @field_validator("extra_hours")
    @classmethod
    def validate_extra_hours(
        cls, v: Optional[Dict[str, List[ExtraHour]]], info: ValidationInfo
    ) -> Optional[Dict[str, List[ExtraHour]]]:
        """Validate extra hours days and overlaps; normalize day keys to lowercase."""
        if not v:
            return v

        schedule: Schedule = info.data.get("schedule")
        valid_days = set(Schedule.VALID_DAYS)

        for day in v.keys():
            if day.lower() not in valid_days:
                raise ValueError(f"Invalid extra hour day: {day}")

        if schedule:
            active_days_list = schedule.active_days
            active = set(active_days_list)
            invalid_inactive = [d for d in v.keys() if d.lower() not in active]
            if invalid_inactive:
                raise ValueError(
                    f"Extra hours days must be within active_days: {invalid_inactive}"
                )

        for day, blocks in v.items():
            sorted_blocks = sorted(blocks, key=lambda b: _parse_hhmm(b.start))
            last_end: Optional[datetime] = None
            for b in sorted_blocks:
                start_dt = _parse_hhmm(b.start)
                end_dt = _parse_hhmm(b.end)
                if last_end and start_dt < last_end:
                    raise ValueError(
                        f"Overlapping extra hours on {day}: {b.start}-{b.end}"
                    )
                last_end = end_dt

        return {k.lower(): v[k] for k in v}

    @field_validator("special_days")
    @classmethod
    def validate_special_days(
        cls, v: Optional[Dict[str, SpecialDay]]
    ) -> Optional[Dict[str, SpecialDay]]:
        """Validate special days date format and parseable dates."""
        if not v:
            return v

        for date_str in v.keys():
            if not _DATE_RE.match(date_str):
                raise ValueError(
                    f"Invalid date format: {date_str}. Use YYYY-MM-DD format"
                )
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"Invalid date: {date_str}")

        return v

    def get_total_work_minutes_for_day(self, day: str) -> int:
        """Calculate total work minutes for a specific day including extra hours."""
        day_l = day.lower()
        if not self.schedule.is_work_day(day_l):
            return 0

        total_minutes = self.schedule.total_work_minutes(day_l)

        if self.extra_hours and day_l in self.extra_hours:
            for extra_hour in self.extra_hours[day_l]:
                total_minutes += extra_hour.duration_minutes()

        return total_minutes

    def get_weekly_work_minutes(self) -> int:
        """Calculate total work minutes for the week."""
        total = 0
        for day in Schedule.VALID_DAYS:
            total += self.get_total_work_minutes_for_day(day)
        return total

    def get_effective_schedule_for_date(self, target_date: date_type) -> Optional[DaySchedule]:
        """
        Get the effective schedule for a specific date.

        Priority order:
        1. Special day with exact date match (highest priority)
        2. Recurring special day with month-day match
        3. Regular weekday schedule (lowest priority)
        """
        date_str = target_date.strftime("%Y-%m-%d")

        # Priority 1: Check exact date special day
        if self.special_days and date_str in self.special_days:
            special = self.special_days[date_str]
            if special.work_hours:
                return DaySchedule(
                    work_hours=special.work_hours,
                    breaks=special.breaks,
                )
            return None

        # Priority 2: Check recurring special days (match month-day)
        if self.special_days:
            month_day = target_date.strftime("%m-%d")
            for special_date_str, special in self.special_days.items():
                if special.is_recurring and special_date_str.endswith(month_day):
                    if special.work_hours:
                        return DaySchedule(
                            work_hours=special.work_hours,
                            break_time=special.break_time,
                        )
                    return None

        # Priority 3: Fall back to regular weekday schedule
        weekday = target_date.strftime("%A").lower()
        return self.schedule.day_schedules.get(weekday)
