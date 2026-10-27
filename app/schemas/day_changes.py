"""
API schemas for per-date schedule exceptions ("Cambiar un día") and bulk
regular-schedule edits.

Input accepts camelCase or snake_case (AliasChoices); output serializes
camelCase via serialization_alias, like schemas/schedule.py. Dumping without
by_alias (e.g. ChangeSpec in email payloads) yields clean snake_case keys.
"""

import re
from datetime import date, datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from pydantic.config import ConfigDict

from .schedule import DayScheduleSchema, ExtraHourSchema

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_DAY_MINUTES = 24 * 60

ShiftTypeLiteral = Literal["day", "night"]
SkipReason = Literal["pilot", "no_shift_that_day", "overlaps_other_shift"]


def _check_hhmm(value: str) -> str:
    if not _HHMM.fullmatch(value):
        raise ValueError("La hora debe tener formato HH:MM (00:00 a 23:59)")
    return value


def _to_minutes(value: str) -> int:
    return int(value[:2]) * 60 + int(value[3:])


class HoursSpec(BaseModel):
    """Work hours of a day change, strict HH:MM."""

    start: str = Field(..., description="Start time in HH:MM format")
    end: str = Field(..., description="End time in HH:MM format")

    @field_validator("start", "end")
    @classmethod
    def validate_hhmm(cls, v: str) -> str:
        return _check_hhmm(v)

    @model_validator(mode="after")
    def validate_end_differs(self) -> "HoursSpec":
        if self.start == self.end:
            raise ValueError("La hora de término debe ser distinta a la de inicio")
        return self

    model_config = ConfigDict(populate_by_name=True)


class BreakSpec(BaseModel):
    """Break of a day change."""

    start: str = Field(..., description="Break start time in HH:MM format")
    duration_minutes: int = Field(
        ...,
        ge=5,
        le=480,
        validation_alias=AliasChoices("durationMinutes", "duration_minutes"),
        serialization_alias="durationMinutes",
        description="Break duration in minutes (5..480)",
    )

    @field_validator("start")
    @classmethod
    def validate_hhmm(cls, v: str) -> str:
        return _check_hhmm(v)

    model_config = ConfigDict(populate_by_name=True)


class ChangeSpec(BaseModel):
    """What to do on the chosen date."""

    kind: Literal["extra_hours", "custom_hours", "no_work"]
    minutes: Optional[int] = Field(
        None, ge=15, le=720, description="Extra minutes (extra_hours only)"
    )
    position: Optional[Literal["after", "before"]] = Field(
        None, description="Extra time after or before the shift (extra_hours only)"
    )
    work_hours: Optional[HoursSpec] = Field(
        None,
        validation_alias=AliasChoices("workHours", "work_hours"),
        serialization_alias="workHours",
        description="Replacement work hours (custom_hours only)",
    )
    breaks: Optional[List[BreakSpec]] = Field(
        None, description="Breaks inside work_hours (custom_hours only)"
    )
    reason: str = Field("", max_length=200, description="Optional reason")

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "ChangeSpec":
        if self.kind == "extra_hours":
            if self.minutes is None or self.position is None:
                raise ValueError("extra_hours requiere minutes y position")
            if self.work_hours is not None or self.breaks:
                raise ValueError("extra_hours no admite work_hours ni breaks")
        elif self.kind == "custom_hours":
            if self.work_hours is None:
                raise ValueError("custom_hours requiere work_hours")
            if self.minutes is not None or self.position is not None:
                raise ValueError("custom_hours no admite minutes ni position")
            if self.breaks:
                self._validate_breaks(self.work_hours, self.breaks)
        else:
            if (
                self.minutes is not None
                or self.position is not None
                or self.work_hours is not None
                or self.breaks
            ):
                raise ValueError("no_work no admite horas, minutos ni descansos")
        return self

    @staticmethod
    def _validate_breaks(work_hours: HoursSpec, breaks: List[BreakSpec]) -> None:
        start = _to_minutes(work_hours.start)
        span = (_to_minutes(work_hours.end) - start) % _DAY_MINUTES
        spans = []
        for b in breaks:
            offset = (_to_minutes(b.start) - start) % _DAY_MINUTES
            if offset + b.duration_minutes > span:
                raise ValueError("Los descansos deben quedar dentro del horario de trabajo")
            spans.append((offset, offset + b.duration_minutes))
        spans.sort()
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            if next_start < prev_end:
                raise ValueError("Los descansos no pueden solaparse")

    model_config = ConfigDict(populate_by_name=True)


