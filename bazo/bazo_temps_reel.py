"""Moteur temps réel Bazo (incident Bettembourg du 04/11/2026).

Fonctions :
  1. Fusion SNCF GTFS-RT + CFL mobiliteit.lu (+ Navitia ciblé) en un état unique par train.
  2. Détection de suppression totale / PARTIELLE (terminus exceptionnel) et événements persistés.
  3. Bascule automatique du suivi usager vers le prochain train réel depuis la gare de rupture.
  4. Retard Réel Cumulé = arrivée réelle du 2e train - arrivée initialement prévue du 1er train.
  5. Règle « faux maintenu » : train immobile > 15 min de retard alors qu'un suivant approche.

Aucun message n'est émis hors du paramètre `envoyer` (injectable : simulation / tests).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import bazo_alternatives as ALT
import bazo_geo
import bazo_notify
import bazo_sources_rt as S
from bazo_config import db_connect

logger = logging.getLogger("bazo.temps_reel")
tz_paris = ZoneInfo("Europe/Paris")

SEUIL_FAUX_MAINTENU_MIN = 15
DELAI_IMMOBILISATION_S = 5 * 60
FENETRE_SUIVANT_MIN = 45
MOTIF_REPORT = "Arrêt à {gare} : changement de train forcé"

_SCHEMA_OK = False


# --------------------------------------------------------------------------
# Schéma
# --------------------------------------------------------------------------
def init_schema() -> None:
    """Création additive (aucune table existante n'est modifiée sauf 3 colonnes ajoutées)."""
    global _SCHEMA_OK
    if _SCHEMA_OK:
        return
    try:     # colonnes `provenance` & co. (phase 1/2) indispensables aux écritures sur trajets_valides
        from bazo_metier import migrer_schema_phase2
        migrer_schema_phase2()
    except Exception as e:
        logger.warning("[SCHEMA] migration phase 2 : %s", e)
    conn = db_connect(timeout=10)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS etats_trains (
            date TEXT NOT NULL, numero_train TEXT NOT NULL, statut TEXT NOT NULL,
            sources TEXT, terminus_prevu TEXT, terminus_effectif TEXT,
            retard_max_min INTEGER DEFAULT 0, detail_json TEXT,
            first_seen_ts INTEGER, updated_ts INTEGER,
            PRIMARY KEY (date, numero_train)
        );
        CREATE TABLE IF NOT EXISTS evenements_trains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, numero_train TEXT NOT NULL, type TEXT NOT NULL,
            gare TEXT, retard_min INTEGER, detail TEXT, source TEXT, ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evt_date_train ON evenements_trains(date, numero_train);
        CREATE TABLE IF NOT EXISTS alertes_temps_reel (
            id_telegram TEXT NOT NULL, date TEXT NOT NULL, cle TEXT NOT NULL, ts INTEGER NOT NULL,
            PRIMARY KEY (id_telegram, date, cle)
        );
        CREATE TABLE IF NOT EXISTS suivi_immobilisation (
            date TEXT NOT NULL, numero_train TEXT NOT NULL, uic_prochain TEXT,
            pred_initiale_ts INTEGER, depuis_ts INTEGER, alerte_ts INTEGER,
            PRIMARY KEY (date, numero_train)
        );
        CREATE TABLE IF NOT EXISTS reports_trajet (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_telegram TEXT NOT NULL, date TEXT NOT NULL, trajet_id INTEGER NOT NULL UNIQUE,
            train_initial TEXT NOT NULL, heure_dep_initiale TEXT, gare_depart TEXT, gare_arrivee TEXT,
            gare_rupture TEXT, arr_prevue_initiale_ts INTEGER,
            train_report TEXT, dep_report_ts INTEGER, arr_prevue_report_ts INTEGER,
            arr_reelle_report_ts INTEGER, retard_cumule_min INTEGER,
            statut TEXT NOT NULL DEFAULT 'en_cours', source TEXT DEFAULT 'auto',
            chaine_json TEXT, created_ts INTEGER, updated_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_reports_statut ON reports_trajet(statut, date);
        CREATE TABLE IF NOT EXISTS trains_habituels (
            id_telegram TEXT NOT NULL, gare_depart TEXT NOT NULL, gare_arrivee TEXT NOT NULL,
            heure_theo TEXT NOT NULL, numero_train TEXT, jours TEXT DEFAULT '',
            nb_obs INTEGER DEFAULT 0, manuel INTEGER DEFAULT 0, updated_ts INTEGER,
            PRIMARY KEY (id_telegram, gare_depart, gare_arrivee, heure_theo)
        );
        """
    )
    cols = {c[1] for c in conn.execute("PRAGMA table_info(trajets_valides)").fetchall()}
    if cols:
        for nom, typ in (("train_report", "TEXT"), ("retard_cumule_min", "INTEGER"), ("gare_rupture", "TEXT")):
            if nom not in cols:
                conn.execute(f"ALTER TABLE trajets_valides ADD COLUMN {nom} {typ}")
    conn.commit()
    conn.close()
    _SCHEMA_OK = True


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
def fmt_duree(minutes: int) -> str:
    m = int(max(0, minutes))
    if m < 60:
        return f"{m} min"
    return f"{m // 60}h{m % 60:02d}"


def calculer_retard_cumule(arrivee_prevue_initiale_ts: int | None, arrivee_reelle_finale_ts: int | None) -> int | None:
    """Retard Réel Cumulé (minutes) = arrivée réelle du dernier train pris - arrivée prévue du 1er train."""
    if not arrivee_prevue_initiale_ts or not arrivee_reelle_finale_ts:
        return None
    return max(0, round((arrivee_reelle_finale_ts - arrivee_prevue_initiale_ts) / 60))


def deja_alerte(conn, id_telegram, date_j: str, cle: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM alertes_temps_reel WHERE id_telegram=? AND date=? AND cle=?",
        (str(id_telegram), date_j, cle),
    ).fetchone() is not None


def marquer_alerte(conn, id_telegram, date_j: str, cle: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alertes_temps_reel (id_telegram, date, cle, ts) VALUES (?,?,?,?)",
        (str(id_telegram), date_j, cle, int(time.time())),
    )


def passage(etat: dict, uic: str, sens: str = "dep") -> tuple[int | None, int | None, int]:
    """(heure planifiée, heure prévue/réelle, retard min) d'un train à une gare."""
    i = S.index_arret(etat, uic)
    if i < 0:
        return None, None, 0
    a = etat["arrets"][i]
    if sens == "dep":
        plan, rt, ret = a.get("plan_dep") or a.get("plan_arr"), a.get("t_dep") or a.get("t_arr"), a.get("ret_dep")
        if ret is None:
            ret = a.get("ret_arr")
    else:
        plan, rt, ret = a.get("plan_arr") or a.get("plan_dep"), a.get("t_arr") or a.get("t_dep"), a.get("ret_arr")
        if ret is None:
            ret = a.get("ret_dep")
    ret = ret or 0
    if plan is None and rt is not None:
        plan = rt - ret * 60
    if rt is None and plan is not None:
        rt = plan + ret * 60
    return plan, rt, ret


def impact_usager(etat: dict, uic_dep: str, uic_arr: str) -> dict | None:
    """Impact de l'état du train sur le segment (uic_dep -> uic_arr) de l'usager.
    {"type": "supprime"|"partiel", "rupture_uic": gare où le trajet s'arrête / ne peut commencer}"""
    i_d, i_a = S.index_arret(etat, uic_dep), S.index_arret(etat, uic_arr)
    if i_d < 0 or i_a <= i_d:
        return None
    ar = etat["arrets"]
    if etat.get("annule") or ar[i_d].get("skipped"):
        return {"type": "supprime", "rupture_uic": uic_dep}
    if ar[i_a].get("skipped"):
        derniere = uic_dep
        for k in range(i_d, i_a):
            if not ar[k].get("skipped"):
                derniere = ar[k]["uic"]
        return {"type": "partiel", "rupture_uic": derniere}
    return None


def _fenetre(h: str | None) -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{1,2})h?\s*-\s*(\d{1,2})h?\s*$", str(h or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _jours_ok(jours: str | None, now: datetime) -> bool:
    lst = [int(x) for x in str(jours or "").split(",") if x.strip().isdigit()]
    return True if not lst else now.weekday() in lst


# --------------------------------------------------------------------------
# Ciblage des usagers concernés par un train
# --------------------------------------------------------------------------
def charger_inscriptions(conn) -> dict:
    cur = conn.execute(
        "SELECT id_telegram, gare_depart, heures, gare_retour, heures_retour, jours_actifs, "
        "alertes_actives, vacances_jusquau FROM inscriptions"
    )
    out = {}
    for r in cur.fetchall():
        out[str(r[0])] = {
            "id_telegram": str(r[0]), "gare_depart": r[1], "heures": r[2], "gare_retour": r[3],
            "heures_retour": r[4], "jours_actifs": r[5],
            "alertes_actives": 1 if r[6] is None else r[6], "vacances_jusquau": r[7],
        }
    return out


def peut_recevoir(user: dict | None, now: datetime) -> bool:
    if not user or not bazo_notify.est_chat_telegram(user["id_telegram"]):
        return False
    from bazo_metier import utilisateur_peut_recevoir_alertes
    return utilisateur_peut_recevoir_alertes(user, now)


def cibles_train(conn, etat: dict, now: datetime, inscriptions: dict | None = None) -> list[dict]:
    """Usagers concernés par le train : check-in > train habituel > profil (créneau + segment)."""
    inscriptions = inscriptions if inscriptions is not None else charger_inscriptions(conn)
    date_j, numero = etat["date"], etat["numero"]
    cibles: dict[str, dict] = {}

    for r in conn.execute(
        """SELECT id, id_telegram, heure_theo, gare_depart, gare_arrivee, heure_arrivee_theo, statut_validation
           FROM trajets_valides
           WHERE date = ? AND numero_train = ? AND origine IN ('checkin', 'confirme_usager')
             AND statut_validation IN ('en_attente', 'valide_normal', 'valide_report')""",
        (date_j, numero),
    ).fetchall():
        u_dep = bazo_geo.uic_pour_gare(r[3] or "", "grand_est")
        u_arr = bazo_geo.uic_pour_gare(r[4] or "", "grand_est")
        if u_dep and u_arr:
            cibles.setdefault(str(r[1]), {
                "id_telegram": str(r[1]), "kind": "checkin", "trajet_id": r[0], "heure_theo": r[2],
                "uic_dep": u_dep, "uic_arr": u_arr, "gare_depart": r[3], "gare_arrivee": r[4],
                "statut": r[6],
            })

    for r in conn.execute(
        "SELECT id_telegram, gare_depart, gare_arrivee, heure_theo, numero_train, jours FROM trains_habituels"
    ).fetchall():
        idt = str(r[0])
        if idt in cibles or not _jours_ok(r[5], now):
            continue
        u_dep = bazo_geo.uic_pour_gare(r[1], "grand_est")
        u_arr = bazo_geo.uic_pour_gare(r[2], "grand_est")
        if not u_dep or not u_arr or S.index_arret(etat, u_dep) < 0 or S.index_arret(etat, u_arr) <= S.index_arret(etat, u_dep):
            continue
        plan, _rt, _ret = passage(etat, u_dep, "dep")
        if (r[4] and str(r[4]) == numero) or (plan and S.epoch_vers_hhmm(plan) == r[3]):
            cibles[idt] = {
                "id_telegram": idt, "kind": "habituel", "trajet_id": None, "heure_theo": r[3],
                "uic_dep": u_dep, "uic_arr": u_arr, "gare_depart": r[1], "gare_arrivee": r[2],
            }

    for idt, u in inscriptions.items():
        if idt in cibles or not _jours_ok(u.get("jours_actifs"), now):
            continue
        for g_d, g_a, h in ((u["gare_depart"], u["gare_retour"], u["heures"]),
                            (u["gare_retour"], u["gare_depart"], u["heures_retour"])):
            fen = _fenetre(h)
            u_dep = bazo_geo.uic_pour_gare(g_d or "", "grand_est")
            u_arr = bazo_geo.uic_pour_gare(g_a or "", "grand_est")
            if not fen or not u_dep or not u_arr:
                continue
            if S.index_arret(etat, u_dep) < 0 or S.index_arret(etat, u_arr) <= S.index_arret(etat, u_dep):
                continue
            plan, _rt, _ret = passage(etat, u_dep, "dep")
            if not plan:
                continue
            heure = datetime.fromtimestamp(plan, tz_paris).hour
            if fen[0] <= heure < fen[1]:
                cibles[idt] = {
                    "id_telegram": idt, "kind": "profil", "trajet_id": None,
                    "heure_theo": S.epoch_vers_hhmm(plan), "uic_dep": u_dep, "uic_arr": u_arr,
                    "gare_depart": g_d, "gare_arrivee": g_a,
                }
                break

    return [c for c in cibles.values() if peut_recevoir(inscriptions.get(c["id_telegram"]), now)]


# --------------------------------------------------------------------------
# États persistés + événements
# --------------------------------------------------------------------------
def enregistrer_evenement(conn, date_j, numero, type_, gare=None, retard=None, detail=None, source=None) -> None:
    conn.execute(
        "INSERT INTO evenements_trains (date, numero_train, type, gare, retard_min, detail, source, ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (date_j, numero, type_, gare, retard, detail, source, int(time.time())),
    )


def mettre_a_jour_etats(conn, finals: dict, now: datetime) -> list[dict]:
    """Persiste l'état de chaque train suivi et retourne les changements significatifs."""
    changements = []
    ts = int(now.timestamp())
    for (date_j, numero), e in finals.items():
        statut = S.classifier(e)
        t_prevu, t_eff = S.terminus_prevu_uic(e), S.terminus_effectif_uic(e)
        rmax = S.retard_max(e)
        row = conn.execute(
            "SELECT statut, terminus_effectif, retard_max_min, first_seen_ts FROM etats_trains "
            "WHERE date=? AND numero_train=?", (date_j, numero),
        ).fetchone()
        sources = ",".join(e.get("sources", []))
        if row is None:
            conn.execute(
                "INSERT INTO etats_trains (date, numero_train, statut, sources, terminus_prevu, terminus_effectif, "
                "retard_max_min, first_seen_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (date_j, numero, statut, sources, t_prevu, t_eff, rmax, ts, ts),
            )
            ancien_statut, ancien_term, ancien_ret = "normal", t_prevu, 0
        else:
            ancien_statut, ancien_term, ancien_ret = row[0], row[1], row[2] or 0
            conn.execute(
                "UPDATE etats_trains SET statut=?, sources=?, terminus_effectif=?, retard_max_min=?, updated_ts=? "
                "WHERE date=? AND numero_train=?",
                (statut, sources, t_eff, rmax, ts, date_j, numero),
            )
        if statut == "supprime" and ancien_statut != "supprime":
            changements.append({"type": "suppression_totale", "date": date_j, "numero": numero, "etat": e})
            enregistrer_evenement(conn, date_j, numero, "suppression_totale", None, None, ",".join(e["sources"]), sources)
        elif statut == "partiel" and (ancien_statut != "partiel" or ancien_term != t_eff):
            changements.append({"type": "suppression_partielle", "date": date_j, "numero": numero, "etat": e})
            enregistrer_evenement(
                conn, date_j, numero, "suppression_partielle", S.nom_gare(t_eff), None,
                f"terminus exceptionnel {S.nom_gare(t_eff)} au lieu de {S.nom_gare(t_prevu)}", sources,
            )
        elif rmax >= 15 and rmax >= ancien_ret + 15 or (rmax >= 15 > ancien_ret):
            enregistrer_evenement(conn, date_j, numero, "retard", None, rmax, None, sources)
    conn.commit()
    return changements


# --------------------------------------------------------------------------
# Remplaçant, bascule, report
# --------------------------------------------------------------------------
def _ts_hhmm(date_j: str, ts: int | None) -> str | None:
    return S.epoch_vers_hhmm(ts)


def trouver_remplacant(finals: dict, date_j: str, uic_from: str, uic_to: str, ref_ts: int,
                       exclure: set[str], now: datetime, avec_navitia: bool = True) -> dict | None:
    """Premier train réel desservant uic_from -> uic_to, partant après ref_ts (états temps réel puis Navitia)."""
    cands = ALT.candidats_depuis_etats(finals, date_j, uic_from, uic_to)
    cands = ALT.filtrer_candidats(cands, finals, date_j, exclure)
    for c in cands:
        if c["dep_rt"] >= ref_ts - 60:
            return c
    if avec_navitia:
        depuis = datetime.fromtimestamp(ref_ts - 600, tz_paris)
        nav = ALT.filtrer_candidats(ALT.journeys_directs(uic_from, uic_to, depuis, count=8), finals, date_j, exclure)
        for c in nav:
            if c["dep_rt"] >= ref_ts - 60:
                return c
    return None


def _maj_trajet_report(conn, tid: int, report: dict, gare_rupture_nom: str, final: bool) -> None:
    h_reelle = S.epoch_vers_hhmm(report["arr_reelle_report_ts"])
    conn.execute(
        """UPDATE trajets_valides SET statut_validation = 'valide_report', train_report = ?, gare_rupture = ?,
               retard_cumule_min = ?, retard_arrivee_min = ?, heure_reelle_arrivee = ?,
               motif_validation = ?, provenance = 'officielle'
           WHERE id = ?""",
        (report["train_report"], gare_rupture_nom, report["retard_cumule_min"], report["retard_cumule_min"],
         h_reelle, MOTIF_REPORT.format(gare=gare_rupture_nom), tid),
    )


def enregistrer_report(conn, cible: dict, etat1: dict, rupture_uic: str, cand: dict, now_ts: int,
                       source: str = "auto") -> dict:
    """Crée / met à jour le report d'un trajet et recalcule le retard cumulé provisoire."""
    uic_arr = cible["uic_arr"]
    _p, _r, _ = passage(etat1, uic_arr, "arr")
    plan_arr_1, _rt1, _ret1 = passage(etat1, uic_arr, "arr")
    if not plan_arr_1:
        row = conn.execute("SELECT heure_arrivee_theo FROM trajets_valides WHERE id=?", (cible["trajet_id"],)).fetchone()
        if row and row[0]:
            plan_arr_1 = S.hhmm_vers_epoch(etat1["date"], row[0])
    existant = conn.execute(
        "SELECT id, arr_prevue_initiale_ts, chaine_json FROM reports_trajet WHERE trajet_id=?", (cible["trajet_id"],)
    ).fetchone()
    arr_init = existant[1] if existant and existant[1] else plan_arr_1
    chaine = json.loads(existant[2]) if existant and existant[2] else [etat1["numero"]]
    if cand["numero"] not in chaine:
        chaine.append(cand["numero"])
    cumul = calculer_retard_cumule(arr_init, cand["arr_rt"])
    rupture_nom = S.nom_gare(rupture_uic)
    champs = {
        "train_report": cand["numero"], "dep_report_ts": cand["dep_rt"], "arr_prevue_report_ts": cand["arr_plan"],
        "arr_reelle_report_ts": cand["arr_rt"], "retard_cumule_min": cumul,
    }
    if existant:
        conn.execute(
            """UPDATE reports_trajet SET gare_rupture=?, train_report=?, dep_report_ts=?, arr_prevue_report_ts=?,
                   arr_reelle_report_ts=?, retard_cumule_min=?, statut='en_cours', chaine_json=?, updated_ts=?
               WHERE trajet_id=?""",
            (rupture_nom, champs["train_report"], champs["dep_report_ts"], champs["arr_prevue_report_ts"],
             champs["arr_reelle_report_ts"], cumul, json.dumps(chaine), now_ts, cible["trajet_id"]),
        )
    else:
        conn.execute(
            """INSERT INTO reports_trajet (id_telegram, date, trajet_id, train_initial, heure_dep_initiale,
                   gare_depart, gare_arrivee, gare_rupture, arr_prevue_initiale_ts, train_report, dep_report_ts,
                   arr_prevue_report_ts, arr_reelle_report_ts, retard_cumule_min, statut, source, chaine_json,
                   created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'en_cours',?,?,?,?)""",
            (cible["id_telegram"], etat1["date"], cible["trajet_id"], etat1["numero"], cible.get("heure_theo"),
             cible["gare_depart"], cible["gare_arrivee"], rupture_nom, arr_init, champs["train_report"],
             champs["dep_report_ts"], champs["arr_prevue_report_ts"], champs["arr_reelle_report_ts"], cumul,
             source, json.dumps(chaine), now_ts, now_ts),
        )
    _maj_trajet_report(conn, cible["trajet_id"], champs, rupture_nom, final=False)
    return {**champs, "arr_prevue_initiale_ts": arr_init, "gare_rupture": rupture_nom, "chaine": chaine}


def _nom(g: str) -> str:
    return bazo_geo.nom_canonique_gare(g) or g


def message_impact(cible: dict, etat: dict, impact: dict, alt_texte: str | None, report: dict | None,
                   cand: dict | None) -> str:
    num = etat["numero"]
    heure = cible.get("heure_theo") or ""
    nom_rupt = S.nom_gare(impact["rupture_uic"])
    if impact["type"] == "partiel":
        nds = [S.nom_gare(a["uic"]) for a in etat["arrets"] if a.get("skipped")
               and S.index_arret(etat, cible["uic_dep"]) < S.index_arret(etat, a["uic"]) <= S.index_arret(etat, cible["uic_arr"])]
        titre = f"🚨 *Train {num} limité à {nom_rupt}*"
        corps = (f"Ton train de *{heure}* ne ira pas jusqu'à *{_nom(cible['gare_arrivee'])}*"
                 + (f" (non desservi : {', '.join(nds)})" if nds else "") + ".")
    else:
        titre = f"❌ *Train {num} supprimé*"
        corps = f"Ton train de *{heure}* au départ de *{_nom(cible['gare_depart'])}* ne circule pas."
    lignes = [titre, "", corps]
    if report and cand:
        lignes += [
            "",
            f"🔁 *Suivi basculé sur le train {cand['numero']}* depuis {nom_rupt} :",
            f"départ *{S.epoch_vers_hhmm(cand['dep_rt'])}*"
            + (f" (+{cand['ret_dep']} min)" if cand.get("ret_dep", 0) >= 3 else "")
            + f", arrivée {_nom(cible['gare_arrivee'])} estimée *{S.epoch_vers_hhmm(cand['arr_rt'])}*.",
            f"⏱️ Retard cumulé estimé : *{fmt_duree(report['retard_cumule_min'] or 0)}* "
            "(arrivée réelle du 2e train − arrivée prévue du 1er).",
            "_Il sera enregistré dans ton dossier PDF quand tu seras arrivé._",
        ]
    elif alt_texte:
        lignes += ["", alt_texte]
    elif cible["kind"] == "checkin":
        lignes += ["", "⚠️ Aucun train de remplacement direct identifié pour l'instant. Je continue de chercher."]
    return "\n".join(lignes)


def notifier_impacts(conn, finals: dict, now: datetime, envoyer=None, inscriptions: dict | None = None) -> int:
    """Pour chaque train supprimé / limité : prévient les usagers concernés (une seule fois) et bascule le
    suivi des check-in vers le train suivant. Retourne le nombre de messages émis."""
    envoyer = envoyer or bazo_notify.envoyer
    inscriptions = inscriptions if inscriptions is not None else charger_inscriptions(conn)
    nb = 0
    now_ts = int(now.timestamp())
    for (date_j, numero), etat in sorted(finals.items()):
        statut = S.classifier(etat)
        if statut not in ("supprime", "partiel"):
            continue
        # Confiance : une annulation vue par une seule source est quand même alertée (principe de précaution),
        # un terminus partiel exige que le dernier arrêt desservi soit lisible.
        radar_a_alerte = conn.execute(
            "SELECT 1 FROM historique WHERE date=? AND numero_train=? AND type='supprime' LIMIT 1", (date_j, numero)
        ).fetchone() is not None
        for cible in cibles_train(conn, etat, now, inscriptions):
            impact = impact_usager(etat, cible["uic_dep"], cible["uic_arr"])
            if not impact:
                continue
            plan_dep, _rt, _r = passage(etat, cible["uic_dep"], "dep")
            if cible["kind"] != "checkin" and plan_dep and plan_dep < now_ts - 30 * 60:
                continue
            if cible["kind"] == "profil" and radar_a_alerte and impact["type"] == "supprime":
                continue
            cle = f"impact:{numero}:{impact['type']}:{impact['rupture_uic']}"
            if deja_alerte(conn, cible["id_telegram"], date_j, cle):
                continue
            cle_horaire = f"{cle}:sans_remplacant:h{now.strftime('%H')}"
            ref_ts = now_ts
            _p, rt_rupt, _ = passage(etat, impact["rupture_uic"], "arr" if impact["type"] == "partiel" else "dep")
            if rt_rupt:
                ref_ts = max(now_ts, rt_rupt) if impact["type"] == "partiel" else max(now_ts - 120, rt_rupt - 600)
            report = cand = alt_texte = None
            if cible["kind"] == "checkin":
                cand = trouver_remplacant(finals, date_j, impact["rupture_uic"], cible["uic_arr"], ref_ts,
                                          {numero}, now)
                if cand:
                    report = enregistrer_report(conn, cible, etat, impact["rupture_uic"], cand, now_ts)
                elif deja_alerte(conn, cible["id_telegram"], date_j, cle_horaire):
                    continue          # déjà prévenu cette heure, on réessaiera à l'heure suivante
            else:
                alts = ALT.chercher_alternatives(
                    date_j, impact["rupture_uic"], cible["uic_arr"], ref_ts, now, finals=finals, exclure={numero},
                )
                alt_texte = alts["texte"]
            msg = message_impact(cible, etat, impact, alt_texte, report, cand)
            if envoyer(cible["id_telegram"], msg):
                nb += 1
                if cand or cible["kind"] != "checkin":
                    marquer_alerte(conn, cible["id_telegram"], date_j, cle)
                else:
                    marquer_alerte(conn, cible["id_telegram"], date_j, cle_horaire)
            conn.commit()
    return nb


def suivre_reports(conn, finals: dict, now: datetime, envoyer=None) -> int:
    """Met à jour les reports en cours : estimation d'arrivée, re-bascule si le 2e train est aussi
    perturbé, finalisation du retard cumulé à l'arrivée."""
    envoyer = envoyer or bazo_notify.envoyer
    now_ts = int(now.timestamp())
    nb = 0
    rows = conn.execute(
        "SELECT id, id_telegram, date, trajet_id, train_initial, gare_depart, gare_arrivee, gare_rupture, "
        "arr_prevue_initiale_ts, train_report, arr_reelle_report_ts, retard_cumule_min, heure_dep_initiale, chaine_json "
        "FROM reports_trajet WHERE statut='en_cours'"
    ).fetchall()
    for (rid, idt, date_j, tid, t1, g_dep, g_arr, g_rupt, arr_init, t2, arr_reelle, cumul_prec, h_init, chaine) in rows:
        u_arr = bazo_geo.uic_pour_gare(g_arr or "", "grand_est")
        u_dep = bazo_geo.uic_pour_gare(g_dep or "", "grand_est")
        etat2 = finals.get((date_j, t2))
        arr_prec = arr_reelle
        cible = {"id_telegram": idt, "trajet_id": tid, "uic_arr": u_arr, "uic_dep": u_dep,
                 "gare_depart": g_dep, "gare_arrivee": g_arr, "heure_theo": h_init, "kind": "checkin"}
        if etat2 and u_arr:
            u_rupt2 = bazo_geo.uic_pour_gare(g_rupt or "", "grand_est") or u_dep
            imp2 = impact_usager(etat2, u_rupt2, u_arr)
            if imp2:   # le train de report est lui aussi supprimé / limité : nouvelle bascule
                cand = trouver_remplacant(finals, date_j, imp2["rupture_uic"], u_arr, now_ts, {t1, t2}, now)
                if cand:
                    report = enregistrer_report(conn, cible, etat2, imp2["rupture_uic"], cand, now_ts)
                    enregistrer_evenement(conn, date_j, t2, "re_bascule", S.nom_gare(imp2["rupture_uic"]), None,
                                          f"report {t2} -> {cand['numero']}", "temps_reel")
                    if envoyer(idt, message_impact(cible, etat2, imp2, None, report, cand)):
                        nb += 1
                    conn.commit()
                    continue
            _p, rt_arr, _ret = passage(etat2, u_arr, "arr")
            if rt_arr:
                arr_reelle = rt_arr
        if arr_reelle and arr_init and arr_reelle != arr_prec:
            cumul = calculer_retard_cumule(arr_init, arr_reelle)
            conn.execute("UPDATE reports_trajet SET arr_reelle_report_ts=?, retard_cumule_min=?, updated_ts=? WHERE id=?",
                         (arr_reelle, cumul, now_ts, rid))
            _maj_trajet_report(conn, tid, {"train_report": t2, "arr_reelle_report_ts": arr_reelle,
                                           "retard_cumule_min": cumul}, g_rupt or "", False)
        if arr_reelle and now_ts >= arr_reelle + 120:
            cumul = calculer_retard_cumule(arr_init, arr_reelle)
            conn.execute("UPDATE reports_trajet SET statut='calcule', retard_cumule_min=?, updated_ts=? WHERE id=?",
                         (cumul, now_ts, rid))
            _maj_trajet_report(conn, tid, {"train_report": t2, "arr_reelle_report_ts": arr_reelle,
                                           "retard_cumule_min": cumul}, g_rupt or "", True)
            enregistrer_evenement(conn, date_j, t1, "retard_cumule", _nom(g_arr or ""), cumul,
                                  f"{t1} -> {t2}", "temps_reel")
            msg = (f"✅ *Retard réel cumulé enregistré : {fmt_duree(cumul or 0)}*\n\n"
                   f"🚆 Train {t1} ({h_init}) arrêté à {g_rupt}, puis train {t2}.\n"
                   f"🕒 Arrivée prévue à {_nom(g_arr or '')} : {S.epoch_vers_hhmm(arr_init)}\n"
                   f"🕒 Arrivée réelle : {S.epoch_vers_hhmm(arr_reelle)}\n\n"
                   "_Ajouté à ton dossier PDF._")
            if envoyer(idt, msg):
                nb += 1
            conn.commit()
        conn.commit()
    return nb


# --------------------------------------------------------------------------
# Règle « faux maintenu »
# --------------------------------------------------------------------------
def prochain_arret(etat: dict, now_ts: int) -> tuple[str | None, int | None, int]:
    """Premier arrêt desservi dont le passage prévu n'est pas encore dépassé (uic, heure prévue, retard)."""
    for a in etat.get("arrets", []):
        if a.get("skipped"):
            continue
        rt = a.get("t_arr") or a.get("t_dep")
        if rt is None:
            plan = a.get("plan_arr") or a.get("plan_dep")
            r = S.retard_arret(a) or 0
            rt = plan + r * 60 if plan else None
        if rt and rt >= now_ts - 60:
            return a["uic"], rt, S.retard_arret(a) or 0
    return None, None, 0


def detecter_faux_maintenus(conn, finals: dict, now: datetime, envoyer=None, inscriptions: dict | None = None) -> int:
    """Train immobile (>15 min de retard, prochain arrêt toujours pas atteint 5 min après la prévision initiale)
    alors qu'un train suivant atteint la même gare plus tôt : alerte préventive."""
    envoyer = envoyer or bazo_notify.envoyer
    inscriptions = inscriptions if inscriptions is not None else charger_inscriptions(conn)
    now_ts = int(now.timestamp())
    nb = 0
    for (date_j, numero), etat in sorted(finals.items()):
        if etat.get("annule") or date_j != now.strftime("%Y-%m-%d"):
            continue
        uic_x, pred, ret = prochain_arret(etat, now_ts)
        if not uic_x or ret < SEUIL_FAUX_MAINTENU_MIN:
            conn.execute("DELETE FROM suivi_immobilisation WHERE date=? AND numero_train=?", (date_j, numero))
            continue
        suivi = conn.execute(
            "SELECT uic_prochain, pred_initiale_ts, depuis_ts, alerte_ts FROM suivi_immobilisation WHERE date=? AND numero_train=?",
            (date_j, numero),
        ).fetchone()
        if suivi is None or suivi[0] != uic_x:
            conn.execute(
                "INSERT OR REPLACE INTO suivi_immobilisation (date, numero_train, uic_prochain, pred_initiale_ts, depuis_ts) "
                "VALUES (?,?,?,?,?)", (date_j, numero, uic_x, pred, now_ts),
            )
            continue
        pred_init, depuis, alerte = suivi[1], suivi[2], suivi[3]
        if alerte or now_ts < (pred_init or now_ts) + DELAI_IMMOBILISATION_S:
            continue
        plan_x, _rt_x, _r = passage(etat, uic_x, "arr")
        i_x = S.index_arret(etat, uic_x)
        uic_y = next((a["uic"] for a in etat["arrets"][i_x + 1:] if not a.get("skipped")), None)
        suivant = None
        for (d2, n2), e2 in finals.items():
            if d2 != date_j or n2 == numero or e2.get("annule"):
                continue
            i2 = S.index_arret(e2, uic_x)
            if i2 < 0 or e2["arrets"][i2].get("skipped") or not plan_x:
                continue
            # même sens de circulation : le suivant dessert aussi la gare qui suit uic_x
            if uic_y and not 0 <= i2 < S.index_arret(e2, uic_y):
                continue
            plan2, rt2, ret2 = passage(e2, uic_x, "arr")
            if not plan2 or not rt2 or not (plan_x < plan2 <= plan_x + FENETRE_SUIVANT_MIN * 60):
                continue
            if rt2 < now_ts - 120 or rt2 > now_ts + FENETRE_SUIVANT_MIN * 60 or ret2 > ret - 5:
                continue
            if suivant is None or rt2 < suivant[2]:
                suivant = (n2, e2, rt2, ret2, i2)
        if not suivant:
            continue
        conn.execute("UPDATE suivi_immobilisation SET alerte_ts=? WHERE date=? AND numero_train=?",
                     (now_ts, date_j, numero))
        enregistrer_evenement(conn, date_j, numero, "faux_maintenu", S.nom_gare(uic_x), ret,
                              f"suivant {suivant[0]}", "temps_reel")
        n2, e2, rt2, ret2, i2 = suivant
        for cible in cibles_train(conn, etat, now, inscriptions):
            cle = f"faux_maintenu:{numero}:{uic_x}"
            i_dep_u = S.index_arret(etat, cible["uic_dep"])
            if deja_alerte(conn, cible["id_telegram"], date_j, cle) or i_dep_u > i_x:
                continue                    # déjà prévenu, ou l'usager monte plus loin
            _pa, rt_dest, _ra = passage(e2, cible["uic_arr"], "arr")
            if S.index_arret(e2, cible["uic_arr"]) <= i2:
                continue
            lignes = [
                "⏸️ *Train à l'arrêt : risque de « faux maintenu »*", "",
                f"🚆 Le train *{numero}* est annoncé *+{ret} min* et ne progresse plus vers *{S.nom_gare(uic_x)}* "
                f"(prévu {S.epoch_vers_hhmm(pred_init)}, toujours pas atteint).",
                f"🚆 Le train suivant *{n2}* atteint {S.nom_gare(uic_x)} à *{S.epoch_vers_hhmm(rt2)}*"
                + (f" (+{ret2} min)" if ret2 >= 3 else "")
                + (f" et {_nom(cible['gare_arrivee'])} vers *{S.epoch_vers_hhmm(rt_dest)}*." if rt_dest else "."),
                "", "👉 Surveille les annonces en gare ou passe sur le train suivant plutôt que d'attendre.",
            ]
            if envoyer(cible["id_telegram"], "\n".join(lignes)):
                nb += 1
                marquer_alerte(conn, cible["id_telegram"], date_j, cle)
        conn.commit()
    conn.commit()
    return nb


# --------------------------------------------------------------------------
# Report manuel / rétroactif (incident déjà passé, ex. Bettembourg 04/11)
# --------------------------------------------------------------------------
def etat_depuis_archives(date_j: str, numero: str, dossier: str = "archives_gtfs") -> dict | None:
    """Dernier état connu du train dans les archives GTFS-RT du jour (boîte noire, 7 jours)."""
    import glob
    fichiers = sorted(glob.glob(os.path.join(dossier, f"sncf_{date_j}_*.pb")))
    dernier = None
    for f in fichiers:
        try:
            with open(f, "rb") as fh:
                etats = S.lire_gtfs_rt(contenu=fh.read())
        except Exception as e:  # fichier corrompu
            logger.warning("[ARCHIVE] %s illisible : %s", f, e)
            continue
        if (date_j, numero) in etats:
            dernier = etats[(date_j, numero)]
    return dernier


def calculer_report_manuel(date_j: str, train1: str, train2: str, uic_dep: str, uic_arr: str,
                           etat1: dict | None = None, etat2: dict | None = None,
                           arrivee_reelle_hhmm: str | None = None) -> dict:
    """Retard cumulé pour un changement de train déjà effectué. Sources : états fournis, sinon archives."""
    etat1 = etat1 or etat_depuis_archives(date_j, train1)
    etat2 = etat2 or etat_depuis_archives(date_j, train2)
    if not etat1:
        raise ValueError(f"train {train1} introuvable dans les données du {date_j}")
    plan_arr_1, _rt, _ = passage(etat1, uic_arr, "arr")
    if arrivee_reelle_hhmm:
        arr_reelle = S.hhmm_vers_epoch(date_j, arrivee_reelle_hhmm)
        origine = "saisie"
    else:
        if not etat2:
            raise ValueError(f"train {train2} introuvable dans les données du {date_j} : précise l'arrivée réelle")
        _p, arr_reelle, _r = passage(etat2, uic_arr, "arr")
        origine = "gtfs_rt_archive"
    cumul = calculer_retard_cumule(plan_arr_1, arr_reelle)
    return {
        "date": date_j, "train1": train1, "train2": train2,
        "arrivee_prevue_train1": S.epoch_vers_hhmm(plan_arr_1), "arrivee_reelle_train2": S.epoch_vers_hhmm(arr_reelle),
        "arr_prevue_ts": plan_arr_1, "arr_reelle_ts": arr_reelle,
        "retard_cumule_min": cumul, "origine_donnee": origine,
    }


def enregistrer_report_manuel(id_telegram: str, date_j: str, train1: str, train2: str, gare_depart: str,
                              gare_arrivee: str, gare_rupture: str, arrivee_reelle_hhmm: str | None = None,
                              etat1: dict | None = None, etat2: dict | None = None) -> dict:
    """Enregistre en base le retard cumulé d'un changement de train réel (déclaré ou reconstitué)."""
    init_schema()
    u_dep = bazo_geo.uic_pour_gare(gare_depart, "grand_est")
    u_arr = bazo_geo.uic_pour_gare(gare_arrivee, "grand_est")
    res = calculer_report_manuel(date_j, train1, train2, u_dep, u_arr, etat1, etat2, arrivee_reelle_hhmm)
    e1 = etat1 or etat_depuis_archives(date_j, train1)
    plan_dep, _rt, _ = passage(e1, u_dep, "dep")
    h_theo = S.epoch_vers_hhmm(plan_dep) or "00h00"
    now_ts = int(time.time())
    conn = db_connect(timeout=10)
    row = conn.execute(
        "SELECT id FROM trajets_valides WHERE id_telegram=? AND date=? AND numero_train=? AND heure_theo=?",
        (str(id_telegram), date_j, train1, h_theo),
    ).fetchone()
    if row:
        tid = row[0]
    else:
        cur = conn.execute(
            """INSERT INTO trajets_valides (id_telegram, numero_train, date, heure_theo, statut_validation,
                   gare_depart, gare_arrivee, destination, heure_arrivee_theo, origine, provenance)
               VALUES (?,?,?,?, 'en_attente', ?,?,?,?, 'confirme_usager', 'officielle')""",
            (str(id_telegram), train1, date_j, h_theo, bazo_geo.nom_canonique_gare(gare_depart),
             bazo_geo.nom_canonique_gare(gare_arrivee), bazo_geo.nom_canonique_gare(gare_arrivee),
             res["arrivee_prevue_train1"]),
        )
        tid = cur.lastrowid
    cible = {"id_telegram": str(id_telegram), "trajet_id": tid, "uic_arr": u_arr, "uic_dep": u_dep,
             "gare_depart": gare_depart, "gare_arrivee": gare_arrivee, "heure_theo": h_theo, "kind": "checkin"}
    cand = {"numero": train2, "dep_rt": now_ts, "arr_plan": res["arr_reelle_ts"], "arr_rt": res["arr_reelle_ts"]}
    e2 = etat2 or etat_depuis_archives(date_j, train2)
    if e2:
        _pd, rt_d, _ = passage(e2, S.index_arret(e2, u_dep) >= 0 and u_dep or e2["arrets"][0]["uic"], "dep")
        pa, _ra, _ = passage(e2, u_arr, "arr")
        cand.update({"dep_rt": rt_d or now_ts, "arr_plan": pa or res["arr_reelle_ts"]})
    u_rupt = bazo_geo.uic_pour_gare(gare_rupture, "grand_est") or u_dep
    base_etat1 = e1 if e1 else {"numero": train1, "date": date_j, "arrets": []}
    cand["arr_rt"] = res["arr_reelle_ts"]
    rep = enregistrer_report(conn, cible, base_etat1, u_rupt, cand, now_ts, source="manuel")
    conn.execute("UPDATE reports_trajet SET statut='calcule', arr_prevue_initiale_ts=?, retard_cumule_min=? WHERE trajet_id=?",
                 (res["arr_prevue_ts"], res["retard_cumule_min"], tid))
    conn.execute("UPDATE trajets_valides SET retard_cumule_min=?, retard_arrivee_min=? WHERE id=?",
                 (res["retard_cumule_min"], res["retard_cumule_min"], tid))
    enregistrer_evenement(conn, date_j, train1, "retard_cumule", gare_arrivee, res["retard_cumule_min"],
                          f"{train1} -> {train2} (reconstitué)", "manuel")
    conn.commit()
    conn.close()
    return {**res, "trajet_id": tid, "rapport": rep["gare_rupture"]}
