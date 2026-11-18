"""Compteur quota API SNCF Navitia — budget journalier configurable."""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from bazo_config import db_connect

tz_paris = ZoneInfo("Europe/Paris")
DAILY_BUDGET = int(os.getenv("BAZO_SNCF_DAILY_BUDGET", "4500"))
RESERVE_CHECKIN = int(os.getenv("BAZO_SNCF_RESERVE_CHECKIN", "400"))


def _cle_jour() -> str:
    return f"sncf_quota_{datetime.now(tz_paris).strftime('%Y-%m-%d')}"


def _ensure_schema() -> None:
    conn = db_connect(timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memoire_radar (
            cle TEXT PRIMARY KEY,
            valeur TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def compter_aujourdhui() -> int:
    _ensure_schema()
    conn = db_connect(timeout=5)
    row = conn.execute("SELECT valeur FROM memoire_radar WHERE cle = ?", (_cle_jour(),)).fetchone()
    conn.close()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def budget_restant() -> int:
    return max(0, DAILY_BUDGET - compter_aujourdhui())


def peut_appeler(source: str = "sncf", reserve: int = 0) -> bool:
    """Vérifie si un appel SNCF est autorisé (budget jour)."""
    limite = DAILY_BUDGET - max(0, reserve)
    return compter_aujourdhui() < limite


def enregistrer_appel(source: str = "sncf", n: int = 1) -> int:
    """Incrémente le compteur. Retourne le total du jour."""
    _ensure_schema()
    cle = _cle_jour()
    conn = db_connect(timeout=5)
    row = conn.execute("SELECT valeur FROM memoire_radar WHERE cle = ?", (cle,)).fetchone()
    total = (int(row[0]) if row else 0) + max(1, n)
    conn.execute(
        """
        INSERT INTO memoire_radar (cle, valeur, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur, updated_at = excluded.updated_at
        """,
        (cle, str(total)),
    )
    conn.commit()
    conn.close()
    return total


def statut_quota() -> dict:
    used = compter_aujourdhui()
    return {
        "budget_jour": DAILY_BUDGET,
        "utilise": used,
        "restant": max(0, DAILY_BUDGET - used),
        "reserve_checkin": RESERVE_CHECKIN,
    }
