# Disaster Safety Application - Production & Deployment Guide

A mission-critical disaster management and safe evacuation application combining a high-performance **FastAPI (Python 3.13) + PostgreSQL/PostGIS** backend with an interactive **React Native / Expo + MapLibre** cross-platform mobile client.

> **CRITICAL DISASTER SAFETY NOTICE:**
> This application is an evidence-based decision-support system that supplements official emergency information. It does **NOT** guarantee that any recommended shelter or evacuation route is free from danger. Natural disasters evolve dynamically; users must always prioritize live directives issued on the ground by the **National Disaster Management Authority (NDMA)**, **State Disaster Management Authorities (SDMA)**, and local emergency personnel.

---

## 1. Prerequisites & Software Versions

| Component | Required Version | Verification Command |
| :--- | :--- | :--- |
| **Node.js** | `>= 20.0.0` (LTS) | `node --version` |
| **npm** | `>= 10.0.0` | `npm --version` |
| **Python** | `3.11` – `3.13` | `python --version` |
| **PostgreSQL** | `15.0+` or `16.x` | `psql --version` |
| **PostGIS Extension** | `3.3+` or `3.4.x` | `SELECT PostGIS_Version();` |
| **Docker & Compose** | `Docker 24+`, `Compose v2.20+` | `docker --version`, `docker compose version` |
| **Expo CLI / EAS CLI** | `EAS CLI >= 12.0.0` | `npx eas --version` |

---

## 2. Production Architecture Overview

```text
┌────────────────────────────────────────────────────────┐
│             Mobile Clients (Android & iOS)             │
│        React Native + Expo SDK 58 + MapLibre Native    │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTPS (TLS 1.3)
                           ▼
┌────────────────────────────────────────────────────────┐
│                 FastAPI Backend Service                │
│    Gunicorn (UvicornWorker) / RateLimiter / Security   │
│             Liveness & Readiness Health Probes         │
└──────┬───────────────────┬──────────────────────┬──────┘
       │                   │                      │
       ▼                   ▼                      ▼
┌──────────────┐   ┌───────────────┐     ┌────────────────┐
│  PostgreSQL  │   │  Background   │     │  External APIs │
│   + PostGIS  │   │ Sync Worker   │     │ OSRM Routing   │
│ 16 / GIST Idx│   │ NDMA/IMD/NCS  │     │ Expo Push GW   │
└──────────────┘   └───────────────┘     └────────────────┘
```

---

## 3. Database Setup & PostGIS Verification

### Step 3.1: Create Application Role and Database
Log into your PostgreSQL instance as an administrator (`postgres`):

```sql
-- 1. Create dedicated application role
CREATE ROLE disaster_safety_app WITH LOGIN PASSWORD 'YOUR_STRONG_PROD_PASSWORD_HERE';

-- 2. Create production database
CREATE DATABASE disaster_safety OWNER disaster_safety_app;

-- 3. Connect to target database
\connect disaster_safety

-- 4. Enable PostGIS extension
CREATE EXTENSION IF NOT EXISTS postgis;

-- 5. Verify PostGIS installation
SELECT postgis_full_version();
```

### Step 3.2: Verify Spatial Privileges
Ensure the role owns the schema and can create spatial indexes:
```sql
GRANT ALL ON SCHEMA public TO disaster_safety_app;
```

---

## 4. Environment & Secret Configuration

Never commit production `.env` files to source control.

### Step 4.1: Backend Environment Setup
Copy the production template into `backend/.env`:
```powershell
cd backend
Copy-Item .env.production.example .env
```
Key production variables to configure:
```env
APP_NAME="Disaster Safety API"
ENVIRONMENT=production
SERVER_HOST=0.0.0.0
SERVER_PORT=8000

# Managed Database Credentials
DB_HOST=your-db-host.internal
DB_PORT=5432
DB_NAME=disaster_safety
DB_USER=disaster_safety_app
DB_PASSWORD=YOUR_STRONG_PROD_PASSWORD_HERE
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_ECHO=false

# Road Network Routing (OSRM)
OSRM_BASE_URL=http://router.project-osrm.org
OSRM_TIMEOUT_SECONDS=5.0

# Alert Synchronization
ALERT_MONITORING_ENABLED=true
ALERT_MONITORING_INTERVAL_SECONDS=300

# Security Restrictions
CORS_ALLOWED_ORIGINS=https://disastersafety.in,https://app.disastersafety.in
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=120
LOG_LEVEL=INFO
```

### Step 4.2: Frontend Environment Setup
Copy the frontend production template into `frontend/.env`:
```powershell
cd frontend
Copy-Item .env.production.example .env
```
Key variables:
```env
EXPO_PUBLIC_API_BASE_URL=https://api.disastersafety.in
EXPO_PUBLIC_MAP_STYLE_URL=https://tiles.openfreemap.org/styles/liberty
EXPO_PUBLIC_MAP_ATTRIBUTION=© OpenFreeMap © OpenMapTiles Data © OpenStreetMap contributors
```

---

## 5. Database Migrations

Apply Alembic migrations to set up spatial tables, indices, and constraints:

```powershell
cd backend
# Activate virtual environment
.\.venv\Scripts\Activate.ps1   # (On Linux: source .venv/bin/activate)

# Check current revision
python -m alembic current

# Upgrade to latest revision (head)
python -m alembic upgrade head
```

To verify the migration SQL offline without executing against the database:
```powershell
python -m alembic upgrade head --sql
```

---

## 6. Backend Deployment Options

### Option A: Containerized Deployment (Docker Compose - Recommended)
The repository includes a production `docker-compose.yml` orchestrating PostgreSQL/PostGIS and the FastAPI ASGI application with healthchecks:

