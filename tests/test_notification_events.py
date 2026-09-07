"""
Unit tests for app.services.notification_events.

The payload builder is pure — no DB, no clock, no network — so these tests
pin the exact shape shifts_api promises to notify_api.
"""

from datetime import date, datetime, timezone

import pytest

from app.models.notification_event import (
    NOTIFICATION_TYPE_SHIFT_CHANGE,
    BreakRef,
    DayHours,
    DeviceRef,
    ScheduleSnapshot,
    ShiftChangeContext,
)
from app.services.notification_events import build_shift_change_event

CHANGED_AT = datetime(2026, 8, 24, 14, 5, 11, 123456, tzinfo=timezone.utc)

DEVICE = DeviceRef(id=42, company_id=3, key="F1", display_name="Fresadora 1")


def day(start: str, end: str, breaks: list[tuple[str, int]] | None = None) -> DayHours:
    return DayHours(
        start=start,
        end=end,
        breaks=[BreakRef(start=s, duration_minutes=d) for s, d in (breaks or [])],
    )


def snapshot(days: dict, **kwargs) -> ScheduleSnapshot:
    defaults = dict(schedule_id=7, valid_from=date(2026, 9, 1), valid_to=None)
    defaults.update(kwargs)
    return ScheduleSnapshot(day_schedules=days, **defaults)


def context(action: str, before=None, after=None, **kwargs) -> ShiftChangeContext:
    return ShiftChangeContext(
        action=action,
        device=kwargs.pop("device", DEVICE),
        shift_type=kwargs.pop("shift_type", "day"),
        changed_at=kwargs.pop("changed_at", CHANGED_AT),
        before=before,
        after=after,
    )


# ==================== Envelope ====================


class TestEnvelope:
    def test_created_event_routes_to_the_devices_company(self):
        event = build_shift_change_event(
            context("created", after=snapshot({"monday": day("08:00", "17:00")}))
        )
        assert event is not None
        assert event.type_key == NOTIFICATION_TYPE_SHIFT_CHANGE
        assert event.company_id == 3

    def test_dedupe_key_identifies_device_shift_action_and_instant(self):
        event = build_shift_change_event(
            context("created", after=snapshot({"monday": day("08:00", "17:00")}))
        )
        assert event.dedupe_key == "shift_change:42:day:created:1787580311123456"

    def test_naive_changed_at_is_read_as_utc(self):
        """Defensive: the service always passes tz-aware, but a naive value
        must not silently shift the key by the local offset."""
        aware = build_shift_change_event(
            context("created", after=snapshot({"monday": day("08:00", "17:00")}))
        )
        naive = build_shift_change_event(
            context(
                "created",
                after=snapshot({"monday": day("08:00", "17:00")}),
                changed_at=CHANGED_AT.replace(tzinfo=None),
            )
        )
        assert naive.dedupe_key == aware.dedupe_key

    def test_dedupe_key_differs_per_action(self):
        after = snapshot({"monday": day("08:00", "17:00")})
        created = build_shift_change_event(context("created", after=after))
        deleted = build_shift_change_event(context("deleted", before=after))
        assert created.dedupe_key != deleted.dedupe_key

    def test_payload_is_json_serialisable(self):
        import json

        event = build_shift_change_event(
            context(
                "updated",
                before=snapshot({"monday": day("08:00", "17:00")}),
                after=snapshot({"monday": day("07:00", "16:00")}),
            )
        )
        assert json.loads(json.dumps(event.payload))["action"] == "updated"

    def test_payload_carries_device_and_validity(self):
        event = build_shift_change_event(
            context("created", after=snapshot({"monday": day("08:00", "17:00")}))
        )
        payload = event.payload
        assert payload["device"] == {
            "id": 42,
            "key": "F1",
            "display_name": "Fresadora 1",
            "company_id": 3,
        }
        assert payload["shift_type"] == "day"
        assert payload["valid_from"] == "2026-09-01"
        assert payload["valid_to"] is None
        assert payload["changed_at"] == "2026-08-24T14:05:11.123456+00:00"


# ==================== Day-level diff ====================


