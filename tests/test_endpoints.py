"""
Integration tests for the schedule router endpoints.

Uses httpx AsyncClient with dependency overrides so no real DB is needed.
The service layer's CRUD calls are patched to return synthetic data.
"""

from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core import actor as actor_module
from app.core.actor import get_actor
from app.core.dependencies import get_db_pool, verify_api_key
from app.models.notification_event import Actor
from app.main import app
from tests.conftest import make_db_record, make_extra_hours_json, make_special_days_json

CRUD_PATH = "app.services.schedule_service.schedule_crud"
BASE = "http://test/shifts-api/v1/schedules"


@pytest.fixture
def mock_pool():
    return AsyncMock()


@pytest.fixture
async def client(mock_pool):
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: mock_pool

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=BASE,
    ) as c:
        yield c

    app.dependency_overrides.clear()


# ==================== POST / (create) ====================


class TestCreateSchedule:
    @pytest.mark.asyncio
    async def test_create_success(self, client):
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.post(
                "/",
                json={
                    "deviceId": 1,
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "breaks": [{"start": "12:00", "durationMinutes": 60}],
                        }
                    },
                    "validFrom": "2025-01-01",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["deviceId"] == 1
        assert body["validFrom"] == "2025-01-01"

    @pytest.mark.asyncio
    async def test_create_with_device_name(self, client):
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_device_id_by_name", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.post(
                "/",
                json={
                    "deviceName": "1103",
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "breaks": [{"start": "12:00", "durationMinutes": 60}],
                        }
                    },
                    "validFrom": "2025-01-01",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["deviceId"] == 1

    @pytest.mark.asyncio
    async def test_create_device_name_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_device_id_by_name", new_callable=AsyncMock, return_value=None):
            resp = await client.post(
                "/",
                json={
                    "deviceName": "nonexistent",
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "breaks": [{"start": "12:00", "durationMinutes": 60}],
                        }
                    },
                    "validFrom": "2025-01-01",
                },
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_missing_valid_from(self, client):
        resp = await client.post(
            "/",
            json={
                "deviceId": 1,
                "schedule": {
                    "monday": {
                        "workHours": {"start": "08:00", "end": "17:00"},
                        "breaks": [{"start": "12:00", "durationMinutes": 60}],
                    }
                },
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_without_breaks(self, client):
        rec = make_db_record(device_id=1, days=["monday"], include_break=False)
        with patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.post(
                "/",
                json={
                    "deviceId": 1,
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                        }
                    },
                    "validFrom": "2025-01-01",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["schedule"]["monday"]["breaks"] is None

    @pytest.mark.asyncio
    async def test_create_with_multiple_breaks(self, client):
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.post(
                "/",
                json={
                    "deviceId": 1,
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "breaks": [
                                {"start": "10:00", "durationMinutes": 15},
                                {"start": "12:00", "durationMinutes": 60},
                            ],
                        }
                    },
                    "validFrom": "2025-01-01",
                },
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_create_legacy_break_format(self, client):
        """Legacy single-object 'break' key should still be accepted."""
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.post(
                "/",
                json={
                    "deviceId": 1,
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "08:00", "end": "17:00"},
                            "break": {"start": "12:00", "durationMinutes": 60},
                        }
                    },
                    "validFrom": "2025-01-01",
                },
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_create_invalid_payload(self, client):
        resp = await client.post("/", json={"deviceId": -1, "schedule": {}, "validFrom": "2025-01-01"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_bounded_uses_split(self, client, mock_pool, create_payload, sample_record):
        """POST with validTo should call create_with_split instead of create_with_auto_close."""
        create_payload["validTo"] = "2026-04-18"

        with patch(f"{CRUD_PATH}.get_device_id_by_name", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.create_with_split", new_callable=AsyncMock, return_value=1) as mock_split, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=sample_record):
            resp = await client.post("/", json=create_payload)

        assert resp.status_code == 200
        mock_split.assert_called_once()


# ==================== GET / (list all current) ====================


class TestGetAllSchedules:
    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        with patch(f"{CRUD_PATH}.get_all_current", new_callable=AsyncMock, return_value=[]):
            resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_multiple(self, client):
        recs = [make_db_record(id=i, device_id=i) for i in range(1, 4)]
        with patch(f"{CRUD_PATH}.get_all_current", new_callable=AsyncMock, return_value=recs):
            resp = await client.get("/")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


# ==================== GET /{device_id} (single) ====================


class TestGetSchedule:
    @pytest.mark.asyncio
    async def test_found_all_shift_types(self, client):
        rec = make_db_record(device_id=2)
        with patch(f"{CRUD_PATH}.get_all_current_by_device_id", new_callable=AsyncMock, return_value=[rec]):
            resp = await client.get("/2")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["deviceId"] == 2

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_all_current_by_device_id", new_callable=AsyncMock, return_value=[]):
            resp = await client.get("/999")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_with_shift_type_filter(self, client):
        rec = make_db_record(device_id=2)
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.get("/2?shiftType=day")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["deviceId"] == 2

    @pytest.mark.asyncio
    async def test_with_date_query(self, client):
        rec = make_db_record(device_id=2)
        with patch(f"{CRUD_PATH}.get_all_by_device_id_and_date", new_callable=AsyncMock, return_value=[rec]):
            resp = await client.get("/2?date=2025-06-15")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["deviceId"] == 2


# ==================== GET /{device_id}/history ====================


class TestGetScheduleHistory:
    @pytest.mark.asyncio
    async def test_history_empty(self, client):
        with patch(f"{CRUD_PATH}.get_all_by_device_id", new_callable=AsyncMock, return_value=[]):
            resp = await client.get("/1/history")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_history_multiple(self, client):
        from datetime import date
        recs = [
            make_db_record(id=1, device_id=1, valid_from=date(2025, 1, 1), valid_to=date(2025, 6, 30)),
            make_db_record(id=2, device_id=1, valid_from=date(2025, 7, 1)),
        ]
        with patch(f"{CRUD_PATH}.get_all_by_device_id", new_callable=AsyncMock, return_value=recs):
            resp = await client.get("/1/history")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


# ==================== PUT /{device_id} (update) ====================


class TestUpdateSchedule:
    @pytest.mark.asyncio
    async def test_update_success(self, client):
        existing = make_db_record(device_id=1)
        updated = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=updated):
            resp = await client.put(
                "/1",
                json={
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "09:00", "end": "18:00"},
                            "breaks": [{"start": "13:00", "durationMinutes": 45}],
                        }
                    }
                },
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_update_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.put(
                "/999",
                json={
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "09:00", "end": "18:00"},
                            "breaks": [{"start": "13:00", "durationMinutes": 45}],
                        }
                    }
                },
            )
        assert resp.status_code == 404


