"""
Builds `notification_outbox` payloads from a schedule change.

Pure functions only: no database, no clock, no network. The caller supplies
`changed_at`, which keeps the output deterministic and the module trivially
testable. `shifts_api` never resolves recipients, templates or the per-company
toggle — `notify_api` does all of that at send time.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from ..models.notification_event import (
    NOTIFICATION_TYPE_SHIFT_CHANGE,
    BreakRef,
    DayHours,
    OutboxEvent,
    ScheduleSnapshot,
    ShiftChangeContext,
)
# The weekday diff lives with the v2 builders, which outlive this module.
from .notification_events_v2 import _day_entry, _diff_days  # noqa: F401

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _to_micros(moment: datetime) -> int:
    """Whole microseconds since the epoch, by exact integer arithmetic.

    Deliberately not `timestamp() * 1_000_000` — that runs the value through a
    float and loses the last digit or two of microsecond precision.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(microseconds=1)


def _breaks_changed(days: List[Dict[str, Any]]) -> bool:
    return any("breaks_before" in d for d in days)


def _special_days_delta(before: Optional[ScheduleSnapshot], after: Optional[ScheduleSnapshot]) -> int:
    """How many special-day entries were added or removed."""
    keys_before = set(before.special_day_keys) if before else set()
    keys_after = set(after.special_day_keys) if after else set()
    return len(keys_before ^ keys_after)


def _validity_changed(before: Optional[ScheduleSnapshot], after: Optional[ScheduleSnapshot]) -> bool:
    if before is None or after is None:
        return False
    return (before.valid_from, before.valid_to) != (after.valid_from, after.valid_to)


def _as_iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def build_shift_change_event(ctx: ShiftChangeContext) -> Optional[OutboxEvent]:
    """Turn a schedule change into an outbox row, or None if nothing changed.

    Returns None when the mutation moved no hours, no validity range and no
    special day — a `PATCH` that only bumps `metadata.version` is not something
    a supervisor needs an email about. Creates and deletes always produce an
    event, because the schedule itself appeared or disappeared.
    """
    before = ctx.before
    after = ctx.after

    days = _diff_days(
        before.day_schedules if before else {},
        after.day_schedules if after else {},
    )
    validity_changed = _validity_changed(before, after)
    special_days_changed = _special_days_delta(before, after)

    is_lifecycle = ctx.action in ("created", "deleted")
    if not is_lifecycle and not days and not validity_changed and not special_days_changed:
        return None

    current = after or before
    payload: Dict[str, Any] = {
        "action": ctx.action,
        "device": {
            "id": ctx.device.id,
            "key": ctx.device.key,
            "display_name": ctx.device.display_name,
            "company_id": ctx.device.company_id,
        },
        "device_label": ctx.device.label,
        "shift_type": ctx.shift_type,
        "schedule_id": current.schedule_id if current else None,
        "valid_from": _as_iso(current.valid_from) if current else None,
        "valid_to": _as_iso(current.valid_to) if current else None,
        "source": current.source if current else "ui",
        "changed_at": ctx.changed_at.isoformat(),
        "days": days,
        "breaks_changed": _breaks_changed(days),
        "validity_changed": validity_changed,
        "special_days_changed": special_days_changed,
        # Always present, null when unknown. Deliberately left out of the
        # dedupe key: who made a change is not part of which change it was.
        "actor": ctx.actor.model_dump() if ctx.actor else None,
    }

    dedupe_key = (
        f"{NOTIFICATION_TYPE_SHIFT_CHANGE}:{ctx.device.id}:{ctx.shift_type}"
        f":{ctx.action}:{_to_micros(ctx.changed_at)}"
    )

    return OutboxEvent(
        type_key=NOTIFICATION_TYPE_SHIFT_CHANGE,
        company_id=ctx.device.company_id,
        dedupe_key=dedupe_key,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Snapshot adapters
#
# Two shapes reach this module: rows read back from the DB (JSONB, camelCase)
# and request schemas on the way in. Both collapse to a ScheduleSnapshot so the
# diff above never has to care which side it is looking at.
# ---------------------------------------------------------------------------


def _jsonb(value: Any) -> Dict[str, Any]:
    """A JSONB column arrives as a str or an already-parsed dict."""
    if not value:
        return {}
    return json.loads(value) if isinstance(value, str) else value


def _breaks_from_jsonb(cfg: Dict[str, Any]) -> List[BreakRef]:
    """Break list from a day config, tolerating the legacy single-object form."""
    raw = cfg.get("breaks") or ([cfg["break"]] if cfg.get("break") else [])
    return [
        BreakRef(
            start=b["start"],
            duration_minutes=b.get("durationMinutes", b.get("duration_minutes", 0)),
        )
        for b in raw
    ]


def snapshot_from_record(record: Mapping[str, Any]) -> ScheduleSnapshot:
    """Snapshot of a `device_schedules` row as it stands in the database."""
    days = {
        name: DayHours(
            start=cfg["workHours"]["start"],
            end=cfg["workHours"]["end"],
            breaks=_breaks_from_jsonb(cfg),
        )
        for name, cfg in _jsonb(record.get("day_schedules")).items()
    }
    return ScheduleSnapshot(
        schedule_id=record.get("id"),
        day_schedules=days,
        special_day_keys=sorted(_jsonb(record.get("special_days"))),
        valid_from=record.get("valid_from"),
        valid_to=record.get("valid_to"),
        source=record.get("source") or "ui",
    )


def _days_from_request(schedule: Mapping[str, Any]) -> Dict[str, DayHours]:
    return {
        name: DayHours(
            start=cfg.work_hours.start,
            end=cfg.work_hours.end,
            breaks=[
                BreakRef(start=b.start, duration_minutes=b.duration_minutes)
                for b in (cfg.breaks or [])
            ],
        )
        for name, cfg in schedule.items()
    }


def snapshot_from_request(
    data: Any,
    fallback: Optional[ScheduleSnapshot] = None,
    *,
    merge: bool = False,
) -> ScheduleSnapshot:
    """Snapshot of the state a request will leave behind.

    `merge=True` mirrors PATCH, where an omitted field keeps its current value.
    `merge=False` mirrors POST/PUT, where an omitted `specialDays` clears it —
    the same asymmetry `ScheduleService.update_schedule` and `patch_schedule`
    already encode, so the described change matches the write that happens.

    Validity always merges: both verbs leave `valid_from`/`valid_to` alone when
    the request omits them.
    """
    base = fallback or ScheduleSnapshot()

    schedule = getattr(data, "schedule", None)
    days = _days_from_request(schedule) if schedule is not None else base.day_schedules

    special = getattr(data, "special_days", None)
    if special is not None:
        special_keys = sorted(special)
    else:
        special_keys = base.special_day_keys if merge else []

    metadata = getattr(data, "metadata", None)
    if metadata is not None and metadata.source:
        source = metadata.source
    else:
        source = base.source if merge else "ui"

    return ScheduleSnapshot(
        schedule_id=base.schedule_id,
        day_schedules=days,
        special_day_keys=special_keys,
        valid_from=getattr(data, "valid_from", None) or base.valid_from,
        valid_to=getattr(data, "valid_to", None) or base.valid_to,
        source=source,
    )
