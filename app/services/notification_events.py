"""
Adapters from stored or requested schedules to the shapes the v2 payload
builders (`notification_events_v2`) diff.

Pure functions only: no database, no clock, no network.
"""

import json
from typing import Any, Dict, List, Mapping

from ..models.notification_event import BreakRef, DayHours, ScheduleSnapshot
from .notification_events_v2 import Extras


# ---------------------------------------------------------------------------
# Snapshot adapters
#
# Two shapes reach this module: rows read back from the DB (JSONB, camelCase)
# and request schemas on the way in. Both collapse to plain DayHours so the
# diff never has to care which side it is looking at.
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


def extras_from_jsonb(value: Any) -> Dict[str, Extras]:
    """extra_hours jsonb (text or dict) as {weekday: [(start, end), ...]}."""
    return {
        day: [(h.get("start"), h.get("end")) for h in (hours or [])]
        for day, hours in _jsonb(value).items()
    }
