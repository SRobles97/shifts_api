"""
API schemas for schedule endpoints.

This module contains Pydantic models used for request/response serialization
in the schedule API endpoints. These are separate from business logic models.
"""

from pydantic import BaseModel, Field, AliasChoices
from pydantic.config import ConfigDict
from typing import List, Optional, Dict
from datetime import datetime


class WorkHoursSchema(BaseModel):
    """Schema for work hours in API requests/responses."""

    start: str = Field(..., description="Start time in HH:MM format")
    end: str = Field(..., description="End time in HH:MM format")

    # Permite poblar por nombre de campo
    model_config = ConfigDict(populate_by_name=True)


class BreakSchema(BaseModel):
    """Schema for break information in API requests/responses."""

    start: str = Field(..., description="Break start time in HH:MM format")
    # Acepta durationMinutes (camel) y duration_minutes (snake).
    duration_minutes: int = Field(
        ...,
        validation_alias=AliasChoices("durationMinutes", "duration_minutes"),
        serialization_alias="durationMinutes",
        description="Break duration in minutes",
    )

    model_config = ConfigDict(populate_by_name=True)


class ScheduleConfigSchema(BaseModel):
    """Schema for basic schedule configuration."""

    # Acepta activeDays y active_days
    active_days: List[str] = Field(
        ...,
        validation_alias=AliasChoices("activeDays", "active_days"),
        serialization_alias="activeDays",
        description="List of active work days",
    )
    # Acepta workHours y work_hours
    work_hours: WorkHoursSchema = Field(
        ...,
        validation_alias=AliasChoices("workHours", "work_hours"),
        serialization_alias="workHours",
        description="Regular work hours",
    )
    # Acepta "break" (camel) y "break_time" (snake)
    break_time: BreakSchema = Field(
        ...,
        validation_alias=AliasChoices("break", "break_time"),
        serialization_alias="break",
        description="Break configuration",
    )

    model_config = ConfigDict(populate_by_name=True)


class ExtraHourSchema(BaseModel):
    """Schema for extra hours definition."""

    start: str = Field(..., description="Extra hour start time in HH:MM format")
    end: str = Field(..., description="Extra hour end time in HH:MM format")

    model_config = ConfigDict(populate_by_name=True)


class MetadataSchema(BaseModel):
    """Schema for schedule metadata."""

    # Acepta createdAt y created_at
    created_at: Optional[datetime] = Field(
        None,
        validation_alias=AliasChoices("createdAt", "created_at"),
        serialization_alias="createdAt",
        description="Creation timestamp",
    )
    version: str = Field(default="1.0", description="Schedule version")
    source: str = Field(default="api", description="Schedule source")

    model_config = ConfigDict(populate_by_name=True)


class ScheduleCreateRequest(BaseModel):
    """Schema for creating/updating schedule requests."""

    # Acepta deviceName y device_name
    device_name: str = Field(
        ...,
        validation_alias=AliasChoices("deviceName", "device_name"),
        serialization_alias="deviceName",
        description="Device name",
    )
    schedule: ScheduleConfigSchema = Field(
        ..., description="Basic schedule configuration"
    )
    # Acepta extraHours y extra_hours
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description="Extra hours by day of week",
    )
    metadata: Optional[MetadataSchema] = Field(None, description="Schedule metadata")

    # Clave para permitir poblar por nombre del campo o por alias
    model_config = ConfigDict(populate_by_name=True)


class ScheduleResponse(BaseModel):
    """Schema for schedule API responses."""

    id: str = Field(..., description="Unique schedule ID")
    device_name: str = Field(
        ..., serialization_alias="deviceName", description="Device name"
    )
    schedule: ScheduleConfigSchema = Field(
        ..., description="Basic schedule configuration"
    )
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None, serialization_alias="extraHours", description="Extra hours by day of week"
    )
    metadata: MetadataSchema = Field(..., description="Schedule metadata")

    model_config = ConfigDict(populate_by_name=True)


class ScheduleDeleteResponse(BaseModel):
    """Schema for schedule deletion responses."""

    message: str = Field(..., description="Deletion confirmation message")

    model_config = ConfigDict(populate_by_name=True)


class ErrorResponse(BaseModel):
    """Schema for error responses."""

    detail: str = Field(..., description="Error description")

    model_config = ConfigDict(populate_by_name=True)
