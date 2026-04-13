"""
API schemas for schedule endpoints.

This module contains Pydantic models used for request/response serialization
in the schedule API endpoints. These are separate from business logic models.

Naming convention:
- ScheduleCreate: POST request body
- ScheduleUpdate: PUT request body (full replacement, device_id from URL)
- SchedulePatch: PATCH request body (partial update, all fields optional)
- ScheduleRead: Response body
"""

from pydantic import BaseModel, Field, AliasChoices, field_validator
from pydantic.config import ConfigDict

SHIFT_TYPE_DESCRIPTION = "Shift type label: 'day' or 'night'"
from typing import Any, List, Optional, Dict
from datetime import date, datetime


class WorkHoursSchema(BaseModel):
    """Schema for work hours in API requests/responses."""

    start: str = Field(..., description="Start time in HH:MM format")
    end: str = Field(..., description="End time in HH:MM format")

    model_config = ConfigDict(populate_by_name=True)


class BreakSchema(BaseModel):
    """Schema for break information in API requests/responses."""

    start: str = Field(..., description="Break start time in HH:MM format")
    duration_minutes: int = Field(
        ...,
        validation_alias=AliasChoices("durationMinutes", "duration_minutes"),
        serialization_alias="durationMinutes",
        description="Break duration in minutes",
    )

    model_config = ConfigDict(populate_by_name=True)


class DayScheduleSchema(BaseModel):
    """Schema for a single day's schedule configuration."""

    work_hours: WorkHoursSchema = Field(
        ...,
        validation_alias=AliasChoices("workHours", "work_hours"),
        serialization_alias="workHours",
        description="Work hours for this day",
    )
    breaks: Optional[List[BreakSchema]] = Field(
        None,
        validation_alias=AliasChoices("breaks", "break", "break_time"),
        serialization_alias="breaks",
        description="Break configurations for this day (optional)",
    )

    @field_validator("breaks", mode="before")
    @classmethod
    def wrap_single_break(cls, v: Any) -> Any:
        """Accept legacy single break object and wrap into a list."""
        if isinstance(v, dict):
            return [v]
        return v

    model_config = ConfigDict(populate_by_name=True)


class ExtraHourSchema(BaseModel):
    """Schema for extra hours definition."""

    start: str = Field(..., description="Extra hour start time in HH:MM format")
    end: str = Field(..., description="Extra hour end time in HH:MM format")

    model_config = ConfigDict(populate_by_name=True)


class SpecialDaySchema(BaseModel):
    """Schema for special day configuration in API."""

    name: str = Field(..., min_length=1, max_length=100, description="Name of the special day")
    type: str = Field(
        ...,
        description="Type of special day: holiday, maintenance, special_event, closure, training"
    )
    work_hours: Optional[WorkHoursSchema] = Field(
        None,
        validation_alias=AliasChoices("workHours", "work_hours"),
        serialization_alias="workHours",
        description="Work hours for this special day (null = no work)"
    )
    breaks: Optional[List[BreakSchema]] = Field(
        None,
        validation_alias=AliasChoices("breaks", "break", "break_time"),
        serialization_alias="breaks",
        description="Break configurations for this special day"
    )

    @field_validator("breaks", mode="before")
    @classmethod
    def wrap_single_break(cls, v: Any) -> Any:
        """Accept legacy single break object and wrap into a list."""
        if isinstance(v, dict):
            return [v]
        return v

    is_recurring: bool = Field(
        default=False,
        validation_alias=AliasChoices("isRecurring", "is_recurring"),
        serialization_alias="isRecurring",
        description="Whether this special day recurs annually"
    )
    recurrence_pattern: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("recurrencePattern", "recurrence_pattern"),
        serialization_alias="recurrencePattern",
        description="Recurrence pattern: yearly or none"
    )

    model_config = ConfigDict(populate_by_name=True)


