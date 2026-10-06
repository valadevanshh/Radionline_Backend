import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

try:
    from app.config import settings
except ImportError:
    from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Base = declarative_base()

def get_engine():
    try:
        # Try PostgreSQL connection first with robust connection pooling
        engine = create_engine(
            settings.DATABASE_URL,
            pool_size=30,
            max_overflow=50,
            pool_timeout=15,
            pool_recycle=1800,
            pool_pre_ping=True
        )
        with engine.connect() as conn:
            logger.info("Successfully connected to PostgreSQL database.")
        return engine
    except Exception as e:
        if getattr(settings, "ENVIRONMENT", "development") == "production" or not getattr(settings, "ALLOW_SQLITE_FALLBACK", True):
            logger.critical(f"Database connection failed in production mode: {e}")
            raise e
        logger.warning(f"PostgreSQL connection failed ({e}). Falling back to SQLite for local execution.")
        sqlite_engine = create_engine(
            settings.SQLITE_FALLBACK_URL,
            connect_args={"check_same_thread": False}
        )
        return sqlite_engine



engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
