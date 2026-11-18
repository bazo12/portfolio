"""Couche DB — ping PostgreSQL + métadonnées backend (M16)."""
from __future__ import annotations

from bazo_config import DATABASE_URL, DB_BACKEND, DB_PATH


def backend_info() -> dict:
    return {
        "backend": DB_BACKEND,
        "sqlite_path": DB_PATH if DB_BACKEND == "sqlite" else None,
        "postgres_configured": bool(DATABASE_URL),
    }


def pg_ping() -> tuple[bool, str]:
    """Test connexion PostgreSQL si BAZO_DATABASE_URL défini."""
    if not DATABASE_URL:
        return False, "BAZO_DATABASE_URL non configuré"
    try:
        import psycopg2

        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1 LIMIT 5"
        )
        tables = [r[0] for r in cur.fetchall()]
        conn.close()
        return True, f"PostgreSQL OK ({len(tables)}+ tables)"
    except Exception as e:
        return False, str(e)
