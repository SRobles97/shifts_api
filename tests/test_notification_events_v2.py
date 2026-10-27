"""
Unit tests for app.services.notification_events_v2.

The builders are pure. The expected dicts mirror the example payloads of
notify_api's tests/test_shift_change_message_v2.py (the renderer), so a change
to either side's shape fails here.
"""

import json
from datetime import date, datetime, timezone
from uuid import UUID

from app.models.notification_event import NOTIFICATION_TYPE_SHIFT_CHANGE, Actor
from app.services.notification_events_v2 import (
    build_day_change_event,
    build_recurring_change_event,
)

CHANGED_AT = datetime(2026, 10, 22, 17, 5, tzinfo=timezone.utc)
ACTOR = Actor(email="ana@planta.cl", name="Ana Pérez")
OP_ID = UUID("11111111-1111-1111-1111-111111111111")


def _dev(i, label, regular, result, shift="day", **extra):
    return {
        "id": i, "label": label, "shift_type": shift, "regular": regular,
        "result": result, "replaced": None, "days": [], "validity_shortened": None,
        **extra,
    }


def _extra_hours_kwargs():
    return dict(
        company_id=5,
        operation_id=OP_ID,
        changed_at=CHANGED_AT,
        actor=ACTOR,
        day=date(2026, 10, 23),
        change={"kind": "extra_hours", "minutes": 120, "position": "after",
                "work_hours": None, "breaks": None, "reason": ""},
        devices=[
            _dev(1, "Torno CNC 1", "08:00–17:00", "08:00–17:00 + 17:00–19:00"),
            _dev(2, "Torno CNC 2", "07:00–16:00", "07:00–16:00 + 16:00–18:00"),
        ],
        skipped=[{"id": 3, "label": "Fresadora 3", "reason": "no_shift_that_day"}],
    )


def _recurring_kwargs():
    return dict(
        company_id=5,
        operation_id=UUID("33333333-3333-3333-3333-333333333333"),
        changed_at=datetime(2026, 10, 22, 19, 40, tzinfo=timezone.utc),
        actor=ACTOR,
        valid_from=date(2026, 11, 3),
        devices=[
            _dev(1, "Torno CNC 1", None, None, days=[
                {"day": "monday", "before": "08:00–17:00", "after": "07:00–16:00", "change": "modified"},
                {"day": "friday", "before": None, "after": "+2 h extras (16:00–18:00)", "change": "added"},
            ]),
            _dev(2, "Torno CNC 2", None, None,
                 validity_shortened={"before_to": None, "after_to": "2026-11-30"}),
        ],
        skipped=[],
        kept_exceptions=[{"device_label": "Torno CNC 1", "date": "2026-11-24", "summary": "+1 h extra"}],
    )


def test_day_change_payload_shape():
    event = build_day_change_event(**_extra_hours_kwargs())
    assert event.type_key == NOTIFICATION_TYPE_SHIFT_CHANGE
    assert event.company_id == 5
    assert event.payload == {
        "version": 2,
        "operation": "day_change",
        "operation_id": "11111111-1111-1111-1111-111111111111",
        "changed_at": "2026-10-22T17:05:00+00:00",
        "actor": {"email": "ana@planta.cl", "name": "Ana Pérez"},
        "source": "ui",
        "date": "2026-10-23",
        "change": {"kind": "extra_hours", "minutes": 120, "position": "after",
                   "work_hours": None, "breaks": None, "reason": ""},
        "devices": _extra_hours_kwargs()["devices"],
        "skipped": [{"id": 3, "label": "Fresadora 3", "reason": "no_shift_that_day"}],
        "kept_exceptions": [],
    }

    # no_work example: custom reason, no skipped, null position.
    kw = _extra_hours_kwargs()
    kw.update(
        day=date(2026, 11, 2),
        change={"kind": "no_work", "minutes": 0, "position": None, "work_hours": None,
                "breaks": None, "reason": "Mantención general"},
        devices=[_dev(1, "Máquina 1", "08:00–17:00", "Sin trabajo")],
        skipped=[],
    )
    payload = build_day_change_event(**kw).payload
    assert payload["date"] == "2026-11-02"
    assert payload["change"]["reason"] == "Mantención general"
    assert payload["skipped"] == []

    # recurring example.
    rec = build_recurring_change_event(**_recurring_kwargs())
    p = rec.payload
    assert p["operation"] == "recurring_change"
    assert p["valid_from"] == "2026-11-03"
    assert "date" not in p and "change" not in p
    assert p["source"] == "ui" and p["version"] == 2
    assert p["kept_exceptions"] == [
        {"device_label": "Torno CNC 1", "date": "2026-11-24", "summary": "+1 h extra"}
    ]
    assert p["devices"][1]["validity_shortened"] == {"before_to": None, "after_to": "2026-11-30"}
    for d in p["devices"]:
        assert set(d) == {"id", "label", "shift_type", "regular", "result",
                          "replaced", "days", "validity_shortened"}


