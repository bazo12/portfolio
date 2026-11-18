"""Service temps réel Bazo : un cycle toutes les 2 minutes.

  GTFS-RT SNCF (chaque cycle) + CFL mobiliteit.lu (toutes les 5 min) + Navitia ciblé (trains suivis, 6 min)
  -> états fusionnés -> suppression partielle / totale, bascule + retard cumulé, faux maintenus, veille grève.

Lancement : `python temps_reel_service.py` (unité systemd `bazo-temps-reel`).
Variables : BAZO_SIMULATION=1 (aucun envoi Telegram), BAZO_ALERTES_TEMPS_REEL=0 (coupe-circuit envoi),
            BAZO_TEMPS_REEL_INTERVALLE (secondes, défaut 120).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import bazo_greve as G
import bazo_hub as HUB
import bazo_live as LIVE
import bazo_notify
import bazo_sources_rt as S
import bazo_temps_reel as TR
from bazo_config import db_connect

logger = logging.getLogger("bazo.temps_reel.service")
tz_paris = ZoneInfo("Europe/Paris")

INTERVALLE_S = int(os.getenv("BAZO_TEMPS_REEL_INTERVALLE", "120"))
INTERVALLE_CFL_S = 300
INTERVALLE_NAVITIA_TRAIN_S = 360
MAX_NAVITIA_PAR_CYCLE = 8

_cache = {"cfl": {}, "cfl_ts": 0.0, "navitia": {}, "navitia_ts": {}}


def trains_suivis(conn, date_j: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT numero_train FROM trajets_valides WHERE date=? AND origine IN ('checkin','confirme_usager') "
        "AND statut_validation IN ('en_attente','valide_normal','valide_report')", (date_j,),
    ).fetchall()
    return [r[0] for r in rows]


def _noter_sante(conn, sante: dict) -> None:
    conn.execute(
        "INSERT INTO memoire_radar (cle, valeur, updated_at) VALUES ('temps_reel_sante', ?, datetime('now')) "
        "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur, updated_at=excluded.updated_at",
        (json.dumps(sante, ensure_ascii=False),),
    )
    conn.commit()


def cycle(now: datetime | None = None, envoyer=None, sources: dict | None = None) -> dict:
    """Un cycle complet. `sources` ({"gtfs":..., "cfl":..., "navitia":...}) permet de rejouer / tester."""
    TR.init_schema()
    G.init_schema()
    now = now or datetime.now(tz_paris)
    date_j = now.strftime("%Y-%m-%d")
    sante = {"ts": int(now.timestamp()), "gtfs": "ok", "cfl": "ok", "navitia": 0, "erreurs": []}
    gtfs, cfl, nav = {}, {}, {}

    if sources is not None:
        gtfs, cfl, nav = sources.get("gtfs", {}), sources.get("cfl", {}), sources.get("navitia", {})
    else:
        try:
            gtfs = S.lire_gtfs_rt()
        except Exception as e:
            sante["gtfs"] = f"ko: {e}"
            sante["erreurs"].append(f"gtfs: {e}")
            logger.warning("[TEMPS-REEL] GTFS-RT indisponible : %s", e)
        if 4.5 <= now.hour + now.minute / 60 < 24 and time.time() - _cache["cfl_ts"] >= INTERVALLE_CFL_S:
            try:
                fusion = {}
                for gare in ("bettembourg", "luxembourg"):
                    fusion = S.fusionner_sources(fusion, S.lire_cfl_board(gare, maintenant=now)) if fusion \
                        else S.lire_cfl_board(gare, maintenant=now)
                _cache["cfl"], _cache["cfl_ts"] = fusion, time.time()
            except Exception as e:
                sante["cfl"] = f"ko: {e}"
                sante["erreurs"].append(f"cfl: {e}")
                logger.warning("[TEMPS-REEL] CFL indisponible : %s", e)
        cfl = _cache["cfl"]
        conn0 = db_connect(timeout=10)
        try:
            appels = 0
            for numero in trains_suivis(conn0, date_j):
                if appels >= MAX_NAVITIA_PAR_CYCLE:
                    break
                if time.time() - _cache["navitia_ts"].get(numero, 0) < INTERVALLE_NAVITIA_TRAIN_S:
                    if numero in _cache["navitia"]:
                        nav[(date_j, numero)] = _cache["navitia"][numero]
                    continue
                _cache["navitia_ts"][numero] = time.time()
                appels += 1
                etat = S.lire_navitia_train(numero, date_j)
                if etat:
                    _cache["navitia"][numero] = etat
                    nav[(date_j, numero)] = etat
            sante["navitia"] = appels
        finally:
            conn0.close()

    finals = S.fusionner_sources(gtfs, cfl, nav) if (gtfs or cfl or nav) else {}
    conn = db_connect(timeout=15)
    resume = {"trains": len(finals), "impacts": 0, "faux_maintenus": 0, "reports": 0, "greve": {}}
    try:
        inscriptions = TR.charger_inscriptions(conn)
        changements = TR.mettre_a_jour_etats(conn, finals, now)
        resume["changements"] = [(c["type"], c["numero"]) for c in changements]
        for etape, fn in (
            ("impacts", lambda: TR.notifier_impacts(conn, finals, now, envoyer, inscriptions)),
            ("faux_maintenus", lambda: TR.detecter_faux_maintenus(conn, finals, now, envoyer, inscriptions)),
            ("reports", lambda: TR.suivre_reports(conn, finals, now, envoyer)),
        ):
            try:
                resume[etape] = fn()
            except Exception as e:
                logger.exception("[TEMPS-REEL] étape %s en échec", etape)
                sante["erreurs"].append(f"{etape}: {e}")
        if changements:     # hub d'entraide : ouvert automatiquement sur un train supprimé / limité
            try:
                HUB.ouvrir_hubs_depuis_etats(conn, changements, int(now.timestamp()))
            except Exception as e:
                logger.exception("[TEMPS-REEL] ouverture hub en échec")
                sante["erreurs"].append(f"hub: {e}")
        if finals:          # instantané pour l'écran d'accueil mobile (jamais bloquant)
            try:
                LIVE.sauver_snapshot(conn, LIVE.construire_snapshot(finals, int(now.timestamp())))
            except Exception as e:
                logger.exception("[TEMPS-REEL] instantané live en échec")
                sante["erreurs"].append(f"live: {e}")
        try:
            resume["greve"] = G.veille_greve(now, finals, envoyer)
        except Exception as e:
            logger.exception("[TEMPS-REEL] veille grève en échec")
            sante["erreurs"].append(f"greve: {e}")
        cle_hab = f"habituels:{date_j}"
        if now.hour >= 3 and not G._derniere_execution(conn, cle_hab):
            G.deduire_trains_habituels(conn)
            G._noter_execution(conn, cle_hab, int(now.timestamp()))
        sante["trains"] = len(finals)
        _noter_sante(conn, sante)
    finally:
        conn.close()
    return resume


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger.info("Service temps réel démarré (intervalle %ss, simulation=%s, envoi=%s)",
                INTERVALLE_S, bazo_notify.SIMULATION, bazo_notify.envoi_actif())
    while True:
        debut = time.time()
        try:
            r = cycle()
            logger.info("Cycle OK : %s trains, impacts=%s faux_maintenus=%s reports=%s",
                        r["trains"], r["impacts"], r["faux_maintenus"], r["reports"])
        except Exception:
            logger.exception("Cycle en échec")
        time.sleep(max(5, INTERVALLE_S - (time.time() - debut)))


if __name__ == "__main__":
    main()
