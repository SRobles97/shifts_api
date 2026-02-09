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
from typing import List, Optional, Dict
from datetime import datetime


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
    break_time: BreakSchema = Field(
        ...,
        validation_alias=AliasChoices("break", "break_time"),
        serialization_alias="break",
        description="Break configuration for this day",
    )

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
    break_time: Optional[BreakSchema] = Field(
        None,
        validation_alias=AliasChoices("break", "break_time"),
        serialization_alias="break",
        description="Break configuration for this special day"
    )
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


# ========== Request Schemas ==========


class ScheduleCreate(BaseModel):
    """Schema for creating a schedule (POST)."""

    device_id: int = Field(
        ...,
        validation_alias=AliasChoices("deviceId", "device_id"),
        serialization_alias="deviceId",
        description="Device ID (FK to devices table)",
    )
    schedule: Dict[str, DayScheduleSchema] = Field(
        ...,
        description="Schedule configuration by day (e.g., {'monday': {...}, 'tuesday': {...}})",
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description="Extra hours by day of week",
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None,
        validation_alias=AliasChoices("specialDays", "special_days"),
        serialization_alias="specialDays",
        description="Special day overrides keyed by ISO date (YYYY-MM-DD)",
    )
    metadata: Optional[MetadataSchema] = Field(None, description="Schedule metadata")

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("deviceId must be a positive integer")
        return v

    model_config = ConfigDict(populate_by_name=True)


class ScheduleUpdate(BaseModel):
    """Schema for full schedule replacement (PUT). device_id comes from URL."""

    schedule: Dict[str, DayScheduleSchema] = Field(
        ..., description="Schedule configuration by day"
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description="Extra hours by day of week",
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None,
        validation_alias=AliasChoices("specialDays", "special_days"),
        serialization_alias="specialDays",
        description="Special day overrides keyed by ISO date (YYYY-MM-DD)",
    )
    metadata: Optional[MetadataSchema] = Field(None, description="Schedule metadata")

    model_config = ConfigDict(populate_by_name=True)


class SchedulePatch(BaseModel):
    """Schema for partial schedule updates (PATCH). All fields optional."""

    schedule: Optional[Dict[str, DayScheduleSchema]] = Field(
        None, description="Schedule configuration by day"
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description="Extra hours by day of week",
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None,
        validation_alias=AliasChoices("specialDays", "special_days"),
        serialization_alias="specialDays",
        description="Special day overrides keyed by ISO date (YYYY-MM-DD)",
    )
    metadata: Optional[MetadataSchema] = Field(None, description="Schedule metadata")

    model_config = ConfigDict(populate_by_name=True)


class ScheduleRead(BaseModel):
    """Schema for schedule API responses."""

    id: str = Field(..., description="Unique schedule ID")
    device_id: int = Field(
        ..., serialization_alias="deviceId", description="Device ID"
    )
    schedule: Dict[str, DayScheduleSchema] = Field(
        ..., description="Schedule configuration by day"
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None, serialization_alias="extraHours", description="Extra hours by day of week"
    )
    special_days: Optional[Dict[str, SpecialDaySchema]] = Field(
        None, serialization_alias="specialDays", description="Special day overrides keyed by ISO date (YYYY-MM-DD)"
    )
    metadata: MetadataSchema = Field(..., description="Schedule metadata")

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