# ==================== PATCH /{device_id} (partial) ====================


class TestPatchSchedule:
    @pytest.mark.asyncio
    async def test_patch_success(self, client):
        existing = make_db_record(device_id=1)
        updated = make_db_record(device_id=1, version="2.0")
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=updated):
            resp = await client.patch("/1", json={"metadata": {"version": "2.0"}})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.patch("/999", json={"metadata": {"version": "2.0"}})
        assert resp.status_code == 404


# ==================== DELETE /{device_id} ====================


class TestDeleteSchedule:
    @pytest.mark.asyncio
    async def test_delete_success(self, client, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.delete_current_by_device_id", new_callable=AsyncMock, return_value=True):
            resp = await client.delete("/1")
        assert resp.status_code == 200
        assert "message" in resp.json()

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.delete("/999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_by_schedule_id(self, client, sample_record):
        with patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.delete_by_id", new_callable=AsyncMock, return_value=True):
            resp = await client.delete("/1?scheduleId=42")
        assert resp.status_code == 200


# ==================== GET /by-day/{day} ====================


class TestGetByDay:
    @pytest.mark.asyncio
    async def test_valid_day(self, client):
        recs = [make_db_record()]
        with patch(f"{CRUD_PATH}.get_by_day", new_callable=AsyncMock, return_value=recs):
            resp = await client.get("/by-day/monday")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_invalid_day(self, client):
        resp = await client.get("/by-day/notaday")
        assert resp.status_code == 400


# ==================== GET /stats/all ====================


class TestStatsAll:
    @pytest.mark.asyncio
    async def test_stats_all(self, client):
        recs = [make_db_record(id=1, device_id=1)]
        with patch(f"{CRUD_PATH}.get_all_current", new_callable=AsyncMock, return_value=recs):
            resp = await client.get("/stats/all")
        assert resp.status_code == 200
        body = resp.json()
        assert "requestTime" in body
        assert len(body["devices"]) == 1


# ==================== GET /stats/{device_id} ====================


class TestStatsDevice:
    @pytest.mark.asyncio
    async def test_stats_found(self, client):
        rec = make_db_record(device_id=1)
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.get("/stats/1")
        assert resp.status_code == 200
        assert "deviceStats" in resp.json()

    @pytest.mark.asyncio
    async def test_stats_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/stats/999")
        assert resp.status_code == 404


# ==================== Special days endpoints ====================


class TestSpecialDaysEndpoints:
    @pytest.mark.asyncio
    async def test_get_special_days(self, client):
        with patch(f"{CRUD_PATH}.get_special_days", new_callable=AsyncMock, return_value={}):
            resp = await client.get("/special-days/1")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_special_days_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_special_days", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/special-days/999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_add_special_day(self, client):
        existing = make_db_record(device_id=1)
        updated = make_db_record(
            device_id=1,
            special_days=make_special_days_json({
                "2025-12-25": {
                    "name": "Navidad", "type": "holiday",
                    "workHours": None, "breaks": None,
                    "isRecurring": False, "recurrencePattern": None,
                }
            }),
        )
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True), \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=updated):
            resp = await client.post(
                "/special-days/1?date=2025-12-25",
                json={"name": "Navidad", "type": "holiday"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_special_day(self, client):
        rec = make_db_record(
            device_id=1,
            special_days=make_special_days_json({
                "2025-12-25": {
                    "name": "Navidad", "type": "holiday",
                    "workHours": None, "breaks": None,
                    "isRecurring": False, "recurrencePattern": None,
                }
            }),
        )
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=rec), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True):
            resp = await client.delete("/special-days/1/2025-12-25")
        assert resp.status_code == 200
        assert "message" in resp.json()


