"""
"Cambiar un día": preview and apply one per-date change over several machines.

Both paths run the same core (`_run`) inside a transaction. Each machine is
written inside its own SAVEPOINT and judged against the effective schedule the
SQL functions compute with the change in place; a machine that cannot take the
change rolls back to its savepoint, so skipping it never undoes the others.
The preview raises a private sentinel to discard everything; the apply commits
and enqueues a single v2 outbox event for the whole operation.
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
from loguru import logger

from ..models.notification_event import Actor, DeviceRef
from ..repositories.exceptions import exceptions_crud
from ..repositories.notification_outbox import notification_outbox_crud
from ..schemas.day_changes import DayChangeDeviceResult, DayChangeRequest, DayChangeResponse
from .notification_events_v2 import build_day_change_event

DEFAULT_TZ = "America/Santiago"
NO_WORK_LABEL = "Sin trabajo"
_DASH = "–"
_POSITION_LABELS = {"after": "después", "before": "antes"}


class _PreviewRollback(Exception):
    """Raised inside the preview transaction only to force its rollback."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _hhmm(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def format_blocks(rows: Iterable[Mapping[str, Any]], tz_name: str) -> Optional[str]:
    """Blocks as "HH:MM–HH:MM" in the machine's zone; None when there are none.

    All blocks in start order. Two consecutive regular pieces are separated by
    " / "; any boundary touching an extra uses " + ". So a day with a break and
    an extra after reads "08:00–12:00 / 13:00–17:00 + 17:00–19:00", and an extra
    before reads "04:30–06:30 + 06:30–15:00".
    """
    tz = ZoneInfo(tz_name)
    out = ""
    prev_kind = None
    for r in sorted(rows, key=lambda r: (r["block_start"], r["block_end"])):
        if prev_kind is not None:
            both_regular = prev_kind == "regular" and r["kind"] == "regular"
            out += " / " if both_regular else " + "
        out += f"{_hhmm(r['block_start'], tz)}{_DASH}{_hhmm(r['block_end'], tz)}"
        prev_kind = r["kind"]
    return out or None


