"""
Builds v2 `notification_outbox` payloads: one event per operation, N machines.

Pure functions only: no database, no clock, no network. `change`, `devices`,
`skipped` and `kept_exceptions` arrive already as plain dicts in the shape
notify_api's v2 renderer reads (snake_case, no aliases); this module
assembles the envelope and owns the weekday diff (`_diff_days`) callers use
to fill `devices[].days`.
"""

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID

from ..models.notification_event import (
    NOTIFICATION_TYPE_SHIFT_CHANGE,
    Actor,
    DayHours,
    OutboxEvent,
)
from ..models.schedule import Schedule

PAYLOAD_VERSION = 2

_NO_BREAKS = "sin pausas"

# En dash, not a hyphen: this string goes straight into the email body.
_HOURS_SEPARATOR = "–"


# ---------------------------------------------------------------------------
# Weekday diff of a recurring schedule (also used by the v1 builder)
# ---------------------------------------------------------------------------


# A weekday's extra hours as (start, end) "HH:MM" pairs.
Extras = Sequence[Tuple[str, str]]

_DAY_MINUTES = 24 * 60
# Extras starting up to this long before the shift are read as "before" it.
_BEFORE_WINDOW = 12 * 60


def _clock(value: str) -> int:
    try:
        return int(value[:2]) * 60 + int(value[3:5])
    except (TypeError, ValueError):
        return 0


def _format_hours(day: Optional[DayHours], extras: Extras = ()) -> Optional[str]:
    """Work hours as "HH:MM–HH:MM", extras joined by " + " in chronological order.

    Order runs from the shift start, so an extra just before it comes first
    ("06:00–08:00 + 08:00–17:00") and one after it last, also across midnight.
    None when the day has neither work hours nor extras.
    """
    pieces = [(day.start, day.end)] if day else []
    pieces += list(extras)
    if not pieces:
        return None
    if day is None:
        # no shift to anchor on: plain clock order
        def offset(piece: Tuple[str, str]) -> int:
            return _clock(piece[0])
    else:
        anchor = _clock(day.start)

        def offset(piece: Tuple[str, str]) -> int:
            return (_clock(piece[0]) - anchor + _BEFORE_WINDOW) % _DAY_MINUTES

    return " + ".join(f"{a}{_HOURS_SEPARATOR}{b}" for a, b in sorted(pieces, key=offset))


def _format_breaks(day: DayHours) -> str:
    if not day.breaks:
        return _NO_BREAKS
    return ", ".join(f"{b.start} ({b.duration_minutes} min)" for b in day.breaks)


def _ordered_days(*sources: Mapping[str, Any]) -> List[str]:
    """Weekday-ordered union of the day keys present in any source.

    Anything unrecognised (there should be nothing) is appended alphabetically
    rather than dropped, so an odd key still shows up in the email.
    """
    seen = {day for source in sources for day in source}
    known = [d for d in Schedule.VALID_DAYS if d in seen]
    unknown = sorted(seen - set(Schedule.VALID_DAYS))
    return known + unknown


def _day_entry(
    name: str,
    before: Optional[DayHours],
    after: Optional[DayHours],
    extras_before: Extras = (),
    extras_after: Extras = (),
) -> Optional[Dict[str, Any]]:
    """One row of the day diff, or None when that day did not change."""
    hours_before = _format_hours(before, extras_before)
    hours_after = _format_hours(after, extras_after)
    breaks_before = _format_breaks(before) if before else None
    breaks_after = _format_breaks(after) if after else None

    if hours_before == hours_after and breaks_before == breaks_after:
        return None

    if hours_before is None:
        change = "added"
    elif hours_after is None:
        change = "removed"
    else:
        change = "modified"

    entry: Dict[str, Any] = {
        "day": name,
        "before": hours_before,
        "after": hours_after,
        "change": change,
    }
    # Only carry break detail when it is part of what changed — otherwise the
    # email repeats the same pause on every row.
    if change == "modified" and breaks_before != breaks_after:
        entry["breaks_before"] = breaks_before
        entry["breaks_after"] = breaks_after
    return entry


def _diff_days(
    before: Dict[str, DayHours],
    after: Dict[str, DayHours],
    extras_before: Optional[Mapping[str, Extras]] = None,
    extras_after: Optional[Mapping[str, Extras]] = None,
) -> List[Dict[str, Any]]:
    """Changed weekdays; with extras given, a change in extra hours alone counts."""
    extras_before = extras_before or {}
    extras_after = extras_after or {}
    entries = []
    for name in _ordered_days(before, after, extras_before, extras_after):
        entry = _day_entry(
            name,
            before.get(name),
            after.get(name),
            extras_before.get(name, ()),
            extras_after.get(name, ()),
        )
        if entry is not None:
            entries.append(entry)
    return entries


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
