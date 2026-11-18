#!/usr/bin/env python3
"""Tests du moteur temps réel / grève (incident Bettembourg 04/11/2026).

Exécution : `python tests_temps_reel.py` — toujours sur une COPIE de la base (/tmp/bazo_test_tr.db),
envois Telegram en simulation, aucun appel réseau (Navitia / CFL / GTFS-RT simulés).
"""
import os
import sqlite3
import sys

_COPIE = "/tmp/bazo_test_tr.db"
_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sillon_lorrain.db")
if os.path.exists(_COPIE):
    os.remove(_COPIE)
_src = sqlite3.connect(f"file:{_SOURCE}?mode=ro", uri=True)
_dst = sqlite3.connect(_COPIE)
_src.backup(_dst)
_src.close()
_dst.close()
os.environ["BAZO_DB_PATH"] = _COPIE
os.environ["BAZO_SIMULATION"] = "1"
os.environ["BAZO_API_REQUIRE_AUTH"] = "0"
os.environ["MOBILITEIT_API_KEY"] = "cle-de-test"

import importlib.util  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import bazo_alternatives as ALT  # noqa: E402
import bazo_geo  # noqa: E402
import bazo_greve as G  # noqa: E402
import bazo_notify as N  # noqa: E402
import bazo_sources_rt as S  # noqa: E402
import bazo_temps_reel as TR  # noqa: E402
import temps_reel_service as SVC  # noqa: E402
from bazo_config import db_connect  # noqa: E402

tz = ZoneInfo("Europe/Paris")
JOUR = "2026-11-04"
DEMAIN = "2026-11-05"
UID = "9000001"
U = {  # UIC
    "metz": "87192039", "hagondange": "87191114", "uckange": "87191130", "thionville": "87191007",
    "bettembourg": "82006030", "howald": "82002501", "luxembourg": "82001000",
}
ORDRE_ML = ["metz", "hagondange", "uckange", "thionville", "bettembourg", "howald", "luxembourg"]


def ts(date_j, hhmm):
    h, m = hhmm.split(":")
    return int(datetime.strptime(date_j, "%Y-%m-%d").replace(hour=int(h), minute=int(m), tzinfo=tz).timestamp())


def dt(date_j, hhmm):
    return datetime.fromtimestamp(ts(date_j, hhmm), tz)


def mk_etat(numero, horaires, retards=None, skipped=(), annule=False, date_j=JOUR, sources=("sncf_gtfs_rt",)):
    """horaires : {gare: 'HH:MM'} dans l'ordre ; retards : {gare: minutes} ; skipped : gares non desservies."""
    retards = retards or {}
    arrets = []
    for gare, hhmm in horaires.items():
        plan = ts(date_j, hhmm)
        r = retards.get(gare, 0)
        arrets.append({
            "uic": U[gare], "nom": gare.title(), "skipped": gare in skipped, "ret_arr": r, "ret_dep": r,
            "t_arr": plan + r * 60, "t_dep": plan + r * 60, "plan_arr": plan, "plan_dep": plan,
        })
    return {"numero": numero, "date": date_j, "sources": list(sources), "annule": annule, "ajoute": False,
            "arrets": arrets, "ts": 0}


H_88571 = {"metz": "08:33", "hagondange": "08:45", "uckange": "08:51", "thionville": "08:57",
           "bettembourg": "09:10", "howald": "09:18", "luxembourg": "09:24"}
H_88724 = {"metz": "09:03", "hagondange": "09:15", "uckange": "09:21", "thionville": "09:27",
           "bettembourg": "09:40", "howald": "09:48", "luxembourg": "10:03"}


