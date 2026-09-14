"""
Domain models for the notification outbox.

These describe *what happened* to a schedule, independent of how it is
eventually rendered or delivered. `shifts_api` only ever writes the event;
`notify_api` resolves recipients, template and delivery at send time.

Deliberately NOT reusing `DaySchedule` from `models.schedule`: that model
enforces business rules (breaks inside work hours, no overlaps) that request
payloads are not currently checked against. Describing a change must never be
able to reject a write the schedule layer would have accepted, so these models
carry the values verbatim and validate nothing.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ShiftChangeAction = Literal["created", "updated", "patched", "deleted"]

NOTIFICATION_TYPE_SHIFT_CHANGE = "shift_change"


class BreakRef(BaseModel):
    """A break, as written — no validation of its own."""

    start: str
    duration_minutes: int


class DayHours(BaseModel):
    """One weekday's work hours, as written — no validation of its own."""

    start: str
    end: str
    breaks: List[BreakRef] = Field(default_factory=list)


class DeviceRef(BaseModel):
    """The device a schedule belongs to, resolved from the `devices` table."""

    id: int = Field(..., description="devices.id")
    company_id: int = Field(..., description="devices.company_id — routes the notification")
    key: str = Field(default="", description="devices.device_key")
    display_name: str = Field(default="", description="devices.display_name")

    @property
    def label(self) -> str:
        """Human-facing device name, falling back to the key then the id."""
        return self.display_name or self.key or f"#{self.id}"


class Actor(BaseModel):
    """The person who made a change, as verified by auth_api's /auth/me."""

    email: str
    name: str = Field(default="", description="users.full_name, empty when unset")


class ScheduleSnapshot(BaseModel):
    """One side of a schedule change — the state before or after."""

    schedule_id: Optional[int] = Field(default=None, description="device_schedules.id, unknown for a create")
    day_schedules: Dict[str, DayHours] = Field(default_factory=dict)
    special_day_keys: List[str] = Field(default_factory=list)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    source: str = Field(default="ui", description="device_schedules.source")


class ShiftChangeContext(BaseModel):
    """Everything the payload builder needs, with no I/O of its own.

    `changed_at` is injected rather than read from the clock so the builder
    stays deterministic under test.
    """

    action: ShiftChangeAction
    device: DeviceRef
    shift_type: str
    changed_at: datetime
    before: Optional[ScheduleSnapshot] = None
    after: Optional[ScheduleSnapshot] = None
    actor: Optional[Actor] = None


class OutboxEvent(BaseModel):
    """A row destined for `notification_outbox`."""

    type_key: str
    company_id: int
    dedupe_key: str
    payload: Dict[str, Any]
