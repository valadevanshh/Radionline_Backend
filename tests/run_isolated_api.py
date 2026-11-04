"""Local-only API on 127.0.0.1:8011 backed by a throwaway SQLite file (never Postgres)."""
import os, sys, tempfile
TMP = os.environ.get("MGR_TMP") or tempfile.mkdtemp(prefix="mgr_e2e_")
os.environ.update({
    "DATABASE_URL": "postgresql://nobody:nothing@127.0.0.1:1/none",
    "ALLOW_SQLITE_FALLBACK": "true", "ENVIRONMENT": "development",
    "FILE_STORAGE_TRANSPORT": "local", "FILE_STORAGE_ROOT": os.path.join(TMP, "files"),
    "JWT_SECRET": "e2e-local-secret-e2e-local-secret-123",
    "CORS_ORIGINS": "http://127.0.0.1:3011,http://localhost:3011",
})
sys.path.insert(0, r"C:\Project\Radionline\Radionline_Backend")
os.chdir(TMP)
from app.database import engine, Base
assert engine.dialect.name == "sqlite", engine.url
import app.models
Base.metadata.create_all(bind=engine)
from app.main import app
from app.database import SessionLocal
from app.models import UserDB
from app.security import hash_password
db = SessionLocal()
if not db.query(UserDB).filter(UserDB.email == "sa@e2e.local").first():
    db.add(UserDB(email="sa@e2e.local", name="E2E SA", role="SUPER_ADMIN", metadata_={"password": hash_password("SaPass123!")}))
    db.commit()
db.close()
print("ISOLATED DB:", engine.url, "in", TMP, flush=True)
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=8011, log_level="warning")