def _preparer_base():
    conn = db_connect()
    TR.init_schema()
    G.init_schema()
    conn.execute("DELETE FROM inscriptions")
    conn.execute("DELETE FROM trajets_valides WHERE date IN (?, ?)", (JOUR, DEMAIN))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, gare_depart, heures, gare_retour, heures_retour, jours_actifs, alertes_actives) "
        "VALUES (?, 'Testeur', 'Metz', '08h-10h', 'Luxembourg', '16h-18h', '0,1,2,3,4', 1)", (UID,))
    conn.execute(
        "INSERT INTO trajets_valides (id_telegram, numero_train, date, heure_theo, statut_validation, gare_depart, gare_arrivee, "
        "destination, heure_arrivee_theo, origine, provenance) VALUES (?, '88571', ?, '08h33', 'en_attente', 'Metz', 'Luxembourg', "
        "'Luxembourg', '09h24', 'checkin', 'officielle')", (UID, JOUR))
    for t in ("etats_trains", "evenements_trains", "alertes_temps_reel", "suivi_immobilisation", "reports_trajet",
              "trains_habituels", "greve_plan", "greve_meta", "greve_import"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    N.MESSAGES_SIMULES.clear()


def _ligne_trajet():
    conn = db_connect()
    row = conn.execute(
        "SELECT statut_validation, train_report, retard_cumule_min, retard_arrivee_min, heure_reelle_arrivee, motif_validation, gare_rupture "
        "FROM trajets_valides WHERE id_telegram=? AND date=? AND numero_train='88571'", (UID, JOUR)).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------- géographie
def test_geo_identifiants_corriges():
    assert bazo_geo.uic_pour_gare("Thionville") == "87191007"
    assert bazo_geo.uic_pour_gare("Bettembourg") == "82006030"
    assert bazo_geo.uic_pour_gare("Howald") == "82002501"
    assert bazo_geo.uic_pour_gare("Sarrebourg") == "87215012"
    assert bazo_geo.uic_pour_gare("Forbach") == "87193003"
    assert bazo_geo.uic_pour_gare("Metz") == "87192039"
    assert bazo_geo.valider_radar_region("grand_est") == []
    assert len(bazo_geo.trajets_radar_multi(["grand_est"])) == 20


# ---------------------------------------------------------------- sources
def test_gtfs_rt_parse_skipped_et_annule():
    from google.transit import gtfs_realtime_pb2 as pb
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = ts(JOUR, "08:58")
    e = feed.entity.add()
    e.id = "1"
    t = e.trip_update
    t.trip.trip_id = "OCESN88571F1187_F:TER:FR:Line::x"
    t.trip.start_date = "20261104"
    t.trip.schedule_relationship = 0
    for gare in ORDRE_ML:
        su = t.stop_time_update.add()
        su.stop_id = f"StopPoint:OCETrain TER-{U[gare]}"
        su.arrival.delay = 15 * 60
        su.arrival.time = ts(JOUR, H_88571[gare]) + 15 * 60
        su.schedule_relationship = 1 if gare in ("bettembourg", "howald", "luxembourg") else 0
    e2 = feed.entity.add()
    e2.id = "2"
    t2 = e2.trip_update
    t2.trip.trip_id = "OCESN88999F1187_F:TER:FR:Line::y"
    t2.trip.start_date = "20261104"
    t2.trip.schedule_relationship = 3
    su = t2.stop_time_update.add()
    su.stop_id = f"StopPoint:OCETrain TER-{U['metz']}"
    etats = S.lire_gtfs_rt(contenu=feed.SerializeToString())
    e1 = etats[(JOUR, "88571")]
    assert S.classifier(e1) == "partiel"
    assert S.terminus_effectif_uic(e1) == U["thionville"] and S.terminus_prevu_uic(e1) == U["luxembourg"]
    assert S.arrets_supprimes_fin(e1) == [U["bettembourg"], U["howald"], U["luxembourg"]]
    assert etats[(JOUR, "88999")]["annule"] and S.classifier(etats[(JOUR, "88999")]) == "supprime"


def test_cfl_board_parse_arrets_annules():
    donnees = {"Departure": [{
        "name": "TER88627", "date": JOUR, "JourneyStatus": "P",
        "Stops": {"Stop": [
            {"extId": "300031019", "name": "Luxembourg, Gare Centrale", "depTime": "20:39:00", "depDate": JOUR,
             "rtDepTime": "20:39:00", "rtDepDate": JOUR},
            {"extId": "300134002", "name": "Bettembourg, Gare", "arrTime": "20:50:00", "arrDate": JOUR,
             "rtArrTime": "21:05:00", "rtArrDate": JOUR, "depTime": "20:51:00", "depDate": JOUR,
             "rtDepTime": "21:06:00", "rtDepDate": JOUR},
            {"extId": "400000098", "name": "Thionville, Gare", "arrTime": "21:03:00", "arrDate": JOUR,
             "depTime": "21:04:00", "depDate": JOUR, "cancelled": True},
            {"extId": "400000071", "name": "Metz-Ville, Gare", "arrTime": "21:28:00", "arrDate": JOUR, "cancelled": True},
        ]}}, {"name": "RE 5135", "date": JOUR, "Stops": {"Stop": []}}]}
    etats = S.etats_depuis_board_cfl(donnees)
    assert list(etats) == [(JOUR, "88627")]
    e = etats[(JOUR, "88627")]
    assert S.classifier(e) == "partiel" and S.terminus_effectif_uic(e) == U["bettembourg"]
    assert S.retard_max(e) == 15 and S.numero_depuis_texte("RE 5135") is None


def test_fusion_une_source_suffit_pour_partiel():
    gtfs = mk_etat("88571", H_88571, {g: 15 for g in H_88571})
    cfl = mk_etat("88571", H_88571, {g: 15 for g in H_88571}, skipped=("bettembourg", "howald", "luxembourg"),
                  sources=("cfl_mobiliteit",))
    f = S.fusionner_etats([gtfs, cfl])
    assert S.classifier(f) == "partiel" and sorted(f["sources"]) == ["cfl_mobiliteit", "sncf_gtfs_rt"]
    assert S.terminus_effectif_uic(f) == U["thionville"]


def test_navitia_perturbation_arrets_supprimes():
    vj = {"vehicle_journeys": [{"stop_times": [
        {"stop_point": {"id": f"stop_point:SNCF:{U[g]}:Train", "stop_area": {"id": f"stop_area:SNCF:{U[g]}"}},
         "departure_time": H_88571[g].replace(":", "") + "00", "arrival_time": H_88571[g].replace(":", "") + "00"}
        for g in ORDRE_ML]},
    ], "disruptions": [{"status": "active", "severity": {"effect": "REDUCED_SERVICE"}, "impacted_objects": [{
        "impacted_stops": [{"stop_point": {"stop_area": {"id": f"stop_area:SNCF:{U[g]}"}}, "stop_time_effect": "deleted"}
                           for g in ("bettembourg", "howald", "luxembourg")]}]}]}
    e = S.etat_depuis_navitia_vj(vj, "88571", JOUR)
    assert S.classifier(e) == "partiel" and S.terminus_effectif_uic(e) == U["thionville"]


# ---------------------------------------------------------------- retard cumulé
def test_formule_retard_cumule():
    assert TR.calculer_retard_cumule(ts(JOUR, "09:24"), ts(JOUR, "10:54")) == 90
    assert TR.calculer_retard_cumule(ts(JOUR, "09:24"), ts(JOUR, "09:10")) == 0
    assert TR.calculer_retard_cumule(None, ts(JOUR, "10:54")) is None
    assert TR.fmt_duree(90) == "1h30" and TR.fmt_duree(45) == "45 min"


def _etats_incident(reel_88724=51):
    e1 = mk_etat("88571", H_88571, {"hagondange": 5, "uckange": 10, "thionville": 15},
                 skipped=("bettembourg", "howald", "luxembourg"))
    e2 = mk_etat("88724", H_88724, {"thionville": 30, "bettembourg": 45, "howald": 48, "luxembourg": reel_88724})
    return {(JOUR, "88571"): e1, (JOUR, "88724"): e2}


def test_scenario_bettembourg_bascule_et_cumul_90_min():
    _preparer_base()
    SVC.G.veille_greve = lambda *a, **k: {}          # pas d'appel Navitia dans ce test
    sources = {"gtfs": _etats_incident()}
    r1 = SVC.cycle(now=dt(JOUR, "08:58"), sources=sources)
    msgs = [m for m in N.MESSAGES_SIMULES if m["chat_id"] == UID]
    assert r1["impacts"] == 1 and len(msgs) == 1, (r1, msgs)
    assert "limité à Thionville" in msgs[0]["texte"] and "88724" in msgs[0]["texte"]
    assert "1h30" in msgs[0]["texte"]
    st, rep, cumul, retard, h_reelle, motif, rupt = _ligne_trajet()
    assert st == "valide_report" and rep == "88724" and cumul == 90 and retard == 90 and h_reelle == "10h54", _ligne_trajet()
    assert rupt == "Thionville" and "changement de train forcé" in motif
    # 2e cycle : aucun doublon de message
    SVC.cycle(now=dt(JOUR, "09:00"), sources=sources)
    assert len([m for m in N.MESSAGES_SIMULES if m["chat_id"] == UID]) == 1
    # le radar (suppression / retard) ne doit jamais écraser le retard cumulé
    import scanner_ligne as sl
    sl.synchroniser_checkin_retard(JOUR, "88571", supprime=True)
    assert _ligne_trajet()[0] == "valide_report" and _ligne_trajet()[2] == 90
    # arrivée du 2e train + 2 min : finalisation
    SVC.cycle(now=dt(JOUR, "10:57"), sources=sources)
    conn = db_connect()
    statut, cumul_db = conn.execute("SELECT statut, retard_cumule_min FROM reports_trajet WHERE id_telegram=?", (UID,)).fetchone()
    evt = conn.execute("SELECT retard_min FROM evenements_trains WHERE type='retard_cumule'").fetchone()
    conn.close()
    assert statut == "calcule" and cumul_db == 90 and evt[0] == 90
    final = [m for m in N.MESSAGES_SIMULES if "Retard réel cumulé" in m["texte"]]
    assert len(final) == 1 and "1h30" in final[0]["texte"]


def test_pdf_retard_cumule_et_motif():
    bot = _charger_bot()
    from bazo_metier import charger_utilisateur
    incidents, rows = bot.assembler_incidents_pdf(UID, "2026-11", charger_utilisateur(UID) or {})
    cle = [k for k in incidents if "88571" in k]
    assert cle, incidents
    inc = incidents[cle[0]]
    assert inc["retard"] == 90 and inc["type"] == "retard" and inc["retard_txt"] == "+90 min", inc
    assert inc["trajet"] == "Metz -> Luxembourg" and "changement de train forcé" in inc["motif"], inc
    from bazo_metier import bilan_mois_trajets_valides_db
    assert bilan_mois_trajets_valides_db(UID, "2026-11")[0] >= 90


def _charger_bot():
    spec = importlib.util.spec_from_file_location("bot_interactif", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_interactif.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_suppression_totale_bascule_depuis_depart():
    _preparer_base()
    e1 = mk_etat("88571", H_88571, annule=True)
    e2 = mk_etat("88724", H_88724, {"luxembourg": 5, "thionville": 5, "bettembourg": 5, "howald": 5})
    SVC.cycle(now=dt(JOUR, "08:20"), sources={"gtfs": {(JOUR, "88571"): e1, (JOUR, "88724"): e2}})
    st, rep, cumul, *_ = _ligne_trajet()
    assert st == "valide_report" and rep == "88724" and cumul == 44      # 10:08 - 09:24
    assert "supprimé" in N.MESSAGES_SIMULES[0]["texte"]


def test_re_bascule_si_2e_train_aussi_supprime():
    _preparer_base()
    e1 = mk_etat("88571", H_88571, {"thionville": 15}, skipped=("bettembourg", "howald", "luxembourg"))
    e2 = mk_etat("88724", H_88724, annule=True)
    H_3 = {"metz": "09:33", "hagondange": "09:45", "uckange": "09:51", "thionville": "09:57", "bettembourg": "10:10",
           "howald": "10:18", "luxembourg": "10:24"}
    e3 = mk_etat("88726", H_3, {"thionville": 20, "luxembourg": 25})
    SVC.cycle(now=dt(JOUR, "08:58"), sources={"gtfs": {(JOUR, "88571"): e1, (JOUR, "88724"): e2, (JOUR, "88726"): e3}})
    st, rep, cumul, *_ = _ligne_trajet()
    assert rep == "88726" and cumul == 85, (rep, cumul)        # 10:49 - 09:24


# ---------------------------------------------------------------- faux maintenu
def test_faux_maintenu_alerte_preventive():
    _preparer_base()
    conn = db_connect()
    e_a0 = mk_etat("88571", H_88571, {g: 25 for g in H_88571})
    e_b = mk_etat("88724", H_88724, {g: 3 for g in H_88724})
    N.MESSAGES_SIMULES.clear()
    TR.detecter_faux_maintenus(conn, {(JOUR, "88571"): e_a0, (JOUR, "88724"): e_b}, dt(JOUR, "09:00"))
    assert not N.MESSAGES_SIMULES                              # 1er passage : seulement l'enregistrement
    # 17 min plus tard : le train est toujours « avant Hagondange » (prévision 09h10 dépassée de >5 min)
    e_a1 = mk_etat("88571", H_88571, {g: 40 for g in H_88571})
    nb = TR.detecter_faux_maintenus(conn, {(JOUR, "88571"): e_a1, (JOUR, "88724"): e_b}, dt(JOUR, "09:17"))
    msgs = [m for m in N.MESSAGES_SIMULES if m["chat_id"] == UID]
    conn.close()
    assert nb == 1 and len(msgs) == 1, (nb, N.MESSAGES_SIMULES)
    assert "faux maintenu" in msgs[0]["texte"] and "88724" in msgs[0]["texte"] and "+40" in msgs[0]["texte"]


def test_faux_maintenu_pas_d_alerte_sans_suivant():
    _preparer_base()
    conn = db_connect()
    e_a0 = mk_etat("88571", H_88571, {g: 25 for g in H_88571})
    TR.detecter_faux_maintenus(conn, {(JOUR, "88571"): e_a0}, dt(JOUR, "09:00"))
    e_a1 = mk_etat("88571", H_88571, {g: 40 for g in H_88571})
    nb = TR.detecter_faux_maintenus(conn, {(JOUR, "88571"): e_a1}, dt(JOUR, "09:17"))
    conn.close()
    assert nb == 0


# ---------------------------------------------------------------- reconstitution manuelle
def test_report_manuel_reconstitution_incident():
    _preparer_base()
    res = TR.enregistrer_report_manuel(
        UID, JOUR, "88571", "88724", "Metz", "Luxembourg", "Thionville",
        arrivee_reelle_hhmm="10h54",
        etat1=mk_etat("88571", H_88571, {"thionville": 15}), etat2=mk_etat("88724", H_88724, {"luxembourg": 51}),
    )
    assert res["retard_cumule_min"] == 90 and res["arrivee_prevue_train1"] == "09h24"
    st, rep, cumul, *_ = _ligne_trajet()
    assert st == "valide_report" and rep == "88724" and cumul == 90


# ---------------------------------------------------------------- alternatives
def _cand(num, dep, arr, ret_dep=0, ret_arr=0, date_j=DEMAIN):
    return {"numero": num, "dep_plan": ts(date_j, dep), "dep_rt": ts(date_j, dep) + ret_dep * 60,
            "arr_plan": ts(date_j, arr), "arr_rt": ts(date_j, arr) + ret_arr * 60, "ret_dep": ret_dep, "ret_arr": ret_arr,
            "annule": False}


def test_alternatives_precedent_et_suivant():
    cands = [_cand("88567", "07:33", "08:24"), _cand("88569", "08:03", "08:54"), _cand("88724", "09:03", "10:03", 0, 10)]
    prec, suiv = ALT.choisir_alternatives(cands, ts(DEMAIN, "08:33"), ts(DEMAIN, "06:00") - 86400)
    assert prec["numero"] == "88569" and suiv["numero"] == "88724"
    # un train précédent déjà parti n'est plus proposé
    prec2, _s = ALT.choisir_alternatives(cands, ts(DEMAIN, "08:33"), ts(DEMAIN, "08:10"))
    assert prec2 is None
    txt = ALT.formater_alternatives(prec, suiv, "Luxembourg")
    assert "08h03" in txt and "08h54" in txt and "09h03" in txt and "10h13" in txt and "(+10 min)" in txt


def test_alternatives_filtre_trains_supprimes():
    cands = [_cand("88569", "08:03", "08:54"), _cand("88724", "09:03", "10:03")]
    ok = ALT.filtrer_candidats(cands, None, DEMAIN, statuts_plan={"88569": "supprime"})
    assert [c["numero"] for c in ok] == ["88724"]


# ---------------------------------------------------------------- grève
def test_comparer_base_temps_reel():
    base = {"88567": _cand("88567", "07:33", "08:24"), "88569": _cand("88569", "08:03", "08:54"),
            "88571": _cand("88571", "08:33", "09:24"), "88726": _cand("88726", "09:33", "10:24")}
    rt = {"88567": _cand("88567", "07:33", "08:24"), "88726": _cand("88726", "09:43", "10:34", 10, 10),
          "88999": _cand("88999", "11:00", "11:50")}
    res = {r["numero"]: r["statut"] for r in G.comparer_base_temps_reel(base, rt)}
    assert res == {"88567": "maintenu", "88569": "supprime", "88571": "supprime", "88726": "modifie", "88999": "ajoute"}


def test_parser_grille_officielle():
    assert [t["numero"] for t in G.parser_liste_trains("88567, 88724;88726\n88726 88740")] == ["88567", "88724", "88726", "88740"]
    csv_txt = "numero,statut,heure\n88567,maintenu,07h33\n88571,supprimé,08h33\nTER 88726,modifié,09h43\n"
    t = G.parser_liste_trains(csv_txt)
    assert [(x["numero"], x["statut"]) for x in t] == [("88567", "maintenu"), ("88571", "supprime"), ("88726", "modifie")]
    imports = {"88567": {"statut": "maintenu"}}
    assert G.appliquer_import("maintenu", "88724", imports, True, True) == ("supprime", 1)
    assert G.appliquer_import("supprime", "88567", imports, True, False) == ("maintenu", 1)
    assert G.appliquer_import("maintenu", "88724", imports, False, True) == ("maintenu", 0)


def _plan_demain():
    base = {"88567": _cand("88567", "07:33", "08:24"), "88569": _cand("88569", "08:03", "08:54"),
            "88571": _cand("88571", "08:33", "09:24"), "88724": _cand("88724", "09:03", "10:03"),
            "88726": _cand("88726", "09:33", "10:24")}
    rt = {"88567": base["88567"], "88724": base["88724"], "88726": _cand("88726", "09:43", "10:34", 10, 10)}
    return base, rt


def _mock_plan(monkey_base_rt):
    base, rt = monkey_base_rt

    def lister(date_j, uic_dep, uic_arr, fraicheur, pages_max=4):
        if (uic_dep, uic_arr) != (U["metz"], U["luxembourg"]):
            return {}
        return dict(base if fraicheur == "base_schedule" else rt)
    G.lister_trains_segment = lister


def test_plan_greve_et_alerte_ciblee_train_habituel():
    _preparer_base()
    _mock_plan(_plan_demain())
    ALT.journeys_directs = lambda uf, ut, depuis, count=12, fraicheur="realtime": [
        _cand("88567", "07:33", "08:24"), _cand("88724", "09:03", "10:03"), _cand("88726", "09:43", "10:34", 10, 10)]
    G.definir_train_habituel(UID, "Metz", "Luxembourg", "08h33", None, "")
    conn = db_connect()
    stats = G.rafraichir_plan(conn, DEMAIN, segments=[(U["metz"], U["luxembourg"])])
    assert stats["supprimes"] == 2 and stats["modifies"] == 1 and stats["mode"] == "veille"      # 2/5 mais < 4 supprimés
    assert G.notifier_greve(conn, DEMAIN, dt(JOUR, "17:00")) == 0                                  # pas d'alerte sans grève
    G.forcer_mode(DEMAIN, "force")
    nb = G.notifier_greve(conn, DEMAIN, dt(JOUR, "17:00"))
    conn.close()
    msgs = [m for m in N.MESSAGES_SIMULES if m["chat_id"] == UID]
    assert nb == 1 and len(msgs) == 1
    t = msgs[0]["texte"]
    assert "SUPPRIMÉ" in t and "08h33" in t and "88571" in t
    assert "Train précédent" in t and "88567" in t and "Train suivant" in t and "88724" in t
    assert "08h24" in t and "10h03" in t                                  # arrivées estimées
    assert "08h03 ❌" in t and "09h33 ⚠️" in t                              # résumé du créneau
    # même plan -> pas de doublon
    conn = db_connect()
    assert G.notifier_greve(conn, DEMAIN, dt(JOUR, "17:30")) == 0
    conn.close()


def test_import_grille_officielle_prime_et_detecte_greve():
    _preparer_base()
    _mock_plan(_plan_demain())
    ALT.journeys_directs = lambda *a, **k: []
    G.rafraichir_plan = G.rafraichir_plan
    conn = db_connect()
    G.rafraichir_plan(conn, DEMAIN, segments=[(U["metz"], U["luxembourg"])])
    conn.close()
    # grille officielle exhaustive : seuls 88567 et 88726 maintenus (88724 pourtant présent dans Navitia)
    orig = G.segments_a_surveiller
    G.segments_a_surveiller = lambda conn, d=None: [(U["metz"], U["luxembourg"])]
    res = G.importer_grille_officielle(DEMAIN, G.parser_liste_trains("88567 88726"), exhaustif=True)
    G.segments_a_surveiller = orig
    assert res["mode"] == "actif" and res["supprimes"] == 3
    conn = db_connect()
    rows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT numero_train, statut, source, divergence FROM greve_plan WHERE date=?", (DEMAIN,))}
    conn.close()
    assert rows["88724"] == ("supprime", "import_sncf", 1)       # divergence Navitia / grille officielle tracée
    assert rows["88567"][0] == "maintenu" and rows["88571"][0] == "supprime"


def test_detection_auto_greve_et_veille_horaires():
    stats = {"total": 20, "supprimes": 6}
    conn = db_connect()
    assert G.detecter_mode(conn, "2030-01-01", stats) == "auto"
    assert G.detecter_mode(conn, "2030-01-01", {"total": 20, "supprimes": 2}) == "veille"
    conn.close()
    assert G.intervalle_veille_s(dt(JOUR, "17:00"), False, True) == 1800
    assert G.intervalle_veille_s(dt(JOUR, "06:00"), True, True) == 900
    assert G.intervalle_veille_s(dt(JOUR, "03:00"), True, True) is None
    assert G.intervalle_veille_s(dt(JOUR, "13:00"), False, False) == 5400


def test_trains_habituels_deduits_des_checkin():
    _preparer_base()
    conn = db_connect()
    for d in ("2026-10-26", "2026-11-02"):
        conn.execute(
            "INSERT OR IGNORE INTO trajets_valides (id_telegram, numero_train, date, heure_theo, statut_validation, gare_depart, "
            "gare_arrivee, origine) VALUES (?, '88571', ?, '08h33', 'valide_normal', 'Metz', 'Luxembourg', 'checkin')", (UID, d))
    conn.commit()
    assert G.deduire_trains_habituels(conn) >= 1
    row = conn.execute("SELECT numero_train, jours FROM trains_habituels WHERE id_telegram=? AND heure_theo='08h33'", (UID,)).fetchone()
    conn.close()
    assert row and row[0] == "88571" and row[1] == "0,2"       # lundi + mercredi


# ---------------------------------------------------------------- notifications
def test_live_snapshot_couleurs_et_prochain_train():
    """Mini-map : tronçons émeraude / ambre / rouge ; tiroir : prochain train, retard exact, alternatives."""
    import bazo_live as L
    now = ts(JOUR, "08:58")
    e1 = mk_etat("88571", H_88571, {"hagondange": 5, "uckange": 10, "thionville": 15},
                 skipped=("bettembourg", "howald", "luxembourg"))
    e2 = mk_etat("88724", H_88724, {"thionville": 30, "bettembourg": 45, "howald": 48, "luxembourg": 51})
    e3 = mk_etat("88726", {"metz": "09:33", "thionville": "09:57", "luxembourg": "10:24"})
    snap = L.construire_snapshot({(JOUR, "88571"): e1, (JOUR, "88724"): e2, (JOUR, "88726"): e3}, now)
    noms = [s["nom"] for s in snap["stations"]]
    seg = {(noms[s["a"]], noms[s["b"]]): s for s in snap["segments"]}
    assert seg[("Metz", "Hagondange")]["statut"] == "retard", seg[("Metz", "Hagondange")]
    assert seg[("Thionville", "Bettembourg")]["statut"] == "supprime"       # terminus anticipé à Thionville
    assert seg[("Bettembourg", "Luxembourg")]["statut"] == "supprime"
    assert seg[("Nancy", "Frouard")]["statut"] == "fluide"                    # aucun train concerné
    assert snap["resume"]["statut"] == "supprime" and snap["resume"]["nb_supprimes"] == 1
    # prochain train de l'usager (train habituel 08h33) : coupé à Thionville -> supprimé pour Metz → Luxembourg
    pt = L.prochain_train(snap, U["metz"], U["luxembourg"], now, habituels=["08h33"])
    assert pt["numero"] == "88571" and pt["statut"] == "supprime" and pt["habituel"] is True
    assert pt["en_cours"] is True                       # parti à 08h33, immobilisé : reste affiché tant qu'il n'est pas arrivé
    assert pt["alternatives"][0]["numero"] == "88724"
    alt = pt["alternatives"][0]
    assert alt["retard_arrivee"] == 51 and alt["arrivee_reelle"] == "10h54" and alt["etiquette"] == "+51 min"
    # Metz → Thionville reste desservi par 88571 (retard de 15 min exact)
    pt2 = L.prochain_train(snap, U["metz"], U["thionville"], now, habituels=["08h33"])
    assert pt2["numero"] == "88571" and pt2["statut"] == "retard" and pt2["retard_arrivee"] == 15, pt2
    # persistance
    L.sauver_snapshot(db_connect(), snap)
    assert L.lire_snapshot()["generated_ts"] == now
    # profil usager hors du Sillon -> pas de prochain train, raison explicite
    vue = L.vue_usager(snap, {"gare_depart": "Forbach", "gare_retour": "Metz"}, {}, now)
    assert vue["prochain_train"] is None and vue["raison"] == "trajet_hors_sillon"
    vue = L.vue_usager(snap, {"gare_depart": "Metz", "gare_retour": "Luxembourg"}, {(U["metz"], U["luxembourg"]): ["08h33"]}, now)
    assert vue["prochain_train"]["numero"] == "88571"


def test_live_stations_et_ordre_axe():
    import bazo_live as L
    st = L.stations_affichees()
    assert len(st) == 9 and len({s["uic"] for s in st}) == 9
    ordre = L.ordre_axe()
    rangs = [ordre[s["uic"]] for s in st]
    assert rangs == sorted(rangs), "stations affichées dans l'ordre Nancy → Luxembourg"


def test_hub_entraide_quorum_offre_reservation():
    """Hub : quorum « je suis bloqué » + corroboration → actif ; covoiturage si attente ≥ 2 h ; plafond de frais ;
    une offre par conducteur ; deux réservations par passager ; contact visible seulement conducteur/passagers."""
    import bazo_hub as H
    import bazo_live as L
    import bazo_metier as M
    from bazo_metier import creer_inscription_si_absent
    H.init_schema(); TR.init_schema()
    conn = db_connect()
    ids = ["9000101", "9000102", "9000103", "9000104"]
    for i in ids:
        conn.execute("DELETE FROM inscriptions WHERE id_telegram=?", (i,))
        conn.execute("DELETE FROM trajets_valides WHERE id_telegram=?", (i,))
        conn.execute("DELETE FROM signalements WHERE id_auteur=?", (i,))
    conn.execute("DELETE FROM hubs WHERE date=?", (JOUR,))
    conn.execute("DELETE FROM hub_offres"); conn.execute("DELETE FROM hub_reservations"); conn.execute("DELETE FROM hub_participants")
    conn.commit()
    import time as _t
    h_now = _t.strftime("%Hh%M")   # la fenêtre de signalement (3 h après le train) doit être ouverte quel que soit l'heure du test
    for i in ids:
        creer_inscription_si_absent(i, prenom=f"U{i[-1]}")
        conn.execute("UPDATE inscriptions SET gare_depart='Metz', gare_retour='Luxembourg', heures='08h-10h', heures_retour='16h-18h', age='Non précisé' WHERE id_telegram=?", (i,))
        conn.execute("""INSERT OR IGNORE INTO trajets_valides (id_telegram, numero_train, date, heure_theo, statut_validation, gare_depart, gare_arrivee, destination, origine)
                        VALUES (?, '88571', ?, ?, 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg', 'checkin')""", (i, JOUR, h_now))
    # état temps réel : train à +20 min (corroboration), prochain train utile dans 3 h (snapshot)
    now = ts(JOUR, "08:50")
    e1 = mk_etat("88571", H_88571, {"metz": 20, "hagondange": 20, "uckange": 20, "thionville": 20, "bettembourg": 20, "howald": 20, "luxembourg": 20})
    e2 = mk_etat("88726", {"metz": "11:55", "thionville": "12:20", "luxembourg": "12:50"})
    TR.mettre_a_jour_etats(conn, {(JOUR, "88571"): e1, (JOUR, "88726"): e2}, dt(JOUR, "08:50"))
    L.sauver_snapshot(conn, L.construire_snapshot({(JOUR, "88571"): e1, (JOUR, "88726"): e2}, now))
    conn.commit()
    r = H.declarer_bloque(ids[0], JOUR, "88571", h_now, "Metz", now)
    assert r["ok"] and r["hub"]["statut"] == "candidat" and r["hub"]["nb_bloques"] == 1, r
    H.declarer_bloque(ids[1], JOUR, "88571", h_now, "Metz", now)
    r = H.declarer_bloque(ids[2], JOUR, "88571", h_now, "Metz", now)
    assert r["hub"]["statut"] == "actif" and r["hub"]["motif"].startswith("quorum_3"), r["hub"]
    assert r["hub"]["covoiturage_ouvert"] is True and r["hub"]["attente_min"] >= 120, r["hub"]
    hub_id = r["hub"]["id"]
    sans_checkin = H.declarer_bloque("9000199", JOUR, "88571", h_now, "Metz", now)
    assert not sans_checkin["ok"] and sans_checkin["erreur"] == "checkin_requis"
    # plafond : Metz → Luxembourg ≈ 71 km route × 0,25 € / (3 places + 1)
    plafond = H.plafond_participation(U["metz"], U["luxembourg"], 3)
    assert 3 <= plafond <= 8, plafond
    trop = H.publier_offre(ids[0], hub_id, "Luxembourg", 3, "parvis", "09h30", plafond + 5, "06 00 00 00 00", True)
    assert not trop["ok"] and trop["erreur"] == "participation_trop_elevee"
    h_dep = _t.strftime("%Hh%M", _t.localtime(_t.time() + 3600))
    ok = H.publier_offre(ids[0], hub_id, "Luxembourg", 3, "parvis", h_dep, plafond, "06 00 00 00 00", True)
    assert ok["ok"], ok
    offre_id = ok["offre"]["id"]
    assert ok["offre"]["contact"] == "06 00 00 00 00" and ok["offre"]["est_conducteur"]
    assert not H.publier_offre(ids[0], hub_id, "Luxembourg", 2, "parvis", h_dep, 1, None, True)["ok"]   # une offre active
    assert not H.publier_offre(ids[1], hub_id, "Luxembourg", 2, "chez_moi", h_dep, 1, None, True)["ok"]  # point RDV fermé
    assert H.reserver(ids[0], offre_id)["erreur"] == "conducteur"
    r1 = H.reserver(ids[1], offre_id); assert r1["ok"] and r1["offre"]["places_restantes"] == 2 and r1["offre"]["contact"] == "06 00 00 00 00"
    assert H.reserver(ids[1], offre_id)["erreur"] == "deja_reserve"
    vue_tiers = H.charger_hub(conn, hub_id, ids[3])
    assert "contact" not in vue_tiers["offres"][0] and vue_tiers["offres"][0]["conducteur"] == "U1"
    H.reserver(ids[2], offre_id); H.reserver(ids[3], offre_id)
    assert H.charger_offre(conn, offre_id)["statut"] == "complete"
    assert H.annuler_reservation(ids[3], offre_id)["offre"]["statut"] == "ouverte"
    assert H.changer_statut_offre(ids[1], offre_id, "annulee")["erreur"] == "offre_introuvable"      # pas le conducteur
    assert H.changer_statut_offre(ids[0], offre_id, "partie")["ok"]
    vue = H.hubs_pour_usager(ids[3], JOUR)
    assert len(vue["hubs"]) == 1 and vue["hubs"][0]["id"] == hub_id and vue["mes_checkins"][0]["numero_train"] == "88571"
    # notification transactionnelle au conducteur (simulation)
    assert any("réservé une place" in m["texte"] and m["chat_id"] == ids[0] for m in N.MESSAGES_SIMULES)
    conn.close()


def test_hub_ouvert_automatiquement_sur_suppression():
    import bazo_hub as H
    H.init_schema()
    conn = db_connect()
    conn.execute("DELETE FROM hubs WHERE date=? AND numero_train='88724'", (JOUR,))
    conn.execute("INSERT OR IGNORE INTO trajets_valides (id_telegram, numero_train, date, heure_theo, statut_validation, gare_depart, gare_arrivee, destination, origine) "
                 "VALUES ('9000101', '88724', ?, '09h03', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg', 'checkin')", (JOUR,))
    e = mk_etat("88724", H_88724, annule=True)
    TR.mettre_a_jour_etats(conn, {(JOUR, "88724"): e}, dt(JOUR, "08:40"))
    conn.commit()
    n = H.ouvrir_hubs_depuis_etats(conn, [{"type": "suppression_totale", "date": JOUR, "numero": "88724", "etat": e}], ts(JOUR, "08:40"))
    hub = conn.execute("SELECT statut, motif_activation FROM hubs WHERE date=? AND numero_train='88724'", (JOUR,)).fetchone()
    conn.close()
    assert n == 1 and hub[0] == "actif" and hub[1] == "train_supprime", (n, hub)


def test_notify_simulation_et_ids_non_telegram():
    N.MESSAGES_SIMULES.clear()
    assert N.envoyer("app_be114060f2", "x") is False and N.envoyer("usr_9d7e1ed717a2", "x") is False
    assert N.envoyer("123456", "x") is True and len(N.MESSAGES_SIMULES) == 1


TESTS = [
    ("geo_identifiants_corriges", test_geo_identifiants_corriges),
    ("gtfs_rt_parse", test_gtfs_rt_parse_skipped_et_annule),
    ("cfl_board_parse", test_cfl_board_parse_arrets_annules),
    ("fusion_sources", test_fusion_une_source_suffit_pour_partiel),
    ("navitia_perturbations", test_navitia_perturbation_arrets_supprimes),
    ("formule_cumul", test_formule_retard_cumule),
    ("scenario_bettembourg", test_scenario_bettembourg_bascule_et_cumul_90_min),
    ("pdf_retard_cumule", test_pdf_retard_cumule_et_motif),
    ("suppression_totale_bascule", test_suppression_totale_bascule_depuis_depart),
    ("re_bascule", test_re_bascule_si_2e_train_aussi_supprime),
    ("faux_maintenu", test_faux_maintenu_alerte_preventive),
    ("faux_maintenu_sans_suivant", test_faux_maintenu_pas_d_alerte_sans_suivant),
    ("report_manuel", test_report_manuel_reconstitution_incident),
    ("alternatives", test_alternatives_precedent_et_suivant),
    ("alternatives_filtre", test_alternatives_filtre_trains_supprimes),
    ("grève_comparaison", test_comparer_base_temps_reel),
    ("grève_parser", test_parser_grille_officielle),
    ("grève_alerte_ciblée", test_plan_greve_et_alerte_ciblee_train_habituel),
    ("grève_import_officiel", test_import_grille_officielle_prime_et_detecte_greve),
    ("grève_détection_horaires", test_detection_auto_greve_et_veille_horaires),
    ("habituels_déduits", test_trains_habituels_deduits_des_checkin),
    ("live_snapshot", test_live_snapshot_couleurs_et_prochain_train),
    ("live_stations", test_live_stations_et_ordre_axe),
    ("hub_entraide", test_hub_entraide_quorum_offre_reservation),
    ("hub_auto_suppression", test_hub_ouvert_automatiquement_sur_suppression),
    ("notify_simulation", test_notify_simulation_et_ids_non_telegram),
]


def main():
    echecs = []
    for nom, fn in TESTS:
        try:
            fn()
            print(f"[OK] {nom}")
        except Exception as e:
            import traceback
            print(f"[FAIL] {nom}: {e!r}")
            traceback.print_exc(limit=4)
            echecs.append(nom)
    if echecs:
        print(f"\n{len(echecs)} test(s) en échec : {echecs}")
        return 1
    print(f"\n{len(TESTS)} tests OK — temps réel / grève.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
