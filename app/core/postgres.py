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

            # Verificar existencia de tabla de horarios antes de crearla
            schedules_exists = await conn.fetchval(
                """
                SELECT EXISTS (SELECT 1
                               FROM information_schema.tables
                               WHERE table_name = 'schedules');
                """
            )

            # Crear tabla para horarios de dispositivos
            if not schedules_exists:
                await conn.execute(
                    """
                    CREATE TABLE schedules
                    (
                        id            BIGSERIAL    PRIMARY KEY,
                        device_id     BIGINT       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                        day_schedules JSONB        NOT NULL,
                        extra_hours   JSONB,
                        special_days  JSONB,
                        version       TEXT         NOT NULL DEFAULT '1.0',
                        source        TEXT         NOT NULL DEFAULT 'ui',
                        created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                        CONSTRAINT uq_schedules_device UNIQUE(device_id)
                    );
                    """
                )
                logger.info("Tabla 'schedules' creada correctamente.")

                # Index on device_id (covered by UNIQUE but explicit for clarity)
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_schedules_device_id
                    ON schedules (device_id);
                    """
                )

                # GIN index for special_days JSONB queries
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_schedules_special_days
                    ON schedules USING GIN (special_days);
                    """
                )
                logger.info("Índices creados correctamente.")
            else:
                logger.info("Tabla 'schedules' ya existe.")

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

        # Skip TimescaleDB hypertable features for schedules table
        # The schedules table is a configuration table, not time-series data
        logger.info(
            "Omitiendo configuración de hypertable para schedules (tabla de configuración, no series temporales)."
        )

        try:
            # Índice para consultas por fecha de creación
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_schedules_created_at
                    ON schedules (created_at DESC);
                """
            )
            logger.info("Índices optimizados creados correctamente.")
        except Exception as e:
            logger.warning(f"Error al crear índices: {e}")

        logger.info("Configuración de TimescaleDB completada exitosamente.")

    except Exception as e:
        logger.error(f"Error general en configuración de TimescaleDB: {e}")


async def get_postgres() -> asyncpg.Pool:
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