def describe_exception(row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Short Spanish summary of a per-date exception, for "replaced"."""
    if row is None:
        return None
    kind = row["kind"]
    if kind == "extra_hours":
        minutes = row["extra_minutes"] or 0
        position = _POSITION_LABELS.get(row["extra_position"], row["extra_position"] or "")
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"+{hours} h {'extra' if hours == 1 else 'extras'} {position}"
        return f"+{minutes} min extra {position}"
    if kind == "custom_hours":
        hours = row["work_hours"]
        if isinstance(hours, str):
            hours = json.loads(hours)
        if not hours:
            return None
        return f"{hours.get('start')}{_DASH}{hours.get('end')}"
    if kind == "no_work":
        return NO_WORK_LABEL
    return kind


def _of_shift(rows, device_id: int, shift_type: str, day: date) -> List[Mapping[str, Any]]:
    return [
        r
        for r in rows
        if r["device_id"] == device_id and r["shift_type"] == shift_type and r["shift_date"] == day
    ]


def _extra_minutes(rows) -> float:
    return sum(
        (r["block_end"] - r["block_start"]).total_seconds() / 60
        for r in rows
        if r["kind"] == "extra"
    )


def _minutes(rows) -> float:
    return sum((r["block_end"] - r["block_start"]).total_seconds() / 60 for r in rows)


def _overlaps(a, b) -> bool:
    return a["block_start"] < b["block_end"] and b["block_start"] < a["block_end"]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


async def _run(
    conn: asyncpg.Connection,
    req: DayChangeRequest,
    refs: Dict[int, DeviceRef],
    operation_id: UUID,
) -> Tuple[List[DayChangeDeviceResult], List[DayChangeDeviceResult]]:
    """Apply the change machine by machine on an open transaction.

    Returns (applied, skipped) in request order. The caller decides whether the
    transaction commits.
    """
    ids = list(req.device_ids)
    day, shift_type, change = req.date, req.shift_type, req.change
    # Through the next day too: custom hours past midnight can reach a shift
    # that starts on day + 1. Only shift_date == day is ever reported.
    window_end = day + timedelta(days=1)

    zones = await exceptions_crud.timezones(conn, ids)
    before = await exceptions_crud.blocks(conn, ids, day, window_end)
    existing = await exceptions_crud.get_many(conn, ids, shift_type, day)
    pilots = await exceptions_crud.mirrored_ids(conn, ids)

    applied: List[DayChangeDeviceResult] = []
    skipped: List[DayChangeDeviceResult] = []

    for device_id in ids:
        ref = refs[device_id]
        tz = zones.get(device_id, DEFAULT_TZ)
        base = DayChangeDeviceResult(device_id=device_id, label=ref.label, shift_type=shift_type)

        if device_id in pilots:
            base.regular = format_blocks(_of_shift(before, device_id, shift_type, day), tz)
            base.skipped = "pilot"
            skipped.append(base)
            continue

        current = existing.get(device_id)
        savepoint = conn.transaction()
        await savepoint.start()
        try:
            if current is not None:
                await exceptions_crud.delete(conn, current["id"])
            baseline = await exceptions_crud.blocks(conn, [device_id], day, window_end)
            await exceptions_crud.upsert(conn, device_id, shift_type, day, change, operation_id)
            if current is not None and current["source"] == "migrated":
                await exceptions_crud.strip_jsonb_key(conn, device_id, day)
            after = await exceptions_crud.blocks(conn, [device_id], day, window_end)
            days = await exceptions_crud.days(conn, [device_id], day, day)

            base_shift = _of_shift(baseline, device_id, shift_type, day)
            after_shift = _of_shift(after, device_id, shift_type, day)
            base.regular = format_blocks(base_shift, tz)

            base_others = [r for r in baseline if r["shift_type"] != shift_type]
            after_others = [r for r in after if r["shift_type"] != shift_type]

            reason = None
            if change.kind == "extra_hours" and any(
                r["orphan_extra"]
                for r in days
                if r["shift_type"] == shift_type and r["shift_date"] == day
            ):
                reason = "no_shift_that_day"
            elif _minutes(after_others) < _minutes(base_others):
                # the change took time from another shift: the SQL function
                # clips every extra against all regulars and earlier extras
                reason = "overlaps_other_shift"
            elif change.kind == "extra_hours":
                if _extra_minutes(after_shift) - _extra_minutes(base_shift) < change.minutes:
                    # the SQL function clipped the extra against another shift
                    reason = "overlaps_other_shift"
            elif change.kind == "custom_hours":
                new_regular = [r for r in after_shift if r["kind"] == "regular"]
                if any(_overlaps(n, o) for n in new_regular for o in base_others):
                    reason = "overlaps_other_shift"
        except BaseException:
            await savepoint.rollback()
            raise

        if reason is not None:
            await savepoint.rollback()
            base.skipped = reason
            skipped.append(base)
            continue

        await savepoint.commit()
        base.replaced = describe_exception(current)
        result = format_blocks(after_shift, tz)
        if result is None and change.kind == "no_work":
            result = NO_WORK_LABEL
        base.result = result
        applied.append(base)

    return applied, skipped


async def preview_day_change(
    pool: asyncpg.Pool, req: DayChangeRequest, refs: Dict[int, DeviceRef]
) -> DayChangeResponse:
    """What applying would do, computed on a transaction that always rolls back."""
    outcome: Dict[str, Any] = {}
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                outcome["applied"], outcome["skipped"] = await _run(conn, req, refs, uuid4())
                raise _PreviewRollback()
        except _PreviewRollback:
            pass
    return DayChangeResponse(
        operation_id=None, date=req.date, applied=outcome["applied"], skipped=outcome["skipped"]
    )


def _event_device(result: DayChangeDeviceResult) -> Dict[str, Any]:
    return {
        "id": result.device_id,
        "label": result.label,
        "shift_type": result.shift_type,
        "regular": result.regular,
        "result": result.result,
        "replaced": result.replaced,
        "days": [],
        "validity_shortened": None,
    }


async def apply_day_change(
    pool: asyncpg.Pool,
    req: DayChangeRequest,
    refs: Dict[int, DeviceRef],
    actor: Optional[Actor],
) -> DayChangeResponse:
    """Apply the change and enqueue one email event, all in one transaction."""
    operation_id = uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            applied, skipped = await _run(conn, req, refs, operation_id)
            if applied:
                company_id = refs[applied[0].device_id].company_id
                event = build_day_change_event(
                    company_id=company_id,
                    operation_id=operation_id,
                    changed_at=datetime.now(timezone.utc),
                    actor=actor,
                    day=req.date,
                    change=req.change.model_dump(mode="json"),
                    devices=[_event_device(r) for r in applied],
                    skipped=[
                        {"id": r.device_id, "label": r.label, "reason": r.skipped}
                        for r in skipped
                    ],
                )
                await notification_outbox_crud.insert_if_present(conn, event)

    if not applied:
        logger.info(f"Day change on {req.date}: every machine skipped, nothing written")
        return DayChangeResponse(operation_id=None, date=req.date, applied=[], skipped=skipped)
    logger.info(
        f"Day change {operation_id} on {req.date}: "
        f"{len(applied)} applied, {len(skipped)} skipped"
    )
    return DayChangeResponse(
        operation_id=operation_id, date=req.date, applied=applied, skipped=skipped
    )
