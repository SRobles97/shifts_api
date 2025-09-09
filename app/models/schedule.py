"""
Business logic models for schedule management.

These models represent the core business entities and logic,
separate from API serialization concerns.
"""

from pydantic import BaseModel, Field, field_validator
from pydantic_core.core_schema import ValidationInfo
from typing import List, Optional, Dict, ClassVar
from datetime import datetime, timedelta
import re


_TIME_RE = re.compile(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$")


def _parse_hhmm(value: str) -> datetime:
    """Parse 'HH:MM' to a datetime (today's date)."""
    return datetime.strptime(value, "%H:%M")


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


class Schedule(BaseModel):
    """
    Core business model for work schedules.

    Represents the complete schedule configuration with business logic
    for validation and calculations.
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

    active_days: List[str] = Field(..., description="List of active work days")
    work_hours: WorkHours = Field(..., description="Regular work hours")
    break_time: Break = Field(..., description="Break configuration")

    @field_validator("active_days")
    @classmethod
    def validate_days(cls, v: List[str]) -> List[str]:
        """Validate active days are valid weekdays; normalize and dedupe."""
        if not v:
            raise ValueError("At least one active day is required")
        normalized = [day.lower() for day in v]
        invalid = [d for d in normalized if d not in cls.VALID_DAYS]
        if invalid:
            raise ValueError(f"Invalid days: {invalid}")
        # dedupe while preserving order
        seen, out = set(), []
        for d in normalized:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    @field_validator("break_time")
    @classmethod
    def break_within_work_hours(cls, v: Break, info: ValidationInfo) -> Break:
        """Ensure break starts and ends within work hours."""
        work_hours: WorkHours = info.data.get("work_hours")
        if work_hours:
            work_start_dt = _parse_hhmm(work_hours.start)
            work_end_dt = _parse_hhmm(work_hours.end)
            break_start_dt = _parse_hhmm(v.start)
            break_end_dt = break_start_dt + timedelta(minutes=v.duration_minutes)

            if not (
                work_start_dt.time() <= break_start_dt.time() <= work_end_dt.time()
            ):
                raise ValueError("Break must start within work hours")
            if break_end_dt > work_end_dt:
                raise ValueError("Break must end within work hours")
        return v

    def total_work_minutes(self) -> int:
        """Calculate total work minutes excluding break."""
        return self.work_hours.duration_minutes() - self.break_time.duration_minutes

    def is_work_day(self, day: str) -> bool:
        """Check if a given day is a work day."""
        return day.lower() in self.active_days


class ScheduleEntity(BaseModel):
    """
    Complete business entity for a device schedule.

    Includes all schedule information plus metadata and extra hours.
    """

    id: Optional[int] = None
    device_name: str = Field(..., description="Device identifier")
    schedule: Schedule = Field(..., description="Basic schedule configuration")
    extra_hours: Optional[Dict[str, List[ExtraHour]]] = Field(
        None, description="Extra hours by day of week"
    )
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    version: str = Field(default="1.0", description="Schedule version")
    source: str = Field(default="api", description="Schedule source")

    @field_validator("device_name")
    @classmethod
    def validate_device_name(cls, v: str) -> str:
        """Validate device name is not empty and normalize whitespace."""
        if not v or not v.strip():
            raise ValueError("Device name cannot be empty")
        return v.strip()

    @field_validator("extra_hours")
    @classmethod
    def validate_extra_hours(
        cls, v: Optional[Dict[str, List[ExtraHour]]], info: ValidationInfo
    ) -> Optional[Dict[str, List[ExtraHour]]]:
        """Validate extra hours days and overlaps; normalize day keys to lowercase."""
        if not v:
            return v

        schedule: Schedule = info.data.get("schedule")  # may be None in partial loads
        valid_days = set(Schedule.VALID_DAYS)

        # Validate day keys
        for day in v.keys():
            if day.lower() not in valid_days:
                raise ValueError(f"Invalid extra hour day: {day}")

        # If schedule is available, enforce extra_hours only for active_days
        if schedule:
            active = set(schedule.active_days)
            invalid_inactive = [d for d in v.keys() if d.lower() not in active]
            if invalid_inactive:
                raise ValueError(
                    f"Extra hours days must be within active_days: {invalid_inactive}"
                )

        # Check overlaps within the same day
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

        # Normalize keys to lowercase
        return {k.lower(): v[k] for k in v}

    def get_total_work_minutes_for_day(self, day: str) -> int:
        """Calculate total work minutes for a specific day including extra hours."""
        day_l = day.lower()
        if not self.schedule.is_work_day(day_l):
            return 0

        total_minutes = self.schedule.total_work_minutes()

        # Add extra hours for the day
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
