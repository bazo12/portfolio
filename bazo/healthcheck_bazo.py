#!/usr/bin/env python3
"""Sprint 1 — Surveillance bot + radar + API SNCF. Usage: python3 healthcheck_bazo.py [--notify]"""
import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

import requests

from bazo_config import ADMIN_CHAT_ID, DB_PATH, TOKEN_SNCF, TOKEN_TELEGRAM, db_connect
from bazo_metier import sync_trajets_valides_depuis_radar, verifier_coherence_alerte_pdf

COUPLES_TEST = [
    ("Metz", "Luxembourg", "87192039", "82001000"),
    ("Thionville", "Luxembourg", "87191007", "82001000"),
]


def service_actif(unit):
    r = subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True)
    return r.returncode == 0


def check_integrity():
    try:
        conn = db_connect(timeout=5)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if row and row[0] == "ok":
            return True, "Intégrité SQLite OK"
        return False, f"Intégrité : {row[0] if row else '?'}"
    except Exception as e:
        return False, f"Intégrité : {e}"


def check_ocesn():
    try:
        conn = db_connect(timeout=5)
        n = conn.execute(
            "SELECT COUNT(*) FROM historique WHERE COALESCE(numero_train,'') LIKE 'OCESN%'"
        ).fetchone()[0]
        conn.close()
        if n == 0:
            return True, "Aucune ligne parasite OCESN"
        return False, f"{n} ligne(s) parasite(s) OCESN restante(s)"
    except Exception as e:
        return False, f"OCESN check : {e}"


def check_backup():
    from bazo_config import BACKUP_DIR

    dest = BACKUP_DIR
    if not os.path.isdir(dest):
        return False, "Dossier backups absent"
    fichiers = [
        f for f in os.listdir(dest)
        if f.startswith("base_donnees_") and (f.endswith(".db") or f.endswith(".db.gz"))
    ]
    if not fichiers:
        return False, "Aucune sauvegarde DB trouvée"
    fichiers.sort(reverse=True)
    dernier = fichiers[0]
    path = os.path.join(dest, dernier)
    age_h = (datetime.now().timestamp() - os.path.getmtime(path)) / 3600
    if age_h > 26:
        return False, f"Dernière sauvegarde trop ancienne : {dernier} ({age_h:.0f}h)"
    return True, f"Dernière sauvegarde : {dernier} ({age_h:.0f}h)"


def check_db():
    if not os.path.isfile(DB_PATH):
        return False, f"Base absente : {DB_PATH}"
    conn = db_connect(timeout=5)
    for table in ("inscriptions", "historique", "trajets_valides"):
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        if not n:
            conn.close()
            return False, f"Table manquante : {table}"
    cols = [c[1] for c in conn.execute("PRAGMA table_info(trajets_valides)").fetchall()]
    conn.close()
    for col in ("id_telegram", "statut_validation", "retard_arrivee_min"):
        if col not in cols:
            return False, f"Colonne trajets_valides.{col} manquante"
    return True, "SQLite OK"


def check_navitia():
    url = (
        "https://api.sncf.com/v1/coverage/sncf/journeys"
        "?from=stop_area:SNCF:87192039&to=stop_area:SNCF:82001000"
        "&count=3&data_freshness=realtime&datetime_represents=departure"
    )
    try:
        r = requests.get(url, auth=(TOKEN_SNCF, ""), timeout=20)
        if r.status_code != 200:
            return False, f"Navitia HTTP {r.status_code}"
        if not r.json().get("journeys"):
            return False, "Navitia : 0 journey"
        return True, "API SNCF OK"
    except Exception as e:
        return False, f"Navitia : {e}"