class MetadataSchema(BaseModel):
    """Schema for schedule metadata."""

    created_at: Optional[datetime] = Field(
        None,
        validation_alias=AliasChoices("createdAt", "created_at"),
        serialization_alias="createdAt",
        description="Creation timestamp",
    )
    version: str = Field(default="1.0", description="Schedule version")
    source: str = Field(default="ui", description="Schedule source")

    model_config = ConfigDict(populate_by_name=True)


_DESC_EXTRA_HOURS = "Extra hours by day of week"
_DESC_SPECIAL_DAYS = "Special day overrides keyed by ISO date (YYYY-MM-DD)"
_DESC_METADATA = "Schedule metadata"
_DESC_SCHEDULE_BY_DAY = "Schedule configuration by day"

# ========== Request Schemas ==========


class ScheduleCreate(BaseModel):
    """Schema for creating a schedule (POST).

    Accepts either ``deviceId`` (int) or ``deviceName`` (str).
    When ``deviceName`` is provided the service layer resolves it to an ID
    via the ``devices`` table.
    """

    device_id: Optional[int] = Field(
        None,
        validation_alias=AliasChoices("deviceId", "device_id"),
        serialization_alias="deviceId",
        description="Device ID (FK to devices table)",
    )
    device_name: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("deviceName", "device_name"),
        serialization_alias="deviceName",
        description="Device name — resolved to device_id automatically",
    )
    shift_type: str = Field(
        default="day",
        validation_alias=AliasChoices("shiftType", "shift_type"),
        serialization_alias="shiftType",
        description=SHIFT_TYPE_DESCRIPTION,
    )
    schedule: Dict[str, DayScheduleSchema] = Field(
        ...,
        description="Schedule configuration by day (e.g., {'monday': {...}, 'tuesday': {...}})",
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description=_DESC_EXTRA_HOURS,
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None,
        validation_alias=AliasChoices("specialDays", "special_days"),
        serialization_alias="specialDays",
        description=_DESC_SPECIAL_DAYS,
    )
    valid_from: date = Field(
        ...,
        validation_alias=AliasChoices("validFrom", "valid_from"),
        serialization_alias="validFrom",
        description="Start date of schedule validity (YYYY-MM-DD)",
    )
    valid_to: Optional[date] = Field(
        None,
        validation_alias=AliasChoices("validTo", "valid_to"),
        serialization_alias="validTo",
        description="End date of schedule validity (None = open-ended)",
    )
    metadata: Optional[MetadataSchema] = Field(None, description=_DESC_METADATA)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("deviceId must be a positive integer")
        return v

    model_config = ConfigDict(populate_by_name=True)


class ScheduleUpdate(BaseModel):
    """Schema for full schedule replacement (PUT). device_id comes from URL."""

    shift_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("shiftType", "shift_type"),
        serialization_alias="shiftType",
        description=SHIFT_TYPE_DESCRIPTION,
    )
    schedule: Dict[str, DayScheduleSchema] = Field(
        ..., description=_DESC_SCHEDULE_BY_DAY
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description=_DESC_EXTRA_HOURS,
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None,
        validation_alias=AliasChoices("specialDays", "special_days"),
        serialization_alias="specialDays",
        description=_DESC_SPECIAL_DAYS,
    )
    valid_from: Optional[date] = Field(
        None,
        validation_alias=AliasChoices("validFrom", "valid_from"),
        serialization_alias="validFrom",
        description="New start date of schedule validity",
    )
    valid_to: Optional[date] = Field(
        None,
        validation_alias=AliasChoices("validTo", "valid_to"),
        serialization_alias="validTo",
        description="New end date of schedule validity",
    )
    metadata: Optional[MetadataSchema] = Field(None, description=_DESC_METADATA)

    model_config = ConfigDict(populate_by_name=True)