```powershell
# Build and start services in background
docker compose up -d --build

# Inspect service logs
docker compose logs -f api

# Verify running health
docker compose ps
```

### Option B: Bare-Metal / Cloud VM Deployment (Linux Systemd)
1. Install system packages and Python 3.13:
   ```bash
   sudo apt update && sudo apt install -y python3.13 python3.13-venv curl libpq5
   ```
2. Setup application directory:
   ```bash
   git clone <repo_url> /opt/disaster-safety-app
   cd /opt/disaster-safety-app/backend
   python3.13 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. Run with Gunicorn multi-worker ASGI server:
   ```bash
   gunicorn -c gunicorn_conf.py app.main:app
   ```

---

## 7. Mobile Application Production Build

The mobile application utilizes `@maplibre/maplibre-react-native` which contains native C++/Kotlin/Swift modules. It requires custom native compilation.

### Step 7.1: Local Release Build (Android)
Ensure Android SDK and Java 17 are installed:
```powershell
cd frontend
npm install

# Generate native Android project with permissions and package ID
npx expo prebuild --platform android --clean

# Build standalone release APK
npx expo run:android --variant release
```

### Step 7.2: Cloud Build with EAS (Expo Application Services)
1. Log into your Expo account:
   ```powershell
   npx eas login
   ```
2. Build Android App Bundle (AAB) for Google Play:
   ```powershell
   npx eas build --profile production --platform android
   ```
3. Build iOS IPA for App Store (Requires Apple Developer account):
   ```powershell
   npx eas build --profile production --platform ios
   ```

---

## 8. Background Monitoring & Alert Synchronization

The background alert monitor polls verified government data sources (NCS, IMD, CWC, INCOIS, SACHET) and dispatches push notifications to affected device profiles:
- **Lifespan Task:** Enabled automatically when `ALERT_MONITORING_ENABLED=true` in `backend/.env`.
- **Concurrency Control:** Utilizes `asyncio.Lock` preventing overlapping runs.
- **Deduplication:** Computes payload hashes (`report_hash`) to avoid sending duplicate push alerts.
- **Manual Trigger Endpoint:**
  ```bash
  curl -X POST https://api.disastersafety.in/api/v1/alerts/sync \
       -H "Content-Type: application/json" \
       -d '{"sync_mode": "MANUAL"}'
  ```

---

## 9. Operational Health & Monitoring

The application exposes 3 distinct health probe endpoints:

| Endpoint | Probe Type | Purpose | HTTP Status |
| :--- | :--- | :--- | :--- |
| `GET /health` | **Liveness** | Ultra-lightweight check for ALBs / Kubernetes. Zero DB queries. | `200 OK` |
| `GET /health/ready` | **Readiness** | Validates DB connection & PostGIS extension before routing traffic. | `200 OK` or `503 Service Unavailable` |
| `GET /health/detailed` | **Diagnostic** | Comprehensive diagnostics: DB, sync logs, OSRM reachability, notifications. | `200 OK` (Healthy/Degraded) or `503` (Unhealthy) |

Example Detailed Health Check Output:
```json
{
  "status": "HEALTHY",
  "timestamp": "2026-11-21T15:20:00Z",
  "checks": {
    "application": { "status": "healthy", "environment": "production" },
    "database": { "status": "connected", "postgis": "3.4.0" },
    "data_sources": {
      "status": "HEALTHY",
      "background_monitoring_enabled": true,
      "last_sync_status": "SUCCESS"
    },
    "routing_service": { "status": "CONFIGURED", "provider": "OSRM" },
    "notifications": { "status": "OPERATIONAL", "registered_devices": 1420 }
  }
}
```

---

## 10. Database Backup and Disaster Recovery

### Step 10.1: Automated Database Backup
Run daily via cron or scheduled task:
```bash
pg_dump -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -Fc -f "/backups/disaster_safety_$(date +%Y%m%d_%H%M%S).dump"
```

### Step 10.2: Database Restore Procedure
In event of database corruption or hardware failure:
```bash
# 1. Terminate active backend connections to database
psql -h $DB_HOST -U postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'disaster_safety' AND pid <> pg_backend_pid();"

# 2. Restore schema, tables, and PostGIS geometries
pg_restore -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME --clean --if-exists /backups/disaster_safety_TARGET_BACKUP.dump

# 3. Verify PostGIS functions
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "SELECT count(*) FROM crisis_events; SELECT PostGIS_Version();"
```

---

## 11. Common Deployment Failures & Troubleshooting

1. **`PostGIS extension is not installed or enabled (503 Service Unavailable)`**:
   - Cause: Database was created without running `CREATE EXTENSION postgis;`.
   - Resolution: Connect as superuser (`postgres`) to the database and execute `CREATE EXTENSION IF NOT EXISTS postgis;`.
2. **`CORS Error in Browser / Web Inspector`**:
   - Cause: Client domain is not listed in `CORS_ALLOWED_ORIGINS`.
   - Resolution: Update `CORS_ALLOWED_ORIGINS` in `backend/.env` with your frontend domain (e.g. `https://app.disastersafety.in`).
3. **`MapLibre Native Crash on Launch (Expo Go)`**:
   - Cause: MapLibre cannot run inside standard Expo Go.
   - Resolution: Run `npx expo run:android` or use an EAS Development Client (`npx eas build --profile development`).
4. **`Rate Limit Exceeded (HTTP 429)`**:
   - Cause: Client IP sent more than `RATE_LIMIT_PER_MINUTE` requests.
   - Resolution: Check `Retry-After` header or adjust `RATE_LIMIT_PER_MINUTE` in backend settings.