# ==================== GET /effective-schedule/{device_id}/{date} ====================


class TestEffectiveSchedule:
    @pytest.mark.asyncio
    async def test_regular_day(self, client):
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_by_device_id_and_date", new_callable=AsyncMock, return_value=rec):
            # 2025-01-13 is a Monday
            resp = await client.get("/effective-schedule/1/2025-01-13")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workHours"]["start"] == "08:00"

    @pytest.mark.asyncio
    async def test_non_work_day(self, client):
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_by_device_id_and_date", new_callable=AsyncMock, return_value=rec):
            # 2025-01-12 is a Sunday
            resp = await client.get("/effective-schedule/1/2025-01-12")
        assert resp.status_code == 200
        assert resp.json() is None

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_by_device_id_and_date", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/effective-schedule/999/2025-01-13")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_date(self, client):
        resp = await client.get("/effective-schedule/1/bad-date")
        assert resp.status_code == 400


# ==================== Pilot mirrors are read-only ====================


@pytest.fixture
def mirrored(stub_mirror_check):
    """Make the device under test a pilot whose schedule the engine mirrors."""
    stub_mirror_check.return_value = True
    return stub_mirror_check


class TestMirroredScheduleIsReadOnly:
    """status-engine rewrites a pilot's schedule from its source every run, so
    an edit here would be sent as an email and then silently reverted. Refuse it
    before anything is written or enqueued."""

    @staticmethod
    def assert_refused(resp):
        assert resp.status_code == 409
        assert "piloto" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_refused(self, client, mirrored):
        with patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock) as write, \
             patch(f"{CRUD_PATH}.create_with_split", new_callable=AsyncMock) as split:
            resp = await client.post(
                "/",
                json={
                    "deviceId": 1,
                    "schedule": {"monday": {"workHours": {"start": "08:00", "end": "17:00"}}},
                    "validFrom": "2025-01-01",
                },
            )
        self.assert_refused(resp)
        write.assert_not_called()
        split.assert_not_called()
        mirrored.assert_awaited_with(ANY, 1)

    @pytest.mark.asyncio
    async def test_update_refused(self, client, mirrored, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write:
            resp = await client.put(
                "/1",
                json={"schedule": {"monday": {"workHours": {"start": "09:00", "end": "18:00"}}}},
            )
        self.assert_refused(resp)
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_patch_refused(self, client, mirrored, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write:
            resp = await client.patch("/1", json={"metadata": {"version": "2.0"}})
        self.assert_refused(resp)
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_refused(self, client, mirrored, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.delete_current_by_device_id", new_callable=AsyncMock) as write:
            resp = await client.delete("/1")
        self.assert_refused(resp)
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_by_schedule_id_checks_the_rows_own_device(self, client, mirrored):
        # The path says device 1, but scheduleId points at device 77's row: the
        # guard must judge the row being deleted, not the URL.
        row = make_db_record(id=42, device_id=77)
        with patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=row), \
             patch(f"{CRUD_PATH}.delete_by_id", new_callable=AsyncMock) as write:
            resp = await client.delete("/1?scheduleId=42")
        self.assert_refused(resp)
        write.assert_not_called()
        mirrored.assert_awaited_with(ANY, 77)

    @pytest.mark.asyncio
    async def test_add_special_day_refused(self, client, mirrored, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write:
            resp = await client.post(
                "/special-days/1?date=2025-12-25",
                json={"name": "Navidad", "type": "holiday"},
            )
        self.assert_refused(resp)
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_special_day_refused(self, client, mirrored):
        rec = make_db_record(
            device_id=1,
            special_days=make_special_days_json({
                "2025-12-25": {
                    "name": "Navidad", "type": "holiday",
                    "workHours": None, "breaks": None,
                    "isRecurring": False, "recurrencePattern": None,
                }
            }),
        )
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=rec), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write:
            resp = await client.delete("/special-days/1/2025-12-25")
        self.assert_refused(resp)
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_reads_still_work(self, client, mirrored, sample_record):
        with patch(f"{CRUD_PATH}.get_all_current_by_device_id", new_callable=AsyncMock, return_value=[sample_record]):
            resp = await client.get("/1")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_missing_schedule_is_still_404(self, client, mirrored):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.put(
                "/999",
                json={"schedule": {"monday": {"workHours": {"start": "09:00", "end": "18:00"}}}},
            )
        assert resp.status_code == 404


# ==================== Who made the change ====================


ANA = Actor(email="ana@planta.cl", name="Ana Pérez")
HOURS = {"schedule": {"monday": {"workHours": {"start": "09:00", "end": "18:00"}}}}


@pytest.fixture
def as_ana():
    app.dependency_overrides[get_actor] = lambda: ANA
    yield
    app.dependency_overrides.pop(get_actor, None)


class TestActorReachesTheEvent:
    """Each notifying route hands the verified actor to its outbox event."""

    @pytest.mark.asyncio
    async def test_create(self, client, as_ana):
        with patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1) as write, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=make_db_record()):
            resp = await client.post("/", json={"deviceId": 1, **HOURS, "validFrom": "2025-01-01"})
        assert resp.status_code == 200
        assert write.call_args.args[2].payload["actor"] == ANA.model_dump()

    @pytest.mark.asyncio
    async def test_update(self, client, as_ana, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=sample_record):
            resp = await client.put("/1", json=HOURS)
        assert resp.status_code == 200
        assert write.call_args.args[3].payload["actor"] == ANA.model_dump()

    @pytest.mark.asyncio
    async def test_patch(self, client, as_ana, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=sample_record):
            resp = await client.patch("/1", json=HOURS)
        assert resp.status_code == 200
        assert write.call_args.args[3].payload["actor"] == ANA.model_dump()

    @pytest.mark.asyncio
    async def test_delete(self, client, as_ana, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.delete_current_by_device_id", new_callable=AsyncMock, return_value=True) as write:
            resp = await client.delete("/1")
        assert resp.status_code == 200
        assert write.call_args.args[3].payload["actor"] == ANA.model_dump()


class TestSaveNeverDependsOnAuthApi:
    """The requirement that matters: identity is best-effort, the write is not."""

    @pytest.mark.asyncio
    async def test_update_succeeds_when_auth_api_is_unreachable(self, client, sample_record):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        with patch.object(actor_module.settings, "AUTH_API_URL", "http://auth.test"), \
             patch.object(actor_module, "_transport", httpx.MockTransport(refuse)), \
             patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=sample_record):
            resp = await client.put("/1", json=HOURS, headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert write.call_args.args[3].payload["actor"] is None

    @pytest.mark.asyncio
    async def test_update_succeeds_when_the_token_is_rejected(self, client, sample_record):
        with patch.object(actor_module.settings, "AUTH_API_URL", "http://auth.test"), \
             patch.object(actor_module, "_transport", httpx.MockTransport(lambda r: httpx.Response(401))), \
             patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=sample_record):
            resp = await client.put("/1", json=HOURS, headers={"Authorization": "Bearer expired"})
        assert resp.status_code == 200
        assert write.call_args.args[3].payload["actor"] is None

    @pytest.mark.asyncio
    async def test_update_without_a_bearer_still_works(self, client, sample_record):
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=sample_record), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock) as write, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=sample_record):
            resp = await client.put("/1", json=HOURS)
        assert resp.status_code == 200
        assert write.call_args.args[3].payload["actor"] is None


# ==================== Auth ====================


class TestAuth:
    @pytest.mark.asyncio
    async def test_missing_api_key(self, mock_pool):
        """Without the override, the real verify_api_key should reject."""
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides[get_db_pool] = lambda: mock_pool

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=BASE,
        ) as c:
            resp = await c.get("/")

        assert resp.status_code == 422 or resp.status_code == 401

        app.dependency_overrides.clear()
