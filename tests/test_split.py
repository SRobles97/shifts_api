"""Tests for the schedule splitting (create_with_split) logic."""

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.repositories.crud import schedule_crud

CRUD_PATH = "app.repositories.crud.ScheduleCRUD"


def _mock_existing_record(
    id=1,
    valid_from=date(2026, 4, 13),
    valid_to=None,
    day_schedules='{"monday": {"workHours": {"start": "08:00", "end": "17:00"}}}',
    extra_hours=None,
    special_days=None,
    version="1.0",
    source="ui",
):
    """Create a dict mimicking an asyncpg.Record for an existing schedule."""
    return {
        "id": id,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "day_schedules": day_schedules,
        "extra_hours": extra_hours,
        "special_days": special_days,
        "version": version,
        "source": source,
    }


def _make_schedule_data(
    device_id=10,
    shift_type="day",
    valid_from=date(2026, 4, 18),
    valid_to=date(2026, 4, 18),
):
    return {
        "device_id": device_id,
        "shift_type": shift_type,
        "day_schedules": '{"saturday": {"workHours": {"start": "08:00", "end": "13:00"}}}',
        "extra_hours": None,
        "special_days": None,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "version": "2.0",
        "source": "mobile_app",
    }


def _make_pool_with_conn(mock_conn):
    """Create a mock pool whose acquire() returns an async context manager yielding mock_conn.

    Also patches conn.transaction() to return an async context manager (no-op)
    so that ``async with conn.transaction():`` works transparently.
    """
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    @asynccontextmanager
    async def _transaction():
        yield

    pool.acquire = _acquire
    mock_conn.transaction = _transaction
    return pool


class TestCreateWithSplitNoOverlap:
    @pytest.mark.asyncio
    async def test_no_overlap_inserts_normally(self):
        """When no existing schedule overlaps, just insert the new one."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None
        mock_conn.fetchval.return_value = 99

        pool = _make_pool_with_conn(mock_conn)

        data = _make_schedule_data()
        result = await schedule_crud.create_with_split(pool, data)

        assert result == 99
        mock_conn.fetchval.assert_called_once()
        mock_conn.execute.assert_not_called()


class TestCreateWithSplitTypical:
    @pytest.mark.asyncio
    async def test_split_open_ended_schedule(self):
        """Typical case: existing A (Apr 13 -> NULL), override B (Apr 18).
        Should: shrink A to Apr 17, insert A' from Apr 19 -> NULL, insert B."""
        existing = _mock_existing_record(
            id=33, valid_from=date(2026, 4, 13), valid_to=None,
        )

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = existing
        mock_conn.fetchval.return_value = 50

        pool = _make_pool_with_conn(mock_conn)

        data = _make_schedule_data(
            valid_from=date(2026, 4, 18), valid_to=date(2026, 4, 18),
        )
        result = await schedule_crud.create_with_split(pool, data)

        assert result == 50
        calls = mock_conn.execute.call_args_list
        assert "valid_to" in calls[0].args[0]
        assert calls[0].args[1] == 33
        assert "INSERT" in calls[1].args[0]
        mock_conn.fetchval.assert_called_once()


class TestCreateWithSplitStartsOnSameDay:
    @pytest.mark.asyncio
    async def test_override_starts_on_existing_start(self):
        """Override starts on same day as existing.
        Should: push existing forward, insert B, no clone."""
        existing = _mock_existing_record(
            id=33, valid_from=date(2026, 4, 13), valid_to=None,
        )

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = existing
        mock_conn.fetchval.return_value = 50

        pool = _make_pool_with_conn(mock_conn)

        data = _make_schedule_data(
            valid_from=date(2026, 4, 13), valid_to=date(2026, 4, 14),
        )
        result = await schedule_crud.create_with_split(pool, data)

        assert result == 50
        calls = mock_conn.execute.call_args_list
        assert len(calls) == 1
        assert "valid_from" in calls[0].args[0]


class TestCreateWithSplitCoversEntireRange:
    @pytest.mark.asyncio
    async def test_override_covers_entire_bounded_schedule(self):
        """Override covers existing entirely. Should: delete existing, insert B."""
        existing = _mock_existing_record(
            id=33, valid_from=date(2026, 4, 13), valid_to=date(2026, 4, 18),
        )

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = existing
        mock_conn.fetchval.return_value = 50

        pool = _make_pool_with_conn(mock_conn)

        data = _make_schedule_data(
            valid_from=date(2026, 4, 13), valid_to=date(2026, 4, 18),
        )
        result = await schedule_crud.create_with_split(pool, data)

        assert result == 50
        calls = mock_conn.execute.call_args_list
        assert len(calls) == 1
        assert "DELETE" in calls[0].args[0]


class TestCreateWithSplitBoundedExisting:
    @pytest.mark.asyncio
    async def test_split_bounded_schedule(self):
        """Existing A (Apr 13 -> Apr 25), override B (Apr 18 -> Apr 20).
        Should: shrink A to Apr 17, insert A' from Apr 21 -> Apr 25, insert B."""
        existing = _mock_existing_record(
            id=33, valid_from=date(2026, 4, 13), valid_to=date(2026, 4, 25),
        )

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = existing
        mock_conn.fetchval.return_value = 50

        pool = _make_pool_with_conn(mock_conn)

        data = _make_schedule_data(
            valid_from=date(2026, 4, 18), valid_to=date(2026, 4, 20),
        )
        result = await schedule_crud.create_with_split(pool, data)

        assert result == 50
        calls = mock_conn.execute.call_args_list
        assert len(calls) == 2
        assert "valid_to" in calls[0].args[0]
        assert "INSERT" in calls[1].args[0]


class TestCreateWithSplitEndsOnExistingEnd:
    @pytest.mark.asyncio
    async def test_no_after_portion_when_override_ends_on_existing_end(self):
        """Existing A (Apr 13 -> Apr 18), override B (Apr 16 -> Apr 18).
        Should: shrink A to Apr 15, insert B, no clone."""
        existing = _mock_existing_record(
            id=33, valid_from=date(2026, 4, 13), valid_to=date(2026, 4, 18),
        )

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = existing
        mock_conn.fetchval.return_value = 50

        pool = _make_pool_with_conn(mock_conn)

        data = _make_schedule_data(
            valid_from=date(2026, 4, 16), valid_to=date(2026, 4, 18),
        )
        result = await schedule_crud.create_with_split(pool, data)

        assert result == 50
        calls = mock_conn.execute.call_args_list
        assert len(calls) == 1
        assert "valid_to" in calls[0].args[0]