def test_removed_operation_label():
    kw = _extra_hours_kwargs()
    event = build_day_change_event(**kw, removed=True)
    assert event.payload["operation"] == "day_change_removed"
    assert event.payload["date"] == "2026-10-23"


def test_recurring_change_none_when_no_changes():
    kw = _recurring_kwargs()
    kw["devices"] = [_dev(1, "A", None, None), _dev(2, "B", None, None)]
    assert build_recurring_change_event(**kw) is None
    # One machine with only a validity change is enough.
    kw["devices"][1]["validity_shortened"] = {"before_to": None, "after_to": "2026-11-30"}
    assert build_recurring_change_event(**kw) is not None


def test_dedupe_key_is_operation_id():
    assert build_day_change_event(**_extra_hours_kwargs()).dedupe_key == f"shift_change:{OP_ID}"
    rec = build_recurring_change_event(**_recurring_kwargs())
    assert rec.dedupe_key == "shift_change:33333333-3333-3333-3333-333333333333"


def test_payload_is_json_serialisable_and_actor_optional():
    kw = _extra_hours_kwargs()
    kw["actor"] = None
    event = build_day_change_event(**kw)
    assert event.payload["actor"] is None
    json.dumps(event.payload)
    json.dumps(build_recurring_change_event(**_recurring_kwargs()).payload)


def test_naive_changed_at_is_treated_as_utc():
    kw = _extra_hours_kwargs()
    kw["changed_at"] = datetime(2026, 10, 22, 17, 5)
    assert build_day_change_event(**kw).payload["changed_at"] == "2026-10-22T17:05:00+00:00"


# --- weekday diff with extra hours ---------------------------------------


def test_diff_days_extras_in_chronological_order_across_midnight():
    from app.models.notification_event import DayHours
    from app.services.notification_events_v2 import _diff_days

    night = {"friday": DayHours(start="22:00", end="06:00")}
    days = _diff_days(night, night, {}, {"friday": [("06:00", "07:00"), ("20:00", "22:00")]})
    assert days == [
        {
            "day": "friday",
            "before": "22:00–06:00",
            "after": "20:00–22:00 + 22:00–06:00 + 06:00–07:00",
            "change": "modified",
        }
    ]


def test_diff_days_without_extras_ignores_them():
    from app.models.notification_event import DayHours
    from app.services.notification_events_v2 import _diff_days

    day = {"monday": DayHours(start="08:00", end="17:00")}
    assert _diff_days(day, day) == []


def test_diff_days_extras_only_day_in_clock_order():
    from app.services.notification_events_v2 import _diff_days

    days = _diff_days({}, {}, {}, {"saturday": [("17:00", "19:00"), ("06:00", "08:00")]})
    assert days == [
        {
            "day": "saturday",
            "before": None,
            "after": "06:00–08:00 + 17:00–19:00",
            "change": "added",
        }
    ]