class TestDayDiff:
    def test_create_reports_every_day_as_added(self):
        event = build_shift_change_event(
            context(
                "created",
                after=snapshot(
                    {"monday": day("08:00", "17:00"), "tuesday": day("08:00", "17:00")}
                ),
            )
        )
        assert [d["change"] for d in event.payload["days"]] == ["added", "added"]
        assert event.payload["days"][0] == {
            "day": "monday",
            "before": None,
            "after": "08:00–17:00",
            "change": "added",
        }

    def test_delete_reports_every_day_as_removed(self):
        event = build_shift_change_event(
            context("deleted", before=snapshot({"friday": day("08:00", "17:00")}))
        )
        assert event.payload["days"] == [
            {"day": "friday", "before": "08:00–17:00", "after": None, "change": "removed"}
        ]

    def test_only_changed_days_appear(self):
        before = snapshot(
            {"monday": day("08:00", "17:00"), "tuesday": day("08:00", "17:00")}
        )
        after = snapshot(
            {"monday": day("08:00", "17:00"), "tuesday": day("07:00", "16:00")}
        )
        event = build_shift_change_event(context("updated", before=before, after=after))
        assert [d["day"] for d in event.payload["days"]] == ["tuesday"]

    def test_days_are_reported_in_weekday_order(self):
        before = snapshot({})
        after = snapshot(
            {
                "friday": day("08:00", "17:00"),
                "monday": day("08:00", "17:00"),
                "wednesday": day("08:00", "17:00"),
            }
        )
        event = build_shift_change_event(context("updated", before=before, after=after))
        assert [d["day"] for d in event.payload["days"]] == [
            "monday",
            "wednesday",
            "friday",
        ]

    def test_dropping_a_day_is_reported_as_removed(self):
        before = snapshot(
            {"monday": day("08:00", "17:00"), "saturday": day("08:00", "13:00")}
        )
        after = snapshot({"monday": day("08:00", "17:00")})
        event = build_shift_change_event(context("updated", before=before, after=after))
        assert event.payload["days"] == [
            {
                "day": "saturday",
                "before": "08:00–13:00",
                "after": None,
                "change": "removed",
            }
        ]

    def test_overnight_hours_render_as_written(self):
        event = build_shift_change_event(
            context(
                "created",
                after=snapshot({"monday": day("22:00", "06:00")}),
                shift_type="night",
            )
        )
        assert event.payload["days"][0]["after"] == "22:00–06:00"
        assert event.payload["shift_type"] == "night"


# ==================== Breaks ====================


class TestBreaks:
    def test_break_change_alone_is_a_reportable_change(self):
        before = snapshot({"monday": day("08:00", "17:00", [("12:00", 60)])})
        after = snapshot({"monday": day("08:00", "17:00", [("12:30", 45)])})
        event = build_shift_change_event(context("updated", before=before, after=after))
        assert event is not None
        assert event.payload["breaks_changed"] is True
        entry = event.payload["days"][0]
        assert entry["change"] == "modified"
        assert entry["breaks_before"] == "12:00 (60 min)"
        assert entry["breaks_after"] == "12:30 (45 min)"

    def test_unchanged_breaks_are_not_reported_per_day(self):
        before = snapshot({"monday": day("08:00", "17:00", [("12:00", 60)])})
        after = snapshot({"monday": day("07:00", "16:00", [("12:00", 60)])})
        event = build_shift_change_event(context("updated", before=before, after=after))
        entry = event.payload["days"][0]
        assert "breaks_before" not in entry
        assert event.payload["breaks_changed"] is False

    def test_removing_all_breaks_is_reported(self):
        before = snapshot({"monday": day("08:00", "17:00", [("12:00", 60)])})
        after = snapshot({"monday": day("08:00", "17:00")})
        event = build_shift_change_event(context("updated", before=before, after=after))
        assert event.payload["days"][0]["breaks_after"] == "sin pausas"


# ==================== Validity and special days ====================


class TestValidityAndSpecialDays:
    def test_validity_change_is_flagged(self):
        before = snapshot({"monday": day("08:00", "17:00")}, valid_from=date(2026, 9, 1))
        after = snapshot({"monday": day("08:00", "17:00")}, valid_from=date(2026, 10, 1))
        event = build_shift_change_event(context("updated", before=before, after=after))
        assert event is not None
        assert event.payload["validity_changed"] is True
        assert event.payload["days"] == []

    def test_special_day_count_reflects_the_delta(self):
        before = snapshot({"monday": day("08:00", "17:00")}, special_day_keys=[])
        after = snapshot(
            {"monday": day("08:00", "17:00")}, special_day_keys=["2026-12-25"]
        )
        event = build_shift_change_event(context("updated", before=before, after=after))
        assert event is not None
        assert event.payload["special_days_changed"] == 1


# ==================== No-op suppression ====================


class TestNoOp:
    def test_identical_before_and_after_produces_no_event(self):
        same = {"monday": day("08:00", "17:00", [("12:00", 60)])}
        event = build_shift_change_event(
            context("patched", before=snapshot(same), after=snapshot(same))
        )
        assert event is None

    def test_metadata_only_patch_produces_no_event(self):
        before = snapshot({"monday": day("08:00", "17:00")}, source="ui")
        after = snapshot({"monday": day("08:00", "17:00")}, source="api")
        event = build_shift_change_event(context("patched", before=before, after=after))
        assert event is None

    def test_a_create_is_never_suppressed_even_with_no_days(self):
        event = build_shift_change_event(context("created", after=snapshot({})))
        assert event is not None
        assert event.payload["action"] == "created"


# ==================== Device labelling ====================


class TestDeviceLabel:
    @pytest.mark.parametrize(
        "display_name,key,expected",
        [
            ("Fresadora 1", "F1", "Fresadora 1"),
            ("", "F1", "F1"),
            ("", "", "#42"),
        ],
    )
    def test_label_falls_back_from_display_name_to_key_to_id(
        self, display_name, key, expected
    ):
        device = DeviceRef(id=42, company_id=3, key=key, display_name=display_name)
        event = build_shift_change_event(
            context(
                "created",
                after=snapshot({"monday": day("08:00", "17:00")}),
                device=device,
            )
        )
        assert event.payload["device_label"] == expected
