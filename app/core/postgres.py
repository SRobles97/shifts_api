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

    Esta función es el punto de entrada principal para la configuración de la base de datos.
    Realiza las siguientes operaciones:
    1. Establece el pool de conexiones a PostgreSQL
    2. Intenta habilitar TimescaleDB (si está disponible)
    3. Crea las tablas necesarias con esquemas optimizados
    4. Configura TimescaleDB con hypertables, políticas de retención y compresión
    5. Crea índices optimizados para consultas por dispositivo y tiempo

    Raises:
        Exception: Si no se puede establecer la conexión o configurar la base de datos
    """
    global conn_pool
    try:
        logger.info("Iniciando conexión a la base de datos PostgreSQL...")

        # Crear pool de conexiones con configuración optimizada para IoT
        conn_pool = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=1,  # Mínimo de conexiones activas
            max_size=5,  # Máximo de conexiones concurrentes
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
                        id                  SERIAL      PRIMARY KEY,  -- ID único del horario
                        device_name         TEXT        NOT NULL,     -- Nombre del dispositivo
                        active_days         TEXT[]      NOT NULL,     -- Días activos (array de strings)
                        work_start_time     TIME        NOT NULL,     -- Hora de inicio de trabajo
                        work_end_time       TIME        NOT NULL,     -- Hora de fin de trabajo
                        break_start_time    TIME        NOT NULL,     -- Hora de inicio de descanso
                        break_duration      INTEGER     NOT NULL,     -- Duración del descanso en minutos
                        extra_hours         JSONB       NULL,         -- Horas extra por día (formato JSON)
                        created_at          TIMESTAMP   DEFAULT NOW(),-- Fecha de creación
                        updated_at          TIMESTAMP   DEFAULT NOW(),-- Fecha de última actualización
                        version             TEXT        DEFAULT '1.0',-- Versión del horario
                        source              TEXT        DEFAULT 'api',-- Fuente del horario
                        
                        UNIQUE(device_name)  -- Un horario por dispositivo
                    );
                    """
                )
                logger.info(
                    "Tabla 'schedules' creada correctamente con clave primaria única."
                )
            else:
                logger.info("Tabla 'schedules' ya existe.")

            # Configurar optimizaciones específicas de TimescaleDB
            await setup_timescaledb(conn)

    except Exception as e:
        logger.error(f"Error al conectar a la base de datos PostgreSQL: {e}")
        raise


async def setup_timescaledb(conn: asyncpg.Connection):
    """
    Configura TimescaleDB con hypertables, políticas de retención y compresión.

    TimescaleDB es una extensión de PostgreSQL optimizada para series temporales
    que proporciona:
    - Particionado automático por tiempo (hypertables)
    - Compresión de datos históricos
    - Políticas de retención automática
    - Consultas agregadas optimizadas

    Args:
        conn (asyncpg.Connection): Conexión activa a la base de datos
    """
    try:
        # Verificar disponibilidad de TimescaleDB
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

        # Verificar si la tabla de schedules ya es hypertable (particionada por tiempo)
        schedules_is_hypertable = await conn.fetchval(
            """
            SELECT EXISTS (SELECT 1
                           FROM timescaledb_information.hypertables
                           WHERE hypertable_name = 'schedules');
            """
        )

        # Convertir schedules a hypertable (particionado por mes)
        if not schedules_is_hypertable:
            try:
                await conn.execute(
                    """
                    SELECT create_hypertable('schedules', 'created_at', 
                                           chunk_time_interval => INTERVAL '1 month');
                    """
                )
                logger.info("Hypertable 'schedules' creada correctamente.")
            except Exception as e:
                logger.warning(f"Error al crear hypertable schedules: {e}")
        else:
            logger.info("schedules ya es una hypertable.")

        # Configurar políticas de retención automática para schedules (retener 2 años)
        try:
            await conn.execute(
                """
                SELECT add_retention_policy('schedules', INTERVAL '2 years', 
                                          if_not_exists => TRUE);
                """
            )
            logger.info("Política de retención configurada para schedules (2 años).")
        except Exception as e:
            logger.warning(f"Error al configurar política de retención schedules: {e}")

        # Configurar compresión automática para schedules > 30 días (ahorro de espacio)
        try:
            # Habilitar compresión en la tabla de schedules
            await conn.execute(
                """
                ALTER TABLE schedules SET (timescaledb.compress);
                """
            )
            # Configurar compresión por dispositivo (mejor ratio de compresión)
            await conn.execute(
                """
                ALTER TABLE schedules SET (timescaledb.compress_segmentby = 'device_name');
                """
            )
            logger.info("Compresión habilitada para schedules.")

            # Política automática: comprimir datos > 30 días
            await conn.execute(
                """
                SELECT add_compression_policy('schedules', INTERVAL '30 days');
                """
            )
            logger.info("Política de compresión añadida para schedules.")

        except Exception as e:
            logger.warning(f"Error al configurar compresión para schedules: {e}")

        # Crear índices optimizados para consultas frecuentes
        try:
            # Índice para consultas por dispositivo
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_schedules_device_name
                    ON schedules (device_name);
                """
            )
            # Índice para consultas por fecha de creación
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_schedules_created_at
                    ON schedules (created_at DESC);
                """
            )
            # Índice compuesto para consultas por dispositivo y fecha
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_schedules_device_created
                    ON schedules (device_name, created_at DESC);
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

    Esta función se utiliza como dependencia en FastAPI para inyectar
    el pool de conexiones en los endpoints que necesitan acceso a la base de datos.

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

    Esta función debe llamarse durante el shutdown de la aplicación
    para liberar recursos de conexión de manera limpia.
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


# Database initialization and connection management only
# CRUD operations have been moved to app/repositories/crud.py
