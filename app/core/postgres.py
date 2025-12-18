import os
import asyncpg
import dotenv
from loguru import logger
from typing import Optional

# Cargar variables de entorno desde archivo .env
dotenv.load_dotenv()

# Variable global para mantener el pool de conexiones
conn_pool: Optional[asyncpg.Pool] = None


async def migrate_schedules_table(conn: asyncpg.Connection):
    """
    Migrar la tabla schedules para agregar soporte de rangos de fechas.

    Esta función agrega las columnas start_date y end_date a tablas existentes,
    migra los datos existentes, y configura las restricciones e índices necesarios.

    Args:
        conn (asyncpg.Connection): Conexión activa a la base de datos
    """
    try:
        # Verificar si start_date ya existe
        start_date_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'schedules' AND column_name = 'start_date'
            );
            """
        )

        if start_date_exists:
            logger.info("Migración de rangos de fechas ya aplicada.")
            return

        logger.info("Iniciando migración para soporte de rangos de fechas...")

        # Paso 1: Agregar columnas (nullable primero)
        await conn.execute(
            "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS start_date DATE;"
        )
        await conn.execute(
            "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS end_date DATE;"
        )
        logger.info("Columnas start_date y end_date agregadas.")

        # Paso 2: Migrar datos existentes
        await conn.execute(
            "UPDATE schedules SET start_date = created_at::date WHERE start_date IS NULL;"
        )
        logger.info("Datos existentes migrados (start_date = created_at).")

        # Paso 3: Hacer start_date NOT NULL
        await conn.execute(
            "ALTER TABLE schedules ALTER COLUMN start_date SET NOT NULL;"
        )
        await conn.execute(
            "ALTER TABLE schedules ALTER COLUMN start_date SET DEFAULT CURRENT_DATE;"
        )
        logger.info("Columna start_date configurada como NOT NULL con default.")

        # Paso 4: Agregar constraint de validación de rango
        await conn.execute(
            """
            ALTER TABLE schedules ADD CONSTRAINT schedules_date_range_check
            CHECK (end_date IS NULL OR end_date >= start_date);
            """
        )
        logger.info("Constraint de validación de rango agregado.")

        # Paso 5: Eliminar constraint UNIQUE en device_name
        try:
            await conn.execute(
                "ALTER TABLE schedules DROP CONSTRAINT IF EXISTS schedules_device_name_key;"
            )
            logger.info("Constraint UNIQUE en device_name eliminado.")
        except Exception as e:
            logger.warning(f"No se pudo eliminar constraint UNIQUE: {e}")

        # Paso 6: Agregar extensión btree_gist y constraint de exclusión
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
            await conn.execute(
                """
                ALTER TABLE schedules ADD CONSTRAINT schedules_no_overlap
                EXCLUDE USING GIST (
                    device_name WITH =,
                    daterange(start_date, COALESCE(end_date, '9999-12-31'::date), '[]') WITH &&
                );
                """
            )
            logger.info("Constraint de exclusión para prevenir solapamientos agregado.")
        except Exception as e:
            logger.warning(f"No se pudo agregar constraint de exclusión: {e}")
            logger.info("Continuando sin constraint de exclusión (validación manual requerida).")

        # Paso 7: Agregar índices para consultas de rangos de fechas
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_schedules_device_date_range
            ON schedules (device_name, start_date, end_date);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_schedules_active
            ON schedules (device_name, start_date)
            WHERE end_date IS NULL OR end_date >= CURRENT_DATE;
            """
        )
        logger.info("Índices para rangos de fechas creados correctamente.")

        logger.info("Migración completada exitosamente.")

    except Exception as e:
        logger.error(f"Error durante la migración de rangos de fechas: {e}")
        raise


async def init_db():
    """
    Inicializa la conexión a PostgreSQL/TimescaleDB y configura el esquema de base de datos.

    Esta función es el punto de entrada principal para la configuración de la base de datos.
    Realiza las siguientes operaciones:
    1. Establece el pool de conexiones a PostgreSQL
    2. Intenta habilitar TimescaleDB (si está disponible)
    3. Crea la tabla de schedules para configuración de horarios
    4. Crea índices optimizados para consultas por dispositivo

    Nota: No se aplican características de TimescaleDB (hypertables, compresión, retención)
    a la tabla schedules ya que es una tabla de configuración, no de series temporales.

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
            server_settings={"timezone": "UTC"},  # Garantiza sesiones en UTC
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
                        day_schedules       JSONB       NOT NULL,     -- Horarios por día (formato JSON)
                        extra_hours         JSONB       NULL,         -- Horas extra por día (formato JSON)
                        special_days        JSONB       NULL,         -- Días especiales con horarios personalizados
                        start_date          DATE        NOT NULL DEFAULT CURRENT_DATE,  -- Fecha de inicio del horario
                        end_date            DATE        NULL,         -- Fecha de fin del horario (NULL = indefinido)
                        created_at          TIMESTAMPTZ DEFAULT NOW(),-- Fecha de creación (UTC)
                        updated_at          TIMESTAMPTZ DEFAULT NOW(),-- Fecha de última actualización (UTC)
                        version             TEXT        DEFAULT '1.0',-- Versión del horario
                        source              TEXT        DEFAULT 'api',-- Fuente del horario

                        CHECK (end_date IS NULL OR end_date >= start_date)  -- Validación de rango de fechas
                    );
                    """
                )
                logger.info(
                    "Tabla 'schedules' creada correctamente con soporte para rangos de fechas."
                )

                # Add GIN index for special_days JSONB queries
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_schedules_special_days
                    ON schedules USING GIN (special_days);
                    """
                )
                logger.info("Índice GIN creado para special_days.")
            else:
                logger.info("Tabla 'schedules' ya existe.")

                # Migrate existing table to add date range support
                await migrate_schedules_table(conn)

            # Configurar optimizaciones específicas de TimescaleDB
            await setup_timescaledb(conn)

    except Exception as e:
        logger.error(f"Error al conectar a la base de datos PostgreSQL: {e}")
        raise


async def setup_timescaledb(conn: asyncpg.Connection):
    """
    Configura optimizaciones de base de datos para PostgreSQL/TimescaleDB.

    Para la tabla de schedules (configuración de horarios):
    - Crea índices optimizados para consultas por dispositivo
    - No aplica características de TimescaleDB (hypertables, compresión, retención)
      ya que schedules es una tabla de configuración, no de series temporales

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

        # Skip TimescaleDB hypertable features for schedules table
        # The schedules table is a configuration table, not time-series data
        # It doesn't benefit from hypertable partitioning, compression, or retention policies
        logger.info(
            "Omitiendo configuración de hypertable para schedules (tabla de configuración, no series temporales)."
        )

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
