import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PROJECT_NAME: str = "RadioNet PACS FastAPI Backend"
    VERSION: str = "1.0.0"
    API_PREFIX: str = "/api"
    
    # Environment & Database Safety
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    # Default: allow SQLite only in development. Production must use Postgres.
    ALLOW_SQLITE_FALLBACK: bool = os.getenv(
        "ALLOW_SQLITE_FALLBACK",
        "false" if os.getenv("ENVIRONMENT", "development") == "production" else "true",
    ).lower() == "true"

    # PostgreSQL Database URL (VPS example: postgresql://radionline:...@127.0.0.1:5432/radionline)
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://radionline:postgres@127.0.0.1:5432/radionline"
    )
    # Fallback to SQLite if PostgreSQL service is offline in local dev environment
    SQLITE_FALLBACK_URL: str = "sqlite:///./radionline.db"
    
    CORS_ORIGINS: list[str] = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000",
        ).split(",")
        if origin.strip()
    ]

    # JWT auth
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-insecure-jwt-secret-change-me")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

    # local = write on this machine. sftp = write to the VPS disk (local API, production files).
    FILE_STORAGE_TRANSPORT: str = os.getenv("FILE_STORAGE_TRANSPORT", "local").strip().lower()
    FILE_STORAGE_ROOT: str = os.getenv("FILE_STORAGE_ROOT", "./data/files")
    FILE_PUBLIC_BASE_URL: str = os.getenv("FILE_PUBLIC_BASE_URL", "")
    FILE_SFTP_HOST: str = os.getenv("FILE_SFTP_HOST", "")
    FILE_SFTP_PORT: int = int(os.getenv("FILE_SFTP_PORT", "22"))
    FILE_SFTP_USER: str = os.getenv("FILE_SFTP_USER", "root")
    FILE_SFTP_PASSWORD: str = os.getenv("FILE_SFTP_PASSWORD", "")
    FILE_SFTP_ROOT: str = os.getenv("FILE_SFTP_ROOT", "/var/www/radionline/storage")

    # Public base URL for report QR links (e.g. https://reports.example.com). Empty = the
    # frontend builds the link from its own origin.
    PUBLIC_REPORT_BASE_URL: str = os.getenv("PUBLIC_REPORT_BASE_URL", "").strip().rstrip("/")

settings = Settings()
