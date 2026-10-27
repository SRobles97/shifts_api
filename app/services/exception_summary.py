"""
Spanish one-line summaries of per-date exceptions, and the "se mantienen" list
a regular-schedule edit reports.

Kept apart from `day_change_service` because the regular-schedule writers
(`schedule_service`, `bulk_schedule_service`) need it too and
`day_change_service` already imports `schedule_service`.
"""

import json
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional

NO_WORK_LABEL = "Sin trabajo"
_DASH = "–"
_POSITION_LABELS = {"after": "después", "before": "antes"}


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


def kept_exception_entries(
    label: str,
    rows: Iterable[Mapping[str, Any]],
    shift_type: str,
    valid_to: Optional[date],
) -> List[Dict[str, Any]]:
    """The exceptions of one machine that stay in force over a new schedule.

    `rows` are the machine's exceptions from the new vigencia start onwards.
    Only the edited shift counts, and nothing past `valid_to`: beyond it the
    exception sits on a later row this edit did not touch.
    """
    return [
        {
            "device_label": label,
            "date": row["date"].isoformat(),
            "summary": describe_exception(row),
        }
        for row in rows
        if row["shift_type"] == shift_type and (valid_to is None or row["date"] <= valid_to)
    ]
