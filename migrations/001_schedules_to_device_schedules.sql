-- Migration: schedules → device_schedules
-- Supports N schedules per device with date-range validity and overlap prevention.
--
-- Run on VPS:
--   psql -U dbmanager -d centineldb -f 001_schedules_to_device_schedules.sql
--
-- This script is idempotent: it checks for table existence before acting.

BEGIN;

-- 1. Ensure btree_gist extension (needed for exclusion constraint)
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 2. Create device_schedules table (only if it does not exist)
CREATE TABLE IF NOT EXISTS device_schedules
(
    id            BIGSERIAL    PRIMARY KEY,
    device_id     BIGINT       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    day_schedules JSONB        NOT NULL,
    extra_hours   JSONB,
    special_days  JSONB,
    valid_from    DATE         NOT NULL,
    valid_to      DATE,
    valid_range   DATERANGE    NOT NULL GENERATED ALWAYS AS (
                      daterange(valid_from, COALESCE(valid_to, '9999-12-31'::date), '[]')
                  ) STORED,
    version       TEXT         NOT NULL DEFAULT '1.0',
    source        TEXT         NOT NULL DEFAULT 'ui',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT excl_device_date_overlap
        EXCLUDE USING gist (device_id WITH =, valid_range WITH &&)
);

-- 3. Create indexes
CREATE INDEX IF NOT EXISTS idx_device_schedules_device_id
    ON device_schedules (device_id);

CREATE INDEX IF NOT EXISTS idx_device_schedules_valid_range
    ON device_schedules USING gist (valid_range);

CREATE INDEX IF NOT EXISTS idx_device_schedules_special_days
    ON device_schedules USING GIN (special_days);

CREATE INDEX IF NOT EXISTS idx_device_schedules_created_at
    ON device_schedules (created_at DESC);

-- 4. Migrate existing data from schedules → device_schedules
--    valid_from = created_at::date, valid_to = NULL (open-ended)
--    Only runs if schedules table exists AND device_schedules is empty
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'schedules'
    ) AND NOT EXISTS (
        SELECT 1 FROM device_schedules LIMIT 1
    ) THEN
        INSERT INTO device_schedules (
            device_id, day_schedules, extra_hours, special_days,
            valid_from, valid_to, version, source, created_at, updated_at
        )
        SELECT
            device_id, day_schedules, extra_hours, special_days,
            created_at::date, NULL, version, source, created_at, updated_at
        FROM schedules;

        RAISE NOTICE 'Migrated % rows from schedules to device_schedules',
            (SELECT COUNT(*) FROM schedules);
    END IF;
END $$;

-- 5. Rename old table (only if it still exists under the original name)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'schedules'
    ) THEN
        ALTER TABLE schedules RENAME TO schedules_old;
        RAISE NOTICE 'Renamed schedules → schedules_old';
    END IF;
END $$;

COMMIT;
