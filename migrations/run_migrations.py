"""Run Postgres scripts.sql; also ensure billing tables via SQLAlchemy (Postgres or SQLite)."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

db_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

print(f"Connecting to PostgreSQL at {db_url}...")
try:
    import psycopg2
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cursor = conn.cursor()

    sql_file = os.path.join(os.path.dirname(__file__), "scripts.sql")
    with open(sql_file, "r", encoding="utf-8") as f:
        sql_script = f.read()

    print("Executing migration script: scripts.sql ...")
    cursor.execute(sql_script)
    print("SUCCESS: Master migration script executed successfully on PostgreSQL!")

    cursor.close()
    conn.close()
except Exception as e:
    print(f"PostgreSQL migration error (will still ensure billing tables via SQLAlchemy): {e}")

# Ensure billing tables on whichever engine the app uses (Postgres or SQLite fallback)
try:
    from app.database import engine
    from app.billing import ensure_billing_tables
    ensure_billing_tables(engine)
    print(f"SUCCESS: Billing tables ensured on engine dialect={engine.dialect.name}")
except Exception as e:
    print(f"SQLAlchemy billing ensure error: {e}")
