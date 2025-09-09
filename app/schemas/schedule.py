"""
API schemas for schedule endpoints.

This module contains Pydantic models used for request/response serialization
in the schedule API endpoints. These are separate from business logic models.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime


class WorkHoursSchema(BaseModel):
    """Schema for work hours in API requests/responses."""

    start: str = Field(..., description="Start time in HH:MM format")
    end: str = Field(..., description="End time in HH:MM format")


class BreakSchema(BaseModel):
    """Schema for break information in API requests/responses."""

    start: str = Field(..., description="Break start time in HH:MM format")
    duration_minutes: int = Field(
        ..., alias="durationMinutes", description="Break duration in minutes"
    )


class ScheduleConfigSchema(BaseModel):
    """Schema for basic schedule configuration."""

    active_days: List[str] = Field(
        ..., alias="activeDays", description="List of active work days"
    )
    work_hours: WorkHoursSchema = Field(
        ..., alias="workHours", description="Regular work hours"
    )
    break_time: BreakSchema = Field(
        ..., alias="break", description="Break configuration"
    )


class ExtraHourSchema(BaseModel):
    """Schema for extra hours definition."""

    start: str = Field(..., description="Extra hour start time in HH:MM format")
    end: str = Field(..., description="Extra hour end time in HH:MM format")


class MetadataSchema(BaseModel):
    """Schema for schedule metadata."""

    created_at: Optional[datetime] = Field(
        None, alias="createdAt", description="Creation timestamp"
    )
    version: str = Field(default="1.0", description="Schedule version")
    source: str = Field(default="api", description="Schedule source")


class ScheduleCreateRequest(BaseModel):
    """Schema for creating/updating schedule requests."""

    device_name: str = Field(..., alias="deviceName", description="Device name")
    schedule: ScheduleConfigSchema = Field(
        ..., description="Basic schedule configuration"
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None, alias="extraHours", description="Extra hours by day of week"
    )
    metadata: Optional[MetadataSchema] = Field(None, description="Schedule metadata")


class ScheduleResponse(BaseModel):
    """Schema for schedule API responses."""

    id: str = Field(..., description="Unique schedule ID")
    device_name: str = Field(..., alias="deviceName", description="Device name")
    schedule: ScheduleConfigSchema = Field(
        ..., description="Basic schedule configuration"
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None, alias="extraHours", description="Extra hours by day of week"
    )
    metadata: MetadataSchema = Field(..., description="Schedule metadata")


class ScheduleDeleteResponse(BaseModel):
    """Schema for schedule deletion responses."""

    message: str = Field(..., description="Deletion confirmation message")


class ErrorResponse(BaseModel):
    """Schema for error responses."""

    detail: str = Field(..., description="Error description")
