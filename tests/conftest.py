"""
Shared fixtures for the test suite.

Provides mock database pool, httpx test client with dependency overrides,
and sample data factories for consistent test data.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.dependencies import get_db_pool, verify_api_key
from app.main import app


# ---------------------------------------------------------------------------
# Sample data helpers
# ---------------------------------------------------------------------------

def make_day_schedules_json(
    days: Optional[List[str]] = None,
    work_start: str = "08:00",
    work_end: str = "17:00",
    break_start: str = "12:00",
    break_duration: int = 60,
) -> str:
    """Return a JSON string matching the DB JSONB column format."""
    if days is None:
        days = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    obj = {}
    for day in days:
        obj[day] = {
            "workHours": {"start": work_start, "end": work_end},
            "break": {"start": break_start, "durationMinutes": break_duration},
        }
    return json.dumps(obj)


def make_extra_hours_json(
    days: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> Optional[str]:
    if days is None:
        return None
    return json.dumps(days)


def make_special_days_json(
    special_days: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[str]:
    if special_days is None:
        return None
    return json.dumps(special_days)


def make_db_record(
    id: int = 1,
    device_id: int = 1,
    days: Optional[List[str]] = None,
    extra_hours: Optional[str] = None,
    special_days: Optional[str] = None,
    version: str = "1.0",
    source: str = "ui",
) -> Dict[str, Any]:
    """Create a dict that mimics an asyncpg.Record for a schedule row."""
    now = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    return {
        "id": id,
        "device_id": device_id,
        "day_schedules": make_day_schedules_json(days),
        "extra_hours": extra_hours,
        "special_days": special_days,
        "created_at": now,
        "updated_at": now,
        "version": version,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_record():
    """A basic schedule DB record for device 1 (Mon-Fri 08-17)."""
    return make_db_record()


@pytest.fixture
def sample_record_with_extras():
    """Schedule DB record with extra hours on monday."""
    return make_db_record(
        extra_hours=make_extra_hours_json(
            {"monday": [{"start": "18:00", "end": "20:00"}]}
        ),
    )


@pytest.fixture
def sample_record_with_special_days():
    """Schedule DB record with a special day."""
    return make_db_record(
        special_days=make_special_days_json(
            {
                "2025-12-25": {
                    "name": "Navidad",
                    "type": "holiday",
                    "workHours": None,
                    "break": None,
                    "isRecurring": True,
                    "recurrencePattern": "yearly",
                }
            }
        ),
    )


@pytest.fixture
def mock_pool():
    """An AsyncMock standing in for asyncpg.Pool."""
    return AsyncMock()


@pytest.fixture
def client(mock_pool):
    """httpx AsyncClient with dependency overrides (no real DB / no auth)."""
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: mock_pool

    yield AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test/shifts-api/v1",
    )

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Request payloads
# ---------------------------------------------------------------------------


@pytest.fixture
def create_payload() -> Dict[str, Any]:
    """Valid ScheduleCreate payload (camelCase)."""
    return {
        "deviceId": 1,
        "schedule": {
            "monday": {
                "workHours": {"start": "08:00", "end": "17:00"},
                "break": {"start": "12:00", "durationMinutes": 60},
            },
            "tuesday": {
                "workHours": {"start": "08:00", "end": "17:00"},
                "break": {"start": "12:00", "durationMinutes": 60},
            },
        },
    }


@pytest.fixture
def update_payload() -> Dict[str, Any]:
    """Valid ScheduleUpdate payload (no deviceId — comes from URL)."""
    return {
        "schedule": {
            "monday": {
                "workHours": {"start": "09:00", "end": "18:00"},
                "break": {"start": "13:00", "durationMinutes": 45},
            },
        },
    }


@pytest.fixture
def patch_payload() -> Dict[str, Any]:
    """Valid SchedulePatch payload (partial, only metadata)."""
    return {
        "metadata": {"version": "2.0", "source": "api"},
    }
