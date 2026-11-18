"""RGPD — export et suppression compte (M6 S1)."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from bazo_config import db_connect

tz_paris = ZoneInfo("Europe/Paris")


def exporter_donnees_utilisateur(id_telegram: str) -> dict:
    """Export JSON de toutes les données personnelles."""
    id_tg = str(id_telegram)
    conn = db_connect(timeout=15)
    profil = conn.execute(
        "SELECT * FROM inscriptions WHERE id_telegram = ?", (id_tg,)
    ).fetchone()
    cols_insc = [d[1] for d in conn.execute("PRAGMA table_info(inscriptions)").fetchall()]
    profil_dict = dict(zip(cols_insc, profil)) if profil else None

    def _rows(table, where, params):
        try:
            cols = [d[1] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            rows = conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
        except Exception:
            return []
        return [dict(zip(cols, r)) for r in rows]

    export = {
        "export_date": datetime.now(tz_paris).isoformat(),
        "id_telegram": id_tg,
        "profil": profil_dict,
        "trajets_valides": _rows("trajets_valides", "id_telegram = ?", (id_tg,)),
        "rapports_incidents": _rows("rapports_incidents", "id_telegram = ?", (id_tg,)),
        "propositions_radar": _rows("propositions_radar", "id_telegram = ?", (id_tg,)),
        "signalements": _rows("signalements", "id_auteur = ?", (id_tg,)),
        "signalements_votes": _rows("signalements_votes", "id_utilisateur = ?", (id_tg,)),
    }
    conn.close()
    return export


def supprimer_compte_utilisateur(id_telegram: str) -> dict:
    """Supprime toutes les données personnelles (droit à l'effacement)."""
    id_tg = str(id_telegram)
    conn = db_connect(timeout=15)
    counts = {}
    import os
    # Photos de signalement sur disque
    try:
        for (chemin,) in conn.execute(
            "SELECT photo_chemin FROM signalements WHERE id_auteur = ? AND photo_chemin IS NOT NULL", (id_tg,)
        ).fetchall():
            if chemin and os.path.exists(chemin):
                os.remove(chemin)
    except Exception:
        pass
    for table, col in (
        ("trajets_valides", "id_telegram"),
        ("rapports_incidents", "id_telegram"),
        ("propositions_radar", "id_telegram"),
        ("signalements_votes", "id_utilisateur"),
        ("signalements", "id_auteur"),
        ("inscriptions", "id_telegram"),
    ):
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (id_tg,))
            counts[table] = cur.rowcount
        except Exception:
            counts[table] = 0
    conn.commit()
    conn.close()
    return {"ok": True, "id_telegram": id_tg, "supprime": counts}
