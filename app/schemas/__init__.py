"""
Schemas module initialization.

This module exports API schemas for request/response serialization.
"""

from .schedule import (
    WorkHoursSchema,
    BreakSchema,
    DayScheduleSchema,
    ExtraHourSchema,
    MetadataSchema,
    ScheduleCreate,
    ScheduleUpdate,
    SchedulePatch,
    ScheduleRead,
    ScheduleDeleteResponse,
    ErrorResponse,
)

from .day_changes import (
    HoursSpec,
    BreakSpec,
    ChangeSpec,
    DayChangeRequest,
    DayChangeDeviceResult,
    DayChangeResponse,
    ExceptionRead,
    BulkScheduleRequest,
    BulkDeviceResult,
    BulkScheduleResponse,
    EffectiveBlock,
)

__all__ = [
    "WorkHoursSchema",
    "BreakSchema",
    "DayScheduleSchema",
    "ExtraHourSchema",
    "MetadataSchema",
    "ScheduleCreate",
    "ScheduleUpdate",
    "SchedulePatch",
    "ScheduleRead",
    "ScheduleDeleteResponse",
    "ErrorResponse",
    "HoursSpec",
    "BreakSpec",
    "ChangeSpec",
    "DayChangeRequest",
    "DayChangeDeviceResult",
    "DayChangeResponse",
    "ExceptionRead",
    "BulkScheduleRequest",
    "BulkDeviceResult",
    "BulkScheduleResponse",
    "EffectiveBlock",
]