def check_coherence_alerte_pdf():
    """Alerte et PDF doivent rester alignés — répare automatiquement si écart détecté."""
    n = verifier_coherence_alerte_pdf()
    if n < 0:
        return False, "Cohérence alerte/PDF : erreur lecture DB"
    if n == 0:
        return True, "Cohérence alerte/PDF OK"
    try:
        conn = db_connect(timeout=10)
        uids = conn.execute(
            """
            SELECT DISTINCT tv.id_telegram FROM trajets_valides tv
            WHERE tv.statut_validation = 'en_attente'
              AND tv.date >= date('now', '-60 days')
              AND (
                EXISTS (SELECT 1 FROM historique h WHERE h.date=tv.date AND h.numero_train=tv.numero_train AND h.type='supprime')
                OR EXISTS (SELECT 1 FROM historique h WHERE h.date=tv.date AND h.numero_train=tv.numero_train
                    AND h.type='retard' AND COALESCE(h.retard_arrivee_min,h.retard_min,0)>=10)
              )
            """
        ).fetchall()
        conn.close()
        for (uid,) in uids:
            if uid:
                sync_trajets_valides_depuis_radar(uid)
        reste = verifier_coherence_alerte_pdf()
        if reste == 0:
            return True, f"Cohérence réparée ({n} trajet(s) resynchronisé(s))"
        return False, f"{reste} trajet(s) encore désaligné(s) alerte/PDF"
    except Exception as e:
        return False, f"Cohérence : {e}"


def check_telegram():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/getMe", timeout=15)
        if r.status_code != 200 or not r.json().get("ok"):
            return False, "Telegram getMe échoué"
        return True, f"Telegram @{r.json()['result'].get('username', '?')}"
    except Exception as e:
        return False, f"Telegram : {e}"


def check_geo_radar():
    from bazo_geo import valider_radar_region

    manquants = valider_radar_region("grand_est")
    if manquants:
        return False, f"UIC radar GE manquants : {', '.join(manquants)}"
    return True, "Radar Grand Est UIC OK"


def check_api_local():
    try:
        r = requests.get("http://127.0.0.1:8080/health", timeout=5)
        if r.status_code != 200:
            return False, f"API HTTP {r.status_code}"
        return True, "API locale OK"
    except Exception as e:
        return False, f"API locale : {e}"


def check_cache():
    from bazo_cache import cache_status

    st = cache_status()
    if not st.get("ok"):
        return False, f"Cache {st.get('backend')} KO"
    return True, f"Cache {st.get('backend')} OK"


def check_postgres():
    from bazo_db import pg_ping

    return pg_ping()


def notifier_admin(texte):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": texte, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as e:
        print(f"Notification admin impossible : {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--notify", action="store_true", help="Alerte Telegram admin si échec")
    args = parser.parse_args()

    checks = [
        ("service:bot", lambda: (service_actif("bazo-bot"), "bazo-bot actif" if service_actif("bazo-bot") else "service bazo-bot ARRÊTÉ")),
        ("service:radar", lambda: (service_actif("bazo-radar"), "bazo-radar actif" if service_actif("bazo-radar") else "service bazo-radar ARRÊTÉ")),
        ("sqlite", check_db),
        ("integrity", check_integrity),
        ("ocesn", check_ocesn),
        ("backup", check_backup),
        ("navitia", check_navitia),
        ("telegram", check_telegram),
        ("geo_radar", check_geo_radar),
        ("cache", check_cache),
        ("coherence_pdf", check_coherence_alerte_pdf),
    ]
    from bazo_config import DATABASE_URL

    if DATABASE_URL:
        checks.insert(-1, ("postgres", check_postgres))
    if service_actif("bazo-api"):
        checks.append(("api_local", check_api_local))
    erreurs = []
    for nom, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, str(e)
        statut = "OK" if ok else "FAIL"
        print(f"[{statut}] {nom}: {msg}")
        if not ok:
            erreurs.append(f"• *{nom}* : {msg}")

    if erreurs and args.notify:
        notifier_admin("🚨 *Bazo Healthcheck*\n\n" + "\n".join(erreurs))

    return 1 if erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