class SchedulePatch(BaseModel):
    """Schema for partial schedule updates (PATCH). All fields optional."""

    shift_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("shiftType", "shift_type"),
        serialization_alias="shiftType",
        description=SHIFT_TYPE_DESCRIPTION,
    )
    schedule: Optional[Dict[str, DayScheduleSchema]] = Field(
        None, description=_DESC_SCHEDULE_BY_DAY
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description=_DESC_EXTRA_HOURS,
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None,
        validation_alias=AliasChoices("specialDays", "special_days"),
        serialization_alias="specialDays",
        description=_DESC_SPECIAL_DAYS,
    )
    valid_from: Optional[date] = Field(
        None,
        validation_alias=AliasChoices("validFrom", "valid_from"),
        serialization_alias="validFrom",
        description="New start date of schedule validity",
    )
    valid_to: Optional[date] = Field(
        None,
        validation_alias=AliasChoices("validTo", "valid_to"),
        serialization_alias="validTo",
        description="New end date of schedule validity",
    )
    metadata: Optional[MetadataSchema] = Field(None, description=_DESC_METADATA)

    model_config = ConfigDict(populate_by_name=True)


class ScheduleRead(BaseModel):
    """Schema for schedule API responses."""

    id: str = Field(..., description="Unique schedule ID")
    device_id: int = Field(
        ..., serialization_alias="deviceId", description="Device ID"
    )
    device_name: Optional[str] = Field(
        None, serialization_alias="deviceName", description="Device name (from devices table)"
    )
    shift_type: str = Field(
        default="day", serialization_alias="shiftType", description=SHIFT_TYPE_DESCRIPTION
    )
    schedule: Dict[str, DayScheduleSchema] = Field(
        ..., description=_DESC_SCHEDULE_BY_DAY
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None, serialization_alias="extraHours", description=_DESC_EXTRA_HOURS
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None, serialization_alias="specialDays", description=_DESC_SPECIAL_DAYS
    )
    valid_from: date = Field(
        ..., serialization_alias="validFrom", description="Start date of schedule validity"
    )
    valid_to: Optional[date] = Field(
        None, serialization_alias="validTo", description="End date of schedule validity"
    )
    metadata: MetadataSchema = Field(..., description=_DESC_METADATA)

    model_config = ConfigDict(populate_by_name=True)


class ScheduleDeleteResponse(BaseModel):
    """Schema for schedule deletion responses."""

    message: str = Field(..., description="Deletion confirmation message")

    model_config = ConfigDict(populate_by_name=True)


class ScheduleStatsSchema(BaseModel):
    """Schema for individual device schedule statistics."""

    device_id: int = Field(
        ..., serialization_alias="deviceId", description="Device ID"
    )
    schedule_start: str = Field(
        ...,
        serialization_alias="scheduleStart",
        description="Schedule start time in HH:MM format",
    )
    schedule_end: str = Field(
        ...,
        serialization_alias="scheduleEnd",
        description="Schedule end time in HH:MM format",
    )
    current_time: str = Field(
        ...,
        serialization_alias="currentTime",
        description="Current time in HH:MM format",
    )
    hours_used: float = Field(
        ..., serialization_alias="hoursUsed", description="Hours worked so far today"
    )
    total_work_hours: float = Field(
        ...,
        serialization_alias="totalWorkHours",
        description="Total work hours scheduled for today",
    )
    usage_percentage: float = Field(
        ...,
        serialization_alias="usagePercentage",
        description="Percentage of work time used (0-100)",
    )

    model_config = ConfigDict(populate_by_name=True)


class AllScheduleStatsResponse(BaseModel):
    """Schema for all devices schedule statistics response."""

    request_time: str = Field(
        ...,
        serialization_alias="requestTime",
        description="Time when the request was made",
    )
    devices: List[ScheduleStatsSchema] = Field(
        ..., description="Statistics for all devices"
    )

    model_config = ConfigDict(populate_by_name=True)


class SingleScheduleStatsResponse(BaseModel):
    """Schema for single device schedule statistics response."""

    request_time: str = Field(
        ...,
        serialization_alias="requestTime",
        description="Time when the request was made",
    )
    device_stats: ScheduleStatsSchema = Field(
        ...,
        serialization_alias="deviceStats",
        description="Statistics for the requested device",
    )

    model_config = ConfigDict(populate_by_name=True)


class ErrorResponse(BaseModel):
    """Schema for error responses."""

    detail: str = Field(..., description="Error description")

    model_config = ConfigDict(populate_by_name=True)
