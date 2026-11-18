"""Enregistrement tokens push mobile — FCM (M19)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bazo_config import db_connect

tz = ZoneInfo("Europe/Paris")


def ensure_device_schema() -> None:
    conn = db_connect(timeout=10)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_telegram TEXT NOT NULL,
            device_token TEXT NOT NULL,
            plateforme TEXT DEFAULT 'unknown',
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(id_telegram, device_token)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_user ON device_tokens(id_telegram)"
    )
    conn.commit()
    conn.close()


def enregistrer_device(id_telegram: str, device_token: str, plateforme: str = "unknown") -> bool:
    if not id_telegram or not device_token or len(device_token) < 8:
        return False
    ensure_device_schema()
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = db_connect(timeout=10)
        conn.execute(
            """
            INSERT INTO device_tokens (id_telegram, device_token, plateforme, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id_telegram, device_token) DO UPDATE SET
                plateforme = excluded.plateforme,
                updated_at = excluded.updated_at
            """,
            (str(id_telegram), device_token.strip(), plateforme[:32], now, now),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def supprimer_device(id_telegram: str, device_token: str) -> bool:
    ensure_device_schema()
    try:
        conn = db_connect(timeout=10)
        conn.execute(
            "DELETE FROM device_tokens WHERE id_telegram = ? AND device_token = ?",
            (str(id_telegram), device_token.strip()),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def lister_devices(id_telegram: str) -> list[dict]:
    ensure_device_schema()
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """
            SELECT device_token, plateforme, updated_at FROM device_tokens
            WHERE id_telegram = ? ORDER BY updated_at DESC
            """,
            (str(id_telegram),),
        ).fetchall()
        conn.close()
        return [
            {"device_token": r[0], "plateforme": r[1], "updated_at": r[2]}
            for r in rows
        ]
    except Exception:
        return []


def compter_devices_total() -> int:
    ensure_device_schema()
    try:
        conn = db_connect(timeout=5)
        n = conn.execute("SELECT COUNT(*) FROM device_tokens").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0