def _check_device_ids(ids: List[int]) -> List[int]:
    if len(set(ids)) != len(ids):
        raise ValueError("device_ids no puede tener repetidos")
    return ids


class DayChangeRequest(BaseModel):
    """Preview/apply a change for one date over several machines."""

    date: date
    device_ids: List[int] = Field(
        ...,
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("deviceIds", "device_ids"),
        serialization_alias="deviceIds",
    )
    shift_type: ShiftTypeLiteral = Field(
        "day",
        validation_alias=AliasChoices("shiftType", "shift_type"),
        serialization_alias="shiftType",
    )
    change: ChangeSpec

    _unique_ids = field_validator("device_ids")(_check_device_ids)

    model_config = ConfigDict(populate_by_name=True)


class DayChangeDeviceResult(BaseModel):
    device_id: int = Field(..., serialization_alias="deviceId")
    label: str
    shift_type: str = Field(..., serialization_alias="shiftType")
    regular: Optional[str] = None
    result: Optional[str] = None
    replaced: Optional[str] = None
    skipped: Optional[SkipReason] = None

    model_config = ConfigDict(populate_by_name=True)


class DayChangeResponse(BaseModel):
    operation_id: Optional[UUID] = Field(None, serialization_alias="operationId")
    date: date
    applied: List[DayChangeDeviceResult]
    skipped: List[DayChangeDeviceResult]

    model_config = ConfigDict(populate_by_name=True)


class HoursRead(BaseModel):
    """Lenient output model: migrated rows may hold legacy, non-strict values."""

    start: str
    end: str

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


class BreakRead(BaseModel):
    """Lenient output model for breaks (no range validation)."""

    start: str
    duration_minutes: int = Field(
        ...,
        validation_alias=AliasChoices("durationMinutes", "duration_minutes"),
        serialization_alias="durationMinutes",
    )

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


class ExceptionRead(BaseModel):
    id: int
    device_id: int = Field(..., serialization_alias="deviceId")
    shift_type: str = Field(..., serialization_alias="shiftType")
    date: date
    kind: str
    work_hours: Optional[HoursRead] = Field(None, serialization_alias="workHours")
    breaks: Optional[List[BreakRead]] = None
    extra_minutes: Optional[int] = Field(None, serialization_alias="extraMinutes")
    extra_position: Optional[str] = Field(None, serialization_alias="extraPosition")
    reason: str = ""
    source: str

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


class BulkScheduleRequest(BaseModel):
    """Preview/apply a new regular schedule over several machines."""

    device_ids: List[int] = Field(
        ...,
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("deviceIds", "device_ids"),
        serialization_alias="deviceIds",
    )
    shift_type: ShiftTypeLiteral = Field(
        "day",
        validation_alias=AliasChoices("shiftType", "shift_type"),
        serialization_alias="shiftType",
    )
    schedule: Dict[str, DayScheduleSchema]
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]] = Field(
        None,
        validation_alias=AliasChoices("extraHours", "extra_hours"),
        serialization_alias="extraHours",
        description="None keeps each machine's current extra hours",
    )
    valid_from: date = Field(
        ...,
        validation_alias=AliasChoices("validFrom", "valid_from"),
        serialization_alias="validFrom",
    )

    _unique_ids = field_validator("device_ids")(_check_device_ids)

    model_config = ConfigDict(populate_by_name=True)


class BulkDeviceResult(BaseModel):
    device_id: int = Field(..., serialization_alias="deviceId")
    label: str
    days: List[dict] = Field(default_factory=list)
    new_valid_to: Optional[date] = Field(None, serialization_alias="newValidTo")
    new_vigencia_kind: Literal["split", "future_bounded", "new_coverage"] = Field(
        ..., serialization_alias="newVigenciaKind"
    )
    kept_exceptions: List[dict] = Field(
        default_factory=list, serialization_alias="keptExceptions"
    )
    skipped: Optional[Literal["pilot"]] = None

    model_config = ConfigDict(populate_by_name=True)


class BulkScheduleResponse(BaseModel):
    operation_id: Optional[UUID] = Field(None, serialization_alias="operationId")
    valid_from: date = Field(..., serialization_alias="validFrom")
    applied: List[BulkDeviceResult]
    skipped: List[BulkDeviceResult]

    model_config = ConfigDict(populate_by_name=True)


class EffectiveBlock(BaseModel):
    device_id: int = Field(..., serialization_alias="deviceId")
    shift_type: str = Field(..., serialization_alias="shiftType")
    shift_date: date = Field(..., serialization_alias="shiftDate")
    start: datetime
    end: datetime
    kind: str

    model_config = ConfigDict(populate_by_name=True)
