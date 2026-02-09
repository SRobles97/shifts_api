"""
Integration tests for the schedule router endpoints.

Uses httpx AsyncClient with dependency overrides so no real DB is needed.
The service layer's CRUD calls are patched to return synthetic data.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.dependencies import get_db_pool, verify_api_key
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
        with patch(f"{CRUD_PATH}.upsert", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
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
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["deviceId"] == 1

    @pytest.mark.asyncio
    async def test_create_invalid_payload(self, client):
        resp = await client.post("/", json={"deviceId": -1, "schedule": {}})
        assert resp.status_code == 422


# ==================== GET / (list all) ====================


class TestGetAllSchedules:
    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        with patch(f"{CRUD_PATH}.get_all", new_callable=AsyncMock, return_value=[]):
            resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_multiple(self, client):
        recs = [make_db_record(id=i, device_id=i) for i in range(1, 4)]
        with patch(f"{CRUD_PATH}.get_all", new_callable=AsyncMock, return_value=recs):
            resp = await client.get("/")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


# ==================== GET /{device_id} (single) ====================


class TestGetSchedule:
    @pytest.mark.asyncio
    async def test_found(self, client):
        rec = make_db_record(device_id=2)
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.get("/2")
        assert resp.status_code == 200
        assert resp.json()["deviceId"] == 2

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/999")
        assert resp.status_code == 200
        assert resp.json() is None


# ==================== PUT /{device_id} (update) ====================


class TestUpdateSchedule:
    @pytest.mark.asyncio
    async def test_update_success(self, client):
        existing = make_db_record(device_id=1)
        updated = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, side_effect=[existing, updated]), \
             patch(f"{CRUD_PATH}.upsert", new_callable=AsyncMock, return_value=1):
            resp = await client.put(
                "/1",
                json={
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "09:00", "end": "18:00"},
                            "break": {"start": "13:00", "durationMinutes": 45},
                        }
                    }
                },
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_update_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.put(
                "/999",
                json={
                    "schedule": {
                        "monday": {
                            "workHours": {"start": "09:00", "end": "18:00"},
                            "break": {"start": "13:00", "durationMinutes": 45},
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
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, side_effect=[existing, updated]), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True):
            resp = await client.patch("/1", json={"metadata": {"version": "2.0"}})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.patch("/999", json={"metadata": {"version": "2.0"}})
        assert resp.status_code == 404


# ==================== DELETE /{device_id} ====================


class TestDeleteSchedule:
    @pytest.mark.asyncio
    async def test_delete_success(self, client):
        with patch(f"{CRUD_PATH}.delete_by_device_id", new_callable=AsyncMock, return_value=True):
            resp = await client.delete("/1")
        assert resp.status_code == 200
        assert "message" in resp.json()

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client):
        with patch(f"{CRUD_PATH}.delete_by_device_id", new_callable=AsyncMock, return_value=False):
            resp = await client.delete("/999")
        assert resp.status_code == 404


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
        with patch(f"{CRUD_PATH}.get_all", new_callable=AsyncMock, return_value=recs):
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
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            resp = await client.get("/stats/1")
        assert resp.status_code == 200
        assert "deviceStats" in resp.json()

    @pytest.mark.asyncio
    async def test_stats_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
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
                    "workHours": None, "break": None,
                    "isRecurring": False, "recurrencePattern": None,
                }
            }),
        )
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, side_effect=[existing, updated]), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True):
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
                    "workHours": None, "break": None,
                    "isRecurring": False, "recurrencePattern": None,
                }
            }),
        )
        updated = make_db_record(device_id=1)
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True):
            resp = await client.delete("/special-days/1/2025-12-25")
        assert resp.status_code == 200
        assert "message" in resp.json()


# ==================== GET /effective-schedule/{device_id}/{date} ====================


class TestEffectiveSchedule:
    @pytest.mark.asyncio
    async def test_regular_day(self, client):
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            # 2025-01-13 is a Monday
            resp = await client.get("/effective-schedule/1/2025-01-13")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workHours"]["start"] == "08:00"

    @pytest.mark.asyncio
    async def test_non_work_day(self, client):
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            # 2025-01-12 is a Sunday
            resp = await client.get("/effective-schedule/1/2025-01-12")
        assert resp.status_code == 200
        assert resp.json() is None

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/effective-schedule/999/2025-01-13")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_date(self, client):
        resp = await client.get("/effective-schedule/1/bad-date")
        assert resp.status_code == 400


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
