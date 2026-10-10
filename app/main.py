import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine, SessionLocal
from app.seed import seed_db
from app.routers import auth, reports, doctors, centers, templates, approvals, invoices, case_thread, study_reports, files
from app.routers import public_reports
from app.routers import notifications as notifications_router
from app.routers import center_users, annotations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__) # reload config

# NOTE: No tables or columns are automatically created by SQLAlchemy.
# All schema creation and DDL changes must be executed via migrations/ SQL scripts.

# Optionally seed initial data if tables already exist
db_session = SessionLocal()
try:
    seed_db(db_session)
except Exception as e:
    logger.info(f"Database seed skipped or pending migration: {e}")
finally:
    db_session.close()

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url="/api/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

try:
    from app.security import decode_access_token
except ImportError:
    from app.security import decode_access_token

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Ensure billing tables exist on both Postgres and SQLite fallback
try:
    from app.billing import ensure_billing_tables
except ImportError:
    from app.billing import ensure_billing_tables
try:
    ensure_billing_tables(engine)
except Exception as e:
    logger.warning(f"Billing table ensure skipped: {e}")

try:
    from app.routers.case_thread import ensure_report_comment_tables
except ImportError:
    from app.routers.case_thread import ensure_report_comment_tables
try:
    ensure_report_comment_tables(engine)
except Exception as e:
    logger.warning(f"Report comments table ensure skipped: {e}")

try:
    from app.routers.study_reports import ensure_study_report_columns
except ImportError:
    from app.routers.study_reports import ensure_study_report_columns
try:
    ensure_study_report_columns(engine)
except Exception as e:
    logger.warning(f"Study report columns ensure skipped: {e}")


try:
    from app.report_links import ensure_report_link_tables
except ImportError:
    from app.report_links import ensure_report_link_tables
try:
    ensure_report_link_tables(engine)
except Exception as e:
    logger.warning(f"Report public link table ensure skipped: {e}")

try:
    from app.notifications import ensure_user_notification_tables
except ImportError:
    from app.notifications import ensure_user_notification_tables
try:
    ensure_user_notification_tables(engine)
except Exception as e:
    logger.warning(f"User notifications table ensure skipped: {e}")


# Multi-centre access links (+ backfill of existing centre logins) and PACS image marks
try:
    from app.access import ensure_access_tables
except ImportError:
    from app.access import ensure_access_tables
try:
    ensure_access_tables(engine)
except Exception as e:
    logger.warning(f"Centre access / image annotation table ensure skipped: {e}")


# Mount Routers
app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(study_reports.router, prefix=settings.API_PREFIX)
app.include_router(reports.router, prefix=settings.API_PREFIX)
app.include_router(doctors.router, prefix=settings.API_PREFIX)
app.include_router(centers.router, prefix=settings.API_PREFIX)
app.include_router(templates.router, prefix=settings.API_PREFIX)
app.include_router(approvals.router, prefix=settings.API_PREFIX)
app.include_router(invoices.router, prefix=settings.API_PREFIX)
app.include_router(invoices.billing_router, prefix=settings.API_PREFIX)
app.include_router(case_thread.router, prefix=settings.API_PREFIX)
app.include_router(files.router, prefix=settings.API_PREFIX)
app.include_router(public_reports.router, prefix=settings.API_PREFIX)
app.include_router(notifications_router.router, prefix=settings.API_PREFIX)
app.include_router(center_users.router, prefix=settings.API_PREFIX)
app.include_router(annotations.router, prefix=settings.API_PREFIX)


from fastapi import WebSocket, WebSocketDisconnect, Query
from typing import Optional
try:
    from app.websocket import manager
except ImportError:
    from app.websocket import manager

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(None)):
    if token:
        try:
            payload = decode_access_token(token)
            if not payload:
                await websocket.close(code=1008)
                return
            ws_user_id = payload.get("sub")
        except Exception:
            await websocket.close(code=1008)
            return
    else:
        await websocket.close(code=1008)
        return
    await manager.connect(websocket, ws_user_id)
    try:
        while True:
            # Keep connection open and receive optional messages
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
        manager.disconnect(websocket)

@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "database": engine.dialect.name,
        "fileStorageRoot": settings.FILE_STORAGE_ROOT,
    }
