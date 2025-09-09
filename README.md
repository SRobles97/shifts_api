# Shifts API - Sistema de Gestión de Horarios

Una API REST basada en FastAPI para gestionar horarios de trabajo y turnos de dispositivos.

## Características

- **Gestión de Horarios**: Crear, leer, actualizar y eliminar horarios de dispositivos  
- **Autenticación con API Key**: Endpoints seguros con autenticación por cabecera  
- **PostgreSQL/TimescaleDB**: Base de datos optimizada con capacidades de series temporales  
- **Generación Automática de IDs**: Identificadores únicos generados por la base de datos  
- **Soporte de Horas Extra**: Períodos de horas extra opcionales por día  
- **Validación Completa**: Validación de lógica de negocio con Pydantic  
- **Arquitectura Estructurada**: Separación clara de responsabilidades  

## Arquitectura

```
app/
├── core/           # Configuración, conexiones a la base de datos, middleware  
├── models/         # Modelos de lógica de negocio con validación
├── schemas/        # Modelos de solicitud/respuesta de la API
├── repositories/   # Capa de acceso a datos (operaciones CRUD)
└── routers/        # Endpoints de la API
```

## Inicio Rápido

### 1. Configuración del Entorno

Crea un archivo `.env` en la raíz del proyecto:

```env
API_KEY=tu-api-key-secreta
DATABASE_URL=postgresql://usuario:contraseña@localhost:5432/base_de_datos
```

### 2. Instalar Dependencias

```bash
pip install -r requirements.txt
```

### 3. Ejecutar la Aplicación

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Acceder a la API

- **Documentación de la API**: http://localhost:8000/docs  
- **Health Check**: http://localhost:8000/health  
- **Endpoint de Horarios**: http://localhost:8000/api/v1/schedules  

## Endpoints de la API

### Autenticación

Todos los endpoints requieren una cabecera `X-API-Key`:

```bash
curl -H "X-API-Key: tu-api-key-secreta" http://localhost:8000/api/v1/schedules
```

### Endpoints de Horarios

| Método   | Endpoint                          | Descripción                           |
| -------- | --------------------------------- | ------------------------------------- |
| `POST`   | `/api/v1/schedules/`              | Crear o actualizar un horario         |
| `GET`    | `/api/v1/schedules/`              | Obtener todos los horarios            |
| `GET`    | `/api/v1/schedules/{device_name}` | Obtener horario por dispositivo       |
| `GET`    | `/api/v1/schedules/by-day/{day}`  | Obtener horarios por día de la semana |
| `DELETE` | `/api/v1/schedules/{device_name}` | Eliminar horario                      |

### Ejemplo de Solicitud

```bash
curl -X POST "http://localhost:8000/api/v1/schedules/"   -H "Content-Type: application/json"   -H "X-API-Key: tu-api-key-secreta"   -d '{
    "deviceName": "Rep1",
    "schedule": {
      "activeDays": ["monday", "tuesday", "wednesday", "thursday", "friday"],
      "workHours": {
        "start": "08:00",
        "end": "17:00"
      },
      "break": {
        "start": "12:00", 
        "durationMinutes": 30
      }
    },
    "extraHours": {
      "monday": [{"start": "17:00", "end": "18:00"}]
    },
    "metadata": {
      "version": "1.0",
      "source": "mobile_app"
    }
  }'
```

## Esquema de Base de Datos

La aplicación utiliza una única tabla `schedules` con la siguiente estructura:

```sql
CREATE TABLE schedules (
    id                  SERIAL      PRIMARY KEY,
    device_name         TEXT        NOT NULL UNIQUE,
    active_days         TEXT[]      NOT NULL,
    work_start_time     TIME        NOT NULL,
    work_end_time       TIME        NOT NULL,
    break_start_time    TIME        NOT NULL,
    break_duration      INTEGER     NOT NULL,
    extra_hours         JSONB       NULL,
    created_at          TIMESTAMP   DEFAULT NOW(),
    updated_at          TIMESTAMP   DEFAULT NOW(),
    version             TEXT        DEFAULT '1.0',
    source              TEXT        DEFAULT 'api'
);
```

## Desarrollo

### Estructura del Proyecto

- **Models**: Lógica de negocio con validación (`app/models/`)  
- **Schemas**: Serialización de la API (`app/schemas/`)  
- **Repositories**: Operaciones de base de datos (`app/repositories/`)  
- **Routers**: Endpoints de la API (`app/routers/`)  
- **Core**: Configuración y utilidades (`app/core/`)  


### Calidad de Código

El proyecto sigue las mejores prácticas de FastAPI con:  
- Patrones de arquitectura limpia  
- Anotaciones de tipos en todo el código  
- Validación completa  
- Manejo adecuado de errores  
- Logging estructurado  

## Optimizaciones con TimescaleDB

Cuando TimescaleDB está disponible, la aplicación automáticamente:  
- Crea hypertables para optimización de series temporales  
- Configura políticas de compresión (30 días)  
- Configura políticas de retención (2 años)  
- Crea índices optimizados  
