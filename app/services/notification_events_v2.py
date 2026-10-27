"""
Builds v2 `notification_outbox` payloads: one event per operation, N machines.

Pure functions only: no database, no clock, no network. `change`, `devices`,
`skipped` and `kept_exceptions` arrive already as plain dicts in the shape
notify_api's v2 renderer reads (snake_case, no aliases); this module only
assembles the envelope.
"""

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from ..models.notification_event import (
    NOTIFICATION_TYPE_SHIFT_CHANGE,
    Actor,
    OutboxEvent,
)

PAYLOAD_VERSION = 2


def _iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _envelope(
    operation: str,
    operation_id: UUID,
    changed_at: datetime,
    actor: Optional[Actor],
) -> Dict[str, Any]:
    return {
        "version": PAYLOAD_VERSION,
        "operation": operation,
        "operation_id": str(operation_id),
        "changed_at": _iso_utc(changed_at),
        "actor": actor.model_dump() if actor else None,
        "source": "ui",
    }


def _event(company_id: int, operation_id: UUID, payload: Dict[str, Any]) -> OutboxEvent:
    return OutboxEvent(
        type_key=NOTIFICATION_TYPE_SHIFT_CHANGE,
        company_id=company_id,
        dedupe_key=f"{NOTIFICATION_TYPE_SHIFT_CHANGE}:{operation_id}",
        payload=payload,
    )


def build_day_change_event(
    *,
    company_id: int,
    operation_id: UUID,
    changed_at: datetime,
    actor: Optional[Actor],
    day: date,
    change: Dict[str, Any],
    devices: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    removed: bool = False,
) -> OutboxEvent:
    """A per-date exception applied (or removed) on one or more machines."""
    operation = "day_change_removed" if removed else "day_change"
    payload = _envelope(operation, operation_id, changed_at, actor)
    payload.update(
        {
            "date": day.isoformat(),
            "change": change,
            "devices": devices,
            "skipped": skipped,
            "kept_exceptions": [],
        }
    )
    return _event(company_id, operation_id, payload)


def _has_changes(device: Dict[str, Any]) -> bool:
    return bool(device.get("days")) or device.get("validity_shortened") is not None


def build_recurring_change_event(
    *,
    company_id: int,
    operation_id: UUID,
    changed_at: datetime,
    actor: Optional[Actor],
    valid_from: date,
    devices: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    kept_exceptions: List[Dict[str, Any]],
) -> Optional[OutboxEvent]:
    """A recurring-schedule change, or None when no machine actually changed."""
    if not any(_has_changes(d) for d in devices):
        return None
    payload = _envelope("recurring_change", operation_id, changed_at, actor)
    payload.update(
        {
            "valid_from": valid_from.isoformat(),
            "devices": devices,
            "skipped": skipped,
            "kept_exceptions": kept_exceptions,
        }
    )
    return _event(company_id, operation_id, payload)
