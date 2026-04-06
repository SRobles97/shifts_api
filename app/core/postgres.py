import os
import asyncpg
import dotenv
from loguru import logger
from typing import Optional

# Cargar variables de entorno desde archivo .env
dotenv.load_dotenv()

# Variable global para mantener el pool de conexiones
conn_pool: Optional[asyncpg.Pool] = None


async def init_db():
    """
    Inicializa la conexión a PostgreSQL/TimescaleDB y configura el esquema de base de datos.

    Raises:
        Exception: Si no se puede establecer la conexión o configurar la base de datos
    """
    global conn_pool
    try:
        logger.info("Iniciando conexión a la base de datos PostgreSQL...")

        # Crear pool de conexiones con configuración optimizada para IoT
        conn_pool = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=1,
            max_size=5,
            server_settings={"timezone": "UTC"},
        )
        logger.info("Conexión a la base de datos PostgreSQL establecida.")

        async with conn_pool.acquire() as conn:
            # Intentar habilitar TimescaleDB para optimizaciones de series temporales
            try:
                await conn.execute(
                    "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"
                )
                logger.info("Extensión TimescaleDB habilitada correctamente.")
            except Exception as e:
                logger.warning(f"No se pudo habilitar TimescaleDB: {e}")
                logger.info("Continuando con PostgreSQL estándar...")

            # Enable btree_gist for exclusion constraint
            try:
                await conn.execute(
                    "CREATE EXTENSION IF NOT EXISTS btree_gist;"
                )
                logger.info("Extensión btree_gist habilitada correctamente.")
            except Exception as e:
                logger.warning(f"No se pudo habilitar btree_gist: {e}")

            # Check if old schedules table exists (for migration)
            old_schedules_exists = await conn.fetchval(
                """
                SELECT EXISTS (SELECT 1
                               FROM information_schema.tables
                               WHERE table_name = 'schedules');
                """
            )

            # Check if new device_schedules table exists
            device_schedules_exists = await conn.fetchval(
                """
                SELECT EXISTS (SELECT 1
                               FROM information_schema.tables
                               WHERE table_name = 'device_schedules');
                """
            )

            # Create device_schedules table
            if not device_schedules_exists:
                await conn.execute(
                    """
                    CREATE TABLE device_schedules
                    (
                        id            BIGSERIAL    PRIMARY KEY,
                        device_id     BIGINT       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                        shift_type    TEXT         NOT NULL DEFAULT 'day',
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
                        CONSTRAINT excl_device_shift_date_overlap
                            EXCLUDE USING gist (device_id WITH =, shift_type WITH =, valid_range WITH &&)
                    );
                    """
                )
                logger.info("Tabla 'device_schedules' creada correctamente.")

                # Indexes
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_device_schedules_device_id
                    ON device_schedules (device_id);
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_device_schedules_valid_range
                    ON device_schedules USING gist (valid_range);
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_device_schedules_special_days
                    ON device_schedules USING GIN (special_days);
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_device_schedules_created_at
                    ON device_schedules (created_at DESC);
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_device_schedules_shift_type
                    ON device_schedules (device_id, shift_type);
                    """
                )
                logger.info("Índices de 'device_schedules' creados correctamente.")

                # Migrate data from old schedules table if it exists
                if old_schedules_exists:
                    migrated = await conn.fetchval(
                        """
                        INSERT INTO device_schedules (
                            device_id, day_schedules, extra_hours, special_days,
                            valid_from, valid_to, version, source, created_at, updated_at
                        )
                        SELECT
                            device_id, day_schedules, extra_hours, special_days,
                            created_at::date, NULL, version, source, created_at, updated_at
                        FROM schedules
                        RETURNING COUNT(*);
                        """
                    )
                    logger.info(f"Migrados {migrated} registros de 'schedules' a 'device_schedules'.")

                    # Rename old table
                    await conn.execute("ALTER TABLE schedules RENAME TO schedules_old;")
                    logger.info("Tabla 'schedules' renombrada a 'schedules_old'.")

            else:
                logger.info("Tabla 'device_schedules' ya existe.")

                # Migrate: add shift_type column if missing
                has_shift_type = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'device_schedules'
                          AND column_name = 'shift_type'
                    );
                    """
                )
                if not has_shift_type:
                    logger.info("Añadiendo columna 'shift_type' a 'device_schedules'...")
                    await conn.execute(
                        "ALTER TABLE device_schedules ADD COLUMN shift_type TEXT NOT NULL DEFAULT 'day';"
                    )
                    # Recreate exclusion constraint with shift_type
                    await conn.execute(
                        "ALTER TABLE device_schedules DROP CONSTRAINT IF EXISTS excl_device_date_overlap;"
                    )
                    await conn.execute(
                        """
                        ALTER TABLE device_schedules
                        ADD CONSTRAINT excl_device_shift_date_overlap
                        EXCLUDE USING gist (device_id WITH =, shift_type WITH =, valid_range WITH &&);
                        """
                    )
                    await conn.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_device_schedules_shift_type
                        ON device_schedules (device_id, shift_type);
                        """
                    )
                    logger.info("Columna 'shift_type' y constraint actualizados.")

            # Configurar optimizaciones específicas de TimescaleDB
            await setup_timescaledb(conn)

    except Exception as e:
        logger.error(f"Error al conectar a la base de datos PostgreSQL: {e}")
        raise


async def setup_timescaledb(conn: asyncpg.Connection):
    """
    Configura optimizaciones de base de datos para PostgreSQL/TimescaleDB.

    Args:
        conn (asyncpg.Connection): Conexión activa a la base de datos
    """
    try:
        result = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM pg_extension
            WHERE extname = 'timescaledb';
            """
        )

        if result == 0:
            logger.info("TimescaleDB no está disponible, usando PostgreSQL estándar.")
            return

        logger.info("Configurando TimescaleDB...")

        # Skip TimescaleDB hypertable features for device_schedules table
        # The device_schedules table is a configuration table, not time-series data
        logger.info(
            "Omitiendo configuración de hypertable para device_schedules (tabla de configuración, no series temporales)."
        )

        logger.info("Configuración de TimescaleDB completada exitosamente.")

    except Exception as e:
        logger.error(f"Error general en configuración de TimescaleDB: {e}")


def get_postgres() -> asyncpg.Pool:
    """
    Obtiene una referencia al pool de conexiones de PostgreSQL.

    Returns:
        asyncpg.Pool: El objeto de pool de conexiones a la base de datos PostgreSQL.

    Raises:
        ConnectionError: Si el pool de conexiones no está inicializado.
    """
    global conn_pool
    if conn_pool is None:
        logger.info("Pool de conexiones no inicializado.")
        raise ConnectionError("Pool de conexiones no inicializado.")
    try:
        return conn_pool
    except Exception as e:
        logger.error(f"Error al devolver el pool de conexiones: {e}")
        raise


async def close_postgres() -> None:
    """
    Cierra el pool de conexiones a la base de datos PostgreSQL.
    """
    global conn_pool
    if conn_pool is not None:
        try:
            logger.info("Cerrando pool de conexiones de PostgreSQL...")
            await conn_pool.close()
            logger.info("Pool de conexiones a PostgreSQL cerrado correctamente.")
        except Exception as e:
            logger.error(f"Error al cerrar el pool de conexiones: {e}")
            raise
        finally:
            conn_pool = None
    else:
        logger.warning("No hay un pool de conexiones para cerrar.")
