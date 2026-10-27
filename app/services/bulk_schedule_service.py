"""
"Editar turno habitual" for several machines: a new regular weekly schedule
from a date, preview and apply.

Both paths run the same core (`_run`) inside a transaction. Each machine gets
its vigencia handled by `bulk_schedules_crud.apply_for_device`; pilots are
skipped. The preview raises a private sentinel to discard everything; the apply
commits and enqueues a single v2 `recurring_change` event for the operation.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import asyncpg
from loguru import logger

from ..models.notification_event import Actor, DeviceRef
from ..models.schedule import Schedule
from ..repositories.bulk_schedules import bulk_schedules_crud
from ..repositories.exceptions import exceptions_crud
from ..repositories.notification_outbox import notification_outbox_crud
from ..schemas.day_changes import BulkDeviceResult, BulkScheduleRequest, BulkScheduleResponse
from .exception_summary import kept_exception_entries
from .notification_events import _days_from_request, extras_from_jsonb, snapshot_from_record
from .notification_events_v2 import Extras, _diff_days, build_recurring_change_event
from . import schedule_service as schedule_module
from .schedule_service import (
    _assert_day_shift_within_day,
    _serialize_day_schedules,
    _serialize_extra_hours,
)


class _PreviewRollback(Exception):
    """Raised inside the preview transaction only to force its rollback."""


def _validate(req: BulkScheduleRequest) -> None:
    unknown = sorted(set(req.schedule) - set(Schedule.VALID_DAYS))
    if req.extra_hours:
        unknown += sorted(set(req.extra_hours) - set(Schedule.VALID_DAYS) - set(unknown))
    if unknown:
        raise ValueError(f"Días no válidos: {', '.join(unknown)}")
    _assert_day_shift_within_day(req.shift_type, req.schedule, None)


def _extra_hours_json(req: BulkScheduleRequest) -> Optional[str]:
    """None keeps each machine's extras; an empty dict clears them."""
    if req.extra_hours is None:
        return None
    return _serialize_extra_hours(req.extra_hours) or "{}"


async def _run(
    conn: asyncpg.Connection,
    req: BulkScheduleRequest,
    refs: Dict[int, DeviceRef],
) -> Tuple[List[BulkDeviceResult], List[BulkDeviceResult]]:
    """Write the schedule machine by machine on an open transaction.

    Returns (applied, skipped) in request order. The caller decides whether the
    transaction commits.
    """
    ids = list(req.device_ids)
    day_schedules_json = _serialize_day_schedules(req.schedule)
    extra_hours_json = _extra_hours_json(req)
    requested = _days_from_request(req.schedule)
    requested_extras = (
        None
        if req.extra_hours is None
        else {day: [(h.start, h.end) for h in hours] for day, hours in req.extra_hours.items()}
    )
    pilots = await exceptions_crud.mirrored_ids(conn, ids)

    applied: List[BulkDeviceResult] = []
    skipped: List[BulkDeviceResult] = []

    for device_id in ids:
        label = refs[device_id].label
        if device_id in pilots:
            plan = await bulk_schedules_crud.plan(conn, device_id, req.shift_type, req.valid_from)
            skipped.append(
                BulkDeviceResult(
                    device_id=device_id,
                    label=label,
                    new_valid_to=plan["new_valid_to"],
                    new_vigencia_kind=plan["kind"],
                    skipped="pilot",
                )
            )
            continue

        try:
            outcome = await bulk_schedules_crud.apply_for_device(
                conn, device_id, req.shift_type, req.valid_from, day_schedules_json, extra_hours_json
            )
        except asyncpg.exceptions.ExclusionViolationError as e:
            # the whole operation rolls back; say which machine blocked it
            raise ValueError(f"{label}: la vigencia se cruza con otro horario de la máquina") from e
        before = outcome["before_row"]
        before_days = snapshot_from_record(before).day_schedules if before is not None else {}
        before_extras = extras_from_jsonb(before["extra_hours"]) if before is not None else {}
        # omitted extraHours: the machine keeps (and carries) its own extras
        after_extras = before_extras if requested_extras is None else requested_extras
        applied.append(
            BulkDeviceResult(
                device_id=device_id,
                label=label,
                days=_diff_days(before_days, requested, before_extras, after_extras),
                new_valid_to=outcome["new_valid_to"],
                new_vigencia_kind=outcome["kind"],
            )
        )

    if applied:
        # Per-date exceptions of this shift inside each new vigencia stay in
        # force over the new schedule. Past ones are not "kept" over anything:
        # list from today on (schedule_service's clock seam).
        rows = await exceptions_crud.exceptions_from(
            conn,
            [r.device_id for r in applied],
            max(req.valid_from, schedule_module._today_santiago()),
        )
        for result in applied:
            result.kept_exceptions.extend(
                kept_exception_entries(
                    result.label,
                    [r for r in rows if r["device_id"] == result.device_id],
                    req.shift_type,
                    result.new_valid_to,
                )
            )

    return applied, skipped


async def preview_bulk(
    pool: asyncpg.Pool, req: BulkScheduleRequest, refs: Dict[int, DeviceRef]
) -> BulkScheduleResponse:
    """What applying would do, computed on a transaction that always rolls back."""
    _validate(req)
    outcome: Dict[str, Any] = {}
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                outcome["applied"], outcome["skipped"] = await _run(conn, req, refs)
                raise _PreviewRollback()
        except _PreviewRollback:
            pass
    return BulkScheduleResponse(
        operation_id=None,
        valid_from=req.valid_from,
        applied=outcome["applied"],
        skipped=outcome["skipped"],
    )


def _event_device(result: BulkDeviceResult, shift_type: str) -> Dict[str, Any]:
    # A bulk edit never shortens a vigencia: the new row inherits the old end.
    return {
        "id": result.device_id,
        "label": result.label,
        "shift_type": shift_type,
        "regular": None,
        "result": None,
        "replaced": None,
        "days": result.days,
        "validity_shortened": None,
    }


async def apply_bulk(
    pool: asyncpg.Pool,
    req: BulkScheduleRequest,
    refs: Dict[int, DeviceRef],
    actor: Optional[Actor],
) -> BulkScheduleResponse:
    """Apply the schedule and enqueue one email event, all in one transaction."""
    _validate(req)
    operation_id: UUID = uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            applied, skipped = await _run(conn, req, refs)
            if applied:
                event = build_recurring_change_event(
                    company_id=refs[applied[0].device_id].company_id,
                    operation_id=operation_id,
                    changed_at=datetime.now(timezone.utc),
                    actor=actor,
                    valid_from=req.valid_from,
                    devices=[_event_device(r, req.shift_type) for r in applied],
                    skipped=[
                        {"id": r.device_id, "label": r.label, "reason": r.skipped}
                        for r in skipped
                    ],
                    kept_exceptions=[e for r in applied for e in r.kept_exceptions],
                )
                # None when no machine's weekdays actually changed
                await notification_outbox_crud.insert_if_present(conn, event)

    if not applied:
        logger.info(f"Bulk schedule from {req.valid_from}: every machine skipped, nothing written")
        return BulkScheduleResponse(
            operation_id=None, valid_from=req.valid_from, applied=[], skipped=skipped
        )
    logger.info(
        f"Bulk schedule {operation_id} from {req.valid_from}: "
        f"{len(applied)} applied, {len(skipped)} skipped"
    )
    return BulkScheduleResponse(
        operation_id=operation_id, valid_from=req.valid_from, applied=applied, skipped=skipped
    )
