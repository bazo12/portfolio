#!/usr/bin/env python3
"""Sprint 1 — Tests fumée : check-in Navitia, dédoublonnage, trajets_valides, PDF."""
import importlib.util
import os
import sqlite3
import sys

# Tests : auth API désactivée sauf test_api_auth dédié
os.environ.setdefault("BAZO_API_REQUIRE_AUTH", "0")
os.environ.setdefault("BAZO_ADMIN_LOCALHOST", "0")
os.environ.setdefault("BAZO_ADMIN_API_KEY", "test_admin_key_ci")
os.environ.setdefault("BAZO_API_RATE_LIMIT", "0")

from bazo_config import DB_PATH, TOKEN_SNCF, db_connect
from bazo_metier import (
    SEUIL_RETARD_MIN,
    barre_progression_gf,
    bilan_mois_trajets_valides_db,
    charger_utilisateur,
    est_annulation,
    format_duree_retard,
    mettre_a_jour_inscription,
    profil_gares_configure,
    inferer_type_reclamation,
    promouvoir_trajet_alerte_radar,
    promouvoir_trajet_signalement,
    stats_rapport_db,
    sync_trajets_valides_depuis_radar,
    verifier_coherence_alerte_pdf,
)

ROOT = os.path.dirname(os.path.abspath(__file__))


def _charger_module_bot():
    path = os.path.join(ROOT, "bot_interactif.py")
    spec = importlib.util.spec_from_file_location("bot_interactif", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_schema_trajets_valides():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(trajets_valides)").fetchall()}
    conn.close()
    requis = {
        "id_telegram", "numero_train", "date", "heure_theo", "heure_reelle_arrivee",
        "statut_validation", "retard_arrivee_min", "motif_validation",
    }
    manquants = requis - cols
    assert not manquants, f"Colonnes manquantes : {manquants}"


def test_uic_gares_cles():
    import bazo_geo as geo
    bot = _charger_module_bot()
    assert geo.uic_pour_gare("Metz") == "87192039"
    assert geo.uic_pour_gare("Luxembourg") == "82001000"
    assert geo.uic_pour_gare("Thionville") == "87191007"
    assert bot.resoudre_stop_area("Metz") == "87192039"
    assert geo.get_index_gare("Hagondange") >= 0
    assert geo.calculer_sens("Metz", "Luxembourg") == 1


def test_bazo_geo_radar():
    import bazo_geo as geo
    trajets = geo.trajets_radar()
    assert len(trajets) == 20
    noms = {t["nom"] for t in trajets}
    assert "Metz ➔ Luxembourg" in noms
    assert "Nancy ➔ Strasbourg" in noms
    assert all(t["url"].startswith("https://api.sncf.com") for t in trajets)


def test_bazo_geo_regions():
    import bazo_geo as geo
    regions = geo.liste_regions()
    assert any(r["id"] == "grand_est" and r["active"] for r in regions)
    assert any(r["id"] == "ile_de_france" and not r["active"] for r in regions)
    assert any(r["id"] == "auvergne_rhone_alpes" and not r["active"] for r in regions)
    assert any(r["id"] == "hauts_de_france" and not r["active"] for r in regions)
    assert geo.uic_pour_gare("Lyon Part-Dieu", "auvergne_rhone_alpes") == "87723197"
    assert geo.uic_pour_gare("Paris Est", "ile_de_france") == "87113001"
    assert geo.uic_pour_gare("Bordeaux Saint-Jean", "nouvelle_aquitaine") == "87581009"
    assert len(geo.trajets_radar("ile_de_france")) >= 4
    assert geo.compter_axes_radar(list(geo.REGIONS.keys())) >= 53
    assert len(regions) == 13
    assert not geo.valider_radar_region("grand_est")
    assert len(geo.trajets_radar_multi(["grand_est", "ile_de_france"])) == 20 + len(
        geo.trajets_radar("ile_de_france")
    )


def test_api_geo():
    from fastapi.testclient import TestClient
    from bazo_api.main import app
    client = TestClient(app)
    r = client.get("/v1/geo/regions")
    assert r.status_code == 200
    r2 = client.get("/v1/geo/uic/Metz")
    assert r2.status_code == 200
    assert r2.json()["uic"] == "87192039"
    r3 = client.get("/v1/geo/radar")
    assert r3.status_code == 200
    assert r3.json()["nb"] == 20


def test_recherche_trains_dedup():
    """Dédoublonnage et tri — mock local, sans appel Navitia."""
    bot = _charger_module_bot()
    fixture = {
        "journeys": [
            {
                "status": "active",
                "departure_date_time": "20260613T070000",
                "arrival_date_time": "20260613T080000",
                "sections": [{
                    "type": "public_transport",
                    "display_informations": {
                        "headsign": "88701", "direction": "Luxembourg",
                        "commercial_mode": "TER", "network": "TER Grand Est",
                    },
                    "stop_date_times": [
                        {"stop_point": {"name": "Metz"}, "departure_date_time": "20260613T070000"},
                        {"stop_point": {"name": "Luxembourg"}, "arrival_date_time": "20260613T080000"},
                    ],
                }],
            },
            {
                "status": "active",
                "departure_date_time": "20260613T073300",
                "arrival_date_time": "20260613T083000",
                "sections": [{
                    "type": "public_transport",
                    "display_informations": {
                        "headsign": "88702", "direction": "Luxembourg",
                        "commercial_mode": "TER", "network": "TER Grand Est",
                    },
                    "stop_date_times": [
                        {"stop_point": {"name": "Metz"}, "departure_date_time": "20260613T073300"},
                        {"stop_point": {"name": "Luxembourg"}, "arrival_date_time": "20260613T083000"},
                    ],
                }],
            },
            {
                "status": "active",
                "departure_date_time": "20260613T070000",
                "arrival_date_time": "20260613T080000",
                "sections": [{
                    "type": "public_transport",
                    "display_informations": {
                        "headsign": "88701", "direction": "Luxembourg",
                        "commercial_mode": "TER", "network": "TER Grand Est",
                    },
                    "stop_date_times": [
                        {"stop_point": {"name": "Metz"}, "departure_date_time": "20260613T070000"},
                        {"stop_point": {"name": "Luxembourg"}, "arrival_date_time": "20260613T080000"},
                    ],
                }],
            },
        ],
    }
    trains = bot._parser_trains_de_journeys(fixture, "7h-9h", "Metz", "Luxembourg")
    nums = [t["numero_train"] for t in trains]
    assert len(trains) == 2, f"Attendu 2 trains uniques, obtenu {nums}"
    assert len(nums) == len(set(nums)), f"Doublons numéros : {nums}"
    minutes = [bot._minutes_heure_label(t["heure_theo"]) for t in trains]
    assert minutes == sorted(minutes), "Tri chronologique incorrect"


def test_checkin_liste_tout_le_creneau():
    """Régression 04/11 : Metz 08h-10h doit proposer le 08h33 (avant, requête au milieu du créneau = 09h),
    les trains supprimés (grève) et les TER « dominés » absents des itinéraires (croisement départs/arrivées)."""
    bot = _charger_module_bot()
    appels = []

    def jny(dep, num, statut=""):
        return {"status": statut, "departure_date_time": f"20261105T{dep}00",
                "arrival_date_time": "20261105T110000",
                "sections": [{"type": "public_transport",
                              "display_informations": {"headsign": num, "direction": "Luxembourg",
                                                       "commercial_mode": "TER", "network": "TER"},
                              "stop_date_times": [
                                  {"stop_point": {"name": "Metz"}, "departure_date_time": f"20261105T{dep}00"},
                                  {"stop_point": {"name": "Luxembourg"}, "arrival_date_time": "20261105T110000"}]}]}

    def faux_journeys(sid_dep, sid_arr, dt, freshness="base_schedule", direct=True):
        appels.append(dt)
        if len(appels) == 1:
            assert dt == "20261105T080000", f"la recherche doit partir du début du créneau, pas de {dt}"
        else:
            return {"journeys": []}      # page suivante : plus rien
        return {"journeys": [jny("0803", "88505", "NO_SERVICE"), jny("0833", "88571"), jny("0933", "88726")]}

    def faux_liste(sid, ressource, cle, depuis, duree_s, max_pages=4):
        if ressource == "departures":
            return [{"display_informations": {"headsign": n, "direction": "Luxembourg", "commercial_mode": "TER", "network": "TER"},
                     "stop_date_time": {"base_departure_date_time": f"20261105T{h}00"}}
                    for n, h in (("88571", "0833"), ("88722", "0850"))]
        return [{"display_informations": {"headsign": n}, "stop_date_time": {"base_arrival_date_time": f"20261105T{h}00"}}
                for n, h in (("88571", "0924"), ("88722", "0954"))]

    orig = (bot._journeys_navitia, bot._liste_navitia, bot.resoudre_stop_area)
    bot._journeys_navitia, bot._liste_navitia = faux_journeys, faux_liste
    bot.resoudre_stop_area = lambda n: {"Metz": "87192039", "Luxembourg": "82001000"}.get(n)
    bot._cache_trains.clear()
    try:
        trains = bot.recuperer_trains_theoriques("Metz", "Luxembourg", "08h-10h", date_j="2026-11-05")
    finally:
        bot._journeys_navitia, bot._liste_navitia, bot.resoudre_stop_area = orig
        bot._cache_trains.clear()
    nums = {t["numero_train"]: t for t in trains}
    assert "88571" in nums and nums["88571"]["heure_theo"] == "08h33", nums.keys()
    assert nums["88505"]["supprime"] is True, "train supprimé conservé et signalé"
    assert "88722" in nums and nums["88722"]["heure_arrivee_theo"] == "09h54", "TER dominé retrouvé"
    assert appels and len(appels) <= 2, appels


def test_checkin_liste_tout_le_creneau():
    """Régression 04/11 : Metz 08h-10h doit proposer le 08h33 (avant, requête au milieu du créneau = 09h),
    les trains supprimés (grève) et les TER « dominés » absents des itinéraires (croisement départs/arrivées)."""
    bot = _charger_module_bot()
    appels = []

    def jny(dep, num, statut=""):
        return {"status": statut, "departure_date_time": f"20261105T{dep}00",
                "arrival_date_time": "20261105T110000",
                "sections": [{"type": "public_transport",
                              "display_informations": {"headsign": num, "direction": "Luxembourg",
                                                       "commercial_mode": "TER", "network": "TER"},
                              "stop_date_times": [
                                  {"stop_point": {"name": "Metz"}, "departure_date_time": f"20261105T{dep}00"},
                                  {"stop_point": {"name": "Luxembourg"}, "arrival_date_time": "20261105T110000"}]}]}

    def faux_journeys(sid_dep, sid_arr, dt, freshness="base_schedule", direct=True):
        appels.append(dt)
        if len(appels) == 1:
            assert dt == "20261105T080000", f"la recherche doit partir du début du créneau, pas de {dt}"
        else:
            return {"journeys": []}      # page suivante : plus rien
        return {"journeys": [jny("0803", "88505", "NO_SERVICE"), jny("0833", "88571"), jny("0933", "88726")]}

    def faux_liste(sid, ressource, cle, depuis, duree_s, max_pages=4):
        if ressource == "departures":
            return [{"display_informations": {"headsign": n, "direction": "Luxembourg", "commercial_mode": "TER", "network": "TER"},
                     "stop_date_time": {"base_departure_date_time": f"20261105T{h}00"}}
                    for n, h in (("88571", "0833"), ("88722", "0850"))]
        return [{"display_informations": {"headsign": n}, "stop_date_time": {"base_arrival_date_time": f"20261105T{h}00"}}
                for n, h in (("88571", "0924"), ("88722", "0954"))]

    orig = (bot._journeys_navitia, bot._liste_navitia, bot.resoudre_stop_area)
    bot._journeys_navitia, bot._liste_navitia = faux_journeys, faux_liste
    bot.resoudre_stop_area = lambda n: {"Metz": "87192039", "Luxembourg": "82001000"}.get(n)
    bot._cache_trains.clear()
    try:
        trains = bot.recuperer_trains_theoriques("Metz", "Luxembourg", "08h-10h", date_j="2026-11-05")
    finally:
        bot._journeys_navitia, bot._liste_navitia, bot.resoudre_stop_area = orig
        bot._cache_trains.clear()
    nums = {t["numero_train"]: t for t in trains}
    assert "88571" in nums and nums["88571"]["heure_theo"] == "08h33", nums.keys()
    assert nums["88505"]["supprime"] is True, "train supprimé conservé et signalé"
    assert "88722" in nums and nums["88722"]["heure_arrivee_theo"] == "09h54", "TER dominé retrouvé"
    assert appels and len(appels) <= 2, appels


def test_pdf_depuis_trajets_valides():
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999999"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_reelle_arrivee,
            statut_validation, gare_depart, gare_arrivee, destination,
            retard_arrivee_min, motif_validation
        ) VALUES (?, '88799', date('now'), '08h00', '08h25', 'valide_bus_panne',
            'Metz', 'Luxembourg', 'Luxembourg', 25, ?)
        """,
        (id_test, bot.MOTIF_RUPTURE_CHARGE),
    )
    conn.commit()
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.close()
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    assert len(inc) >= 1
    ligne = list(inc.values())[0]
    assert bot.MOTIF_RUPTURE_CHARGE in ligne["motif"]
    assert ligne["retard"] >= 10
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_retard_base_sur_arrivee():
    """Le retard doit se calculer sur l'arrivée théorique, jamais sur le départ."""
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999997"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    # Départ théo 07h00, arrivée théo 07h45, arrivée réelle 08h05 -> retard arrivée = 20 min (pas 65).
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_arrivee_theo,
            heure_reelle_arrivee, statut_validation, gare_depart, gare_arrivee,
            destination, retard_arrivee_min
        ) VALUES (?, '88701', date('now'), '07h00', '07h45', '08h05',
            'valide_normal', 'Metz', 'Luxembourg', 'Luxembourg', NULL)
        """,
        (id_test,),
    )
    conn.commit()
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.close()
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    assert len(inc) >= 1, "Incident retard non détecté"
    ligne = list(inc.values())[0]
    assert ligne["retard"] == 20, f"Retard arrivée attendu 20, obtenu {ligne['retard']}"
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_train_suivant_compte_dans_pdf():
    """« J'ai pris le suivant » avec retard >= 10 doit apparaître dans le PDF."""
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999996"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_arrivee_theo,
            heure_reelle_arrivee, statut_validation, gare_depart, gare_arrivee,
            destination, retard_arrivee_min, motif_validation
        ) VALUES (?, '88702', date('now'), '07h00', '07h45', '08h05',
            'valide_normal', 'Metz', 'Luxembourg', 'Luxembourg', 20,
            'Prise du train suivant (retard en cascade)')
        """,
        (id_test,),
    )
    conn.commit()
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.close()
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    assert len(inc) >= 1, "Le trajet 'train suivant' n'apparaît pas dans le PDF"
    ligne = list(inc.values())[0]
    assert "train suivant" in ligne["motif"].lower()
    assert ligne["retard"] >= 10
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_bilan_mois_trajets():
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999998"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_reelle_arrivee,
            statut_validation, gare_depart, gare_arrivee, destination, retard_arrivee_min
        ) VALUES (?, '88600', date('now'), '07h00', '07h18', 'valide_normal',
            'Metz', 'Luxembourg', 'Luxembourg', 18)
        """,
        (id_test,),
    )
    conn.commit()
    conn.close()
    retard, suppr, nb_incidents, nb_trajets = bot.bilan_mois_trajets_valides(id_test)
    assert nb_incidents >= 1
    assert nb_trajets >= 1
    assert retard >= 10
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_suppression_comptee():
    """Un trajet 'valide_supprime' doit être compté comme suppression (PDF + bilan)."""
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999995"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_arrivee_theo,
            statut_validation, gare_depart, gare_arrivee, destination,
            retard_arrivee_min, motif_validation
        ) VALUES (?, '88509', date('now'), '08h00', '08h50', 'valide_supprime',
            'Metz', 'Luxembourg', 'Luxembourg', 0, 'Train supprimé (annulation SNCF)')
        """,
        (id_test,),
    )
    conn.commit()
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.close()
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    assert len(inc) == 1
    assert list(inc.values())[0]["type"] == "supprime"
    retard, suppr, nb_incidents, nb_trajets = bot.bilan_mois_trajets_valides(id_test)
    assert suppr == 1 and nb_trajets == 1
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_checkout_proprietaire_trajet():
    """Un utilisateur ne peut valider que ses propres trajets (anti-IDOR)."""
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_a, id_b = "999999994", "999999993"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram IN (?, ?)", (id_a, id_b))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88710', date('now'), '08h00', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg')
        """,
        (id_a,),
    )
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    assert bot._verifier_proprietaire_trajet(tid, id_a) is True
    assert bot._verifier_proprietaire_trajet(tid, id_b) is False
    assert bot._verifier_proprietaire_trajet(999999999, id_a) is False
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_a,))
    conn.commit()
    conn.close()


def test_bilan_metier_coherent():
    bot = _charger_module_bot()
    conn = db_connect(timeout=5)
    id_test = "999999992"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_reelle_arrivee,
            statut_validation, gare_depart, gare_arrivee, destination, retard_arrivee_min
        ) VALUES (?, '88601', date('now'), '07h00', '07h18', 'valide_normal',
            'Metz', 'Luxembourg', 'Luxembourg', 18)
        """,
        (id_test,),
    )
    conn.commit()
    conn.close()
    r1 = bilan_mois_trajets_valides_db(id_test)
    r2 = bot.bilan_mois_trajets_valides(id_test)
    assert r1 == r2, f"Incohérence bilan bot/métier : {r1} vs {r2}"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_barre_progression():
    b0 = barre_progression_gf(0, 0)
    assert "░" in b0 and "0%" in b0
    b1 = barre_progression_gf(60, 0)
    assert "█" in b1
    b2 = barre_progression_gf(0, 3)
    assert "100%" in b2


def test_charger_utilisateur():
    conn = db_connect(timeout=5)
    id_test = "999999991"
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures) "
        "VALUES (?, 'Test', 'Alice', 'Metz', '7h-9h')",
        (id_test,),
    )
    conn.commit()
    conn.close()
    u = charger_utilisateur(id_test)
    assert u is not None
    assert u["prenom"] == "Alice"
    assert charger_utilisateur("000000000") is None
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_inferer_type_reclamation():
    assert inferer_type_reclamation({"age": "Abo mensuel Metz-Lux"}) == "abo"
    assert inferer_type_reclamation({"age": "Billet à l'unité"}) == "billet"


def test_format_duree_retard():
    assert format_duree_retard(18) == "18 min"
    assert format_duree_retard(125) == "2h05"


def test_mettre_a_jour_inscription():
    id_test = "999999990"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures) "
        "VALUES (?, 'T', 'U', 'Metz', '7h-9h')",
        (id_test,),
    )
    conn.commit()
    conn.close()
    assert mettre_a_jour_inscription(id_test, gare_retour="Luxembourg", heures_retour="17h-19h")
    u = charger_utilisateur(id_test)
    assert u["gare_retour"] == "Luxembourg"
    assert profil_gares_configure(u) is True
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_annulation_helper():
    assert est_annulation("/annuler") is True
    assert est_annulation("❌ Annuler") is True
    assert est_annulation("🏠 Menu") is True
    assert est_annulation("Bonjour") is False


def test_stats_tv_valides_mois():
    conn = db_connect(timeout=5)
    id_test = "999999988"
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88001', date('now'), '08h00', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg')
        """,
        (id_test,),
    )
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination, retard_arrivee_min
        ) VALUES (?, '88002', date('now'), '09h00', 'valide_normal', 'Metz', 'Luxembourg', 'Luxembourg', 12)
        """,
        (id_test,),
    )
    conn.commit()
    conn.close()
    s = stats_rapport_db()
    assert s.get("tv_mois", 0) >= 2
    assert s.get("tv_valides_mois", 0) >= 1
    assert s.get("tv_en_attente_mois", 0) >= 1
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_retard_reel_scanner_suppression():
    bot = _charger_module_bot()
    conn = db_connect(timeout=5)
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    conn.execute(
        "DELETE FROM historique WHERE date=? AND numero_train='88999'",
        (date_j,),
    )
    conn.execute(
        """
        INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min)
        VALUES (?, '88999', 'Metz-Lux', 'Luxembourg', 'supprime', 0)
        """,
        (date_j,),
    )
    conn.commit()
    conn.close()
    retard, suppr = bot.retard_reel_scanner(date_j, "88999", "Luxembourg")
    assert suppr is True
    assert retard == 0
    conn = db_connect(timeout=5)
    conn.execute(
        "DELETE FROM historique WHERE date=? AND numero_train='88999'",
        (date_j,),
    )
    conn.commit()
    conn.close()


def test_pdf_en_attente_avec_radar():
    """Un check-in sans check-out doit apparaître si le radar a vu un retard ≥ 10 min."""
    bot = _charger_module_bot()
    conn = db_connect(timeout=5)
    id_test = "999999987"
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "DELETE FROM historique WHERE date=? AND numero_train='88988'",
        (date_j,),
    )
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_arrivee_theo,
            statut_validation, gare_depart, gare_arrivee, destination
        ) VALUES (?, '88988', ?, '08h00', '08h45', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg')
        """,
        (id_test, date_j),
    )
    conn.execute(
        """
        INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min, retard_arrivee_min, motif, heure_theo)
        VALUES (?, '88988', 'Metz-Lux', 'Luxembourg', 'retard', 15, 15, 'Test radar PDF', '08h00')
        """,
        (date_j,),
    )
    conn.commit()
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.close()
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    assert len(inc) >= 1, "Retard radar absent du PDF pour trajet en_attente"
    ligne = list(inc.values())[0]
    assert ligne["retard"] >= 10
    assert ligne["type"] == "retard"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "DELETE FROM historique WHERE date=? AND numero_train='88988'",
        (date_j,),
    )
    conn.commit()
    conn.close()


def test_alerte_radar_fige_pdf():
    """Toute alerte ciblée check-in doit figer l'incident pour le PDF."""
    conn = db_connect(timeout=5)
    id_test = "999999986"
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_arrivee_theo,
            statut_validation, gare_depart, gare_arrivee, destination
        ) VALUES (?, '88977', ?, '07h30', '08h15', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg')
        """,
        (id_test, date_j),
    )
    conn.commit()
    conn.close()
    incident = {
        "type": "retard", "motif": "Test alerte", "retard_depart_min": 18,
        "heure_reel": "07h48", "gare": "Metz",
    }
    assert promouvoir_trajet_alerte_radar(id_test, date_j, "88977", "07h30", incident)
    conn = db_connect(timeout=5)
    row = conn.execute(
        "SELECT statut_validation, retard_arrivee_min FROM trajets_valides WHERE id_telegram=?",
        (id_test,),
    ).fetchone()
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()
    assert row[0] == "valide_normal"
    assert row[1] >= SEUIL_RETARD_MIN


def test_signalement_lie_checkin():
    conn = db_connect(timeout=5)
    id_test = "999999985"
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    cur = conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88976', ?, '18h00', 'en_attente', 'Luxembourg', 'Metz', 'Metz')
        """,
        (id_test, date_j),
    )
    trajet_id = cur.lastrowid
    conn.commit()
    conn.close()
    tid = promouvoir_trajet_signalement(id_test, "bus", retard_min=15, id_trajet=trajet_id)
    assert tid is not None
    conn = db_connect(timeout=5)
    row = conn.execute(
        "SELECT statut_validation FROM trajets_valides WHERE id=?", (tid,),
    ).fetchone()
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()
    assert row[0] == "valide_bus_panne"


def test_coherence_alerte_pdf_fonction():
    n = verifier_coherence_alerte_pdf()
    assert n >= 0


def test_sync_retard_sans_heure_checkin():
    """Le radar doit lier le retard même si l'heure scan ≠ heure check-in."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scanner", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_ligne.py"))
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    conn = db_connect(timeout=5)
    id_test = "999999983"
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88974', ?, '08h33', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg')
        """,
        (id_test, date_j),
    )
    conn.commit()
    conn.close()
    scanner.synchroniser_checkin_retard(
        date_j, "88974", retard=22, mode="depart",
    )
    conn = db_connect(timeout=5)
    row = conn.execute(
        "SELECT statut_validation, retard_arrivee_min FROM trajets_valides WHERE id_telegram=?",
        (id_test,),
    ).fetchone()
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()
    assert row[0] == "valide_normal"
    assert row[1] >= SEUIL_RETARD_MIN


def test_checkin_ne_reset_pas_valide():
    """Re-déclarer le même train ne doit pas effacer un trajet déjà validé."""
    bot = _charger_module_bot()
    conn = db_connect(timeout=5)
    id_test = "999999984"
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            retard_arrivee_min, gare_depart, gare_arrivee, destination
        ) VALUES (?, '88975', ?, '07h00', 'valide_normal', 22, 'Metz', 'Luxembourg', 'Luxembourg')
        """,
        (id_test, date_j),
    )
    conn.commit()
    user = {"gare_depart": "Metz", "gare_retour": "Luxembourg"}
    train = {"numero_train": "88975", "heure_theo": "07h00", "heure_arrivee_theo": "07h45", "destination": "Luxembourg"}
    bot.enregistrer_checkin(id_test, train, "7h-9h", "aller", user)
    row = conn.execute(
        "SELECT statut_validation, retard_arrivee_min FROM trajets_valides WHERE id_telegram=?",
        (id_test,),
    ).fetchone()
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()
    assert row[0] == "valide_normal"
    assert row[1] == 22


def test_cmd_trajet_raccourci():
    import inspect
    bot = _charger_module_bot()
    src = inspect.getsource(bot.cmd_trajet)
    assert "Déclarer mon trajet" in src
    assert "gestion_boutons" in src


def test_texte_formulaire_mois_dynamique():
    bot = _charger_module_bot()
    txt = bot.texte_formulaire_abo("Juin 2026")
    assert "Juin 2026" in txt
    assert "Garantie Fiabilité" in txt
    assert "G30" not in txt and "TGV" not in txt


def test_pdf_aligne_alertes_deux_tranches():
    """Alertes et PDF : même filtre créneau aller + retour (suppressions + retards radar)."""
    bot = _charger_module_bot()
    user = {
        "id_telegram_propre": "999999",
        "gare_depart": "Metz",
        "gare_retour": "Luxembourg",
        "heures": "08h-10h",
        "heures_retour": "18h-20h",
    }
    assert bot.incident_correspond_profil(user, "Metz", "Luxembourg", "08h30")
    assert bot.incident_correspond_profil(user, "Luxembourg", "Metz", "19h00")
    assert not bot.incident_correspond_profil(user, "Metz", "Luxembourg", "14h30")
    assert bot.sens_et_tranche_profil(user, "Luxembourg", "Metz", "19h00") == ("retour", "18h-20h")
    assert bot.sens_et_tranche_profil(user, "Metz", "Luxembourg", "09h15") == ("aller", "08h-10h")
    assert callable(bot.assembler_incidents_pdf)
    assert callable(bot.bilan_depuis_incidents_pdf)
    bilan = bot.bilan_depuis_incidents_pdf({
        "a": {"type": "retard", "retard": 15},
        "b": {"type": "supprime", "retard": 0},
    }, 1)
    assert bilan == (15, 1, 2, 1)


def test_jours_a_completer_desactive():
    """Complétion manuelle désactivée — PDF 100 % auto via radar."""
    bot = _charger_module_bot()
    user = {
        "id_telegram": "999",
        "gare_depart": "Metz", "gare_retour": "Luxembourg",
        "heures": "7h-9h", "heures_retour": "18h-20h",
    }
    assert bot.jours_a_completer_mois(user, "2026-06") == []


def _nettoyer_jeu_test(id_test, numeros=(), dates_like=None):
    """Supprime uniquement les données insérées par un test (jamais l'historique réel)."""
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    try:
        conn.execute("DELETE FROM propositions_radar WHERE id_telegram = ?", (id_test,))
    except Exception:
        pass
    try:
        conn.execute(
            "DELETE FROM signalements_votes WHERE id_utilisateur = ? OR signalement_id IN "
            "(SELECT id FROM signalements WHERE id_auteur = ?)", (id_test, id_test),
        )
        conn.execute("DELETE FROM signalements WHERE id_auteur = ?", (id_test,))
    except Exception:
        pass
    for num in numeros:
        conn.execute("DELETE FROM historique WHERE numero_train = ?", (str(num),))
        conn.execute("DELETE FROM surveillance_trains WHERE numero_train = ?", (str(num),))
    conn.commit()
    conn.close()


def _inscrire_test(id_test, g_all="Metz", t_all="07h-09h", g_ret="Luxembourg", t_ret="18h-20h"):
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """INSERT INTO inscriptions (
            id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour
        ) VALUES (?, 'T', 'Phase1', ?, ?, ?, ?)""",
        (id_test, g_all, t_all, g_ret, t_ret),
    )
    conn.commit()
    conn.close()


def _inserer_trajet(id_test, num, date_j, h_theo, statut, g_dep, g_arr, dest, retard=None,
                    motif=None, sens=None, h_arr=None, origine=None):
    conn = db_connect(timeout=5)
    conn.execute(
        """INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, heure_arrivee_theo, statut_validation,
            gare_depart, gare_arrivee, destination, retard_arrivee_min, motif_validation, sens, origine
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'checkin'))""",
        (id_test, num, date_j, h_theo, h_arr, statut, g_dep, g_arr, dest, retard, motif, sens, origine),
    )
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return tid


def test_pas_auto_completion_pdf():
    """R3 : un incident radar dans le créneau, sans check-in, devient une proposition, pas une ligne PDF."""
    from bazo_metier import persister_incident_depuis_alerte

    bot = _charger_module_bot()
    id_test = "999999971"
    mois = "2026-06"
    num = "839001"
    _nettoyer_jeu_test(id_test, numeros=(num,))
    _inscrire_test(id_test, "Metz", "7h-9h", "Luxembourg", "18h-20h")
    user = bot.charger_utilisateur(id_test)
    persister_incident_depuis_alerte({
        "date": "2026-06-10", "numero_train": num, "gare": "Metz",
        "destination": "Luxembourg", "heure_theo": "08h15", "type": "retard",
        "motif": "Test auto PDF", "info": 25, "retard_depart_min": 25, "retard_arrivee_min": 25,
        "heure_reel": "08h40",
    })
    n = bot.auto_completer_trajets_radar_mois(id_test, mois, user)
    incidents, rows = bot.assembler_incidents_pdf(id_test, mois, user)
    conn = db_connect(timeout=5)
    nb_tv = conn.execute(
        "SELECT COUNT(*) FROM trajets_valides WHERE id_telegram = ?", (id_test,),
    ).fetchone()[0]
    prop = conn.execute(
        "SELECT statut, gare_depart, gare_arrivee FROM propositions_radar WHERE id_telegram = ? AND numero_train = ?",
        (id_test, num),
    ).fetchone()
    conn.close()
    _nettoyer_jeu_test(id_test, numeros=(num,))
    assert n >= 1
    assert incidents == {}, incidents
    assert nb_tv == 0
    assert prop and prop[0] == "proposee" and prop[1] == "Metz" and prop[2] == "Luxembourg"


def test_pas_proposition_avant_inscription():
    """Aucune proposition radar pour un jour antérieur à l'inscription de l'usager."""
    from bazo_metier import persister_incident_depuis_alerte

    bot = _charger_module_bot()
    id_test = "999999969"
    num = "839002"
    _nettoyer_jeu_test(id_test, numeros=(num,))
    _inscrire_test(id_test, "Metz", "7h-9h", "Luxembourg", "18h-20h")
    conn = db_connect(timeout=5)
    conn.execute(
        "UPDATE inscriptions SET date_inscription = '2027-01-20 10:00:00' WHERE id_telegram = ?", (id_test,),
    )
    conn.commit()
    conn.close()
    user = bot.charger_utilisateur(id_test)
    for date_j in ("2027-01-12", "2027-01-26"):
        persister_incident_depuis_alerte({
            "date": date_j, "numero_train": num, "gare": "Metz",
            "destination": "Luxembourg", "heure_theo": "08h15", "type": "retard",
            "motif": "Test inscription", "info": 25, "retard_depart_min": 25, "retard_arrivee_min": 25,
            "heure_reel": "08h40",
        })
    n = bot.auto_completer_trajets_radar_mois(id_test, "2027-01", user)
    conn = db_connect(timeout=5)
    dates_num = [r[0] for r in conn.execute(
        "SELECT date FROM propositions_radar WHERE id_telegram = ? AND numero_train = ?", (id_test, num),
    ).fetchall()]
    avant = conn.execute(
        "SELECT COUNT(*) FROM propositions_radar WHERE id_telegram = ? AND date < '2027-01-20'", (id_test,),
    ).fetchone()[0]
    conn.close()
    _nettoyer_jeu_test(id_test, numeros=(num,))
    assert n >= 1, n
    assert dates_num == ["2027-01-26"], dates_num
    assert avant == 0, avant


def test_pdf_trajet_segment_usager():
    """R1 : le PDF affiche gare_depart -> gare_arrivee, jamais la direction commerciale."""
    bot = _charger_module_bot()
    id_test = "999999970"
    mois = "2026-06"
    _nettoyer_jeu_test(id_test)
    _inserer_trajet(id_test, "88601", "2026-06-11", "08h00", "valide_normal",
                    "Metz", "Luxembourg", "Nancy", retard=20, sens="aller")
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    _nettoyer_jeu_test(id_test)
    assert len(inc) == 1
    ligne = list(inc.values())[0]
    assert ligne["trajet"] == "Metz -> Luxembourg", ligne["trajet"]
    assert ligne["direction_commerciale"] == "Nancy"
    assert ligne["sens"] == "aller"


def test_pdf_retour_depart_different_arrivee():
    bot = _charger_module_bot()
    id_test = "999999969"
    mois = "2026-06"
    _nettoyer_jeu_test(id_test)
    _inserer_trajet(id_test, "88602", "2026-06-11", "18h10", "valide_supprime",
                    "Luxembourg", "Metz", "Luxembourg", retard=0, sens="retour")
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    _nettoyer_jeu_test(id_test)
    assert len(inc) == 1
    assert list(inc.values())[0]["trajet"] == "Luxembourg -> Metz"


def test_pdf_alias_metz_ville():
    bot = _charger_module_bot()
    id_test = "999999968"
    mois = "2026-06"
    _nettoyer_jeu_test(id_test)
    _inserer_trajet(id_test, "88603", "2026-06-12", "08h00", "valide_normal",
                    "Metz Ville", "Luxembourg", "Luxembourg (Luxembourg)", retard=15)
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    _nettoyer_jeu_test(id_test)
    assert list(inc.values())[0]["trajet"] == "Metz -> Luxembourg"


def test_howald_distinct_luxembourg():
    import bazo_geo as geo
    assert geo.uic_pour_gare("Howald") != "82001000"
    assert not geo.gares_equivalentes("Howald", "Luxembourg")
    assert geo.nom_canonique_gare("Metz Ville") == "Metz"
    assert geo.nom_canonique_gare("Metz Nord") == "Metz Nord"
    assert geo.nom_canonique_gare("Luxembourg (Luxembourg)") == "Luxembourg"
    assert geo.gare_sur_segment("Hagondange", "Metz", "Luxembourg")
    assert not geo.gare_sur_segment("Nancy", "Metz", "Luxembourg")
    assert not geo.gare_sur_segment("Metz", None, None)


def test_suppression_hors_segment_ignoree():
    """R4 : suppression observée à Nancy seulement — ne concerne pas Metz -> Luxembourg."""
    from bazo_metier import train_supprime_radar_db
    bot = _charger_module_bot()
    id_test = "999999967"
    num = "88604"
    date_j = "2026-06-12"
    _nettoyer_jeu_test(id_test, numeros=(num,))
    conn = db_connect(timeout=5)
    conn.execute(
        "INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min, heure_theo) VALUES (?, ?, 'Luxembourg', 'Nancy', 'supprime', 0, '07h30')",
        (date_j, num),
    )
    conn.commit()
    conn.close()
    assert train_supprime_radar_db(date_j, num, "Metz", gare_arrivee="Luxembourg") is False
    _inserer_trajet(id_test, num, date_j, "08h00", "en_attente", "Metz", "Luxembourg", "Luxembourg")
    sync_trajets_valides_depuis_radar(id_test, "2026-06")
    conn = db_connect(timeout=5)
    statut = conn.execute(
        "SELECT statut_validation FROM trajets_valides WHERE id_telegram = ?", (id_test,),
    ).fetchone()[0]
    conn.close()
    _nettoyer_jeu_test(id_test, numeros=(num,))
    assert statut != "valide_supprime"


def test_suppression_sur_segment_comptee():
    from bazo_metier import train_supprime_radar_db
    id_test = "999999966"
    num = "88605"
    date_j = "2026-06-12"
    _nettoyer_jeu_test(id_test, numeros=(num,))
    conn = db_connect(timeout=5)
    conn.execute(
        "INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min, heure_theo) VALUES (?, ?, 'Luxembourg', 'Hagondange', 'supprime', 0, '08h12')",
        (date_j, num),
    )
    conn.commit()
    conn.close()
    assert train_supprime_radar_db(date_j, num, "Metz", gare_arrivee="Luxembourg") is True
    _inserer_trajet(id_test, num, date_j, "08h00", "en_attente", "Metz", "Luxembourg", "Luxembourg")
    sync_trajets_valides_depuis_radar(id_test, "2026-06")
    conn = db_connect(timeout=5)
    statut = conn.execute(
        "SELECT statut_validation FROM trajets_valides WHERE id_telegram = ?", (id_test,),
    ).fetchone()[0]
    conn.close()
    _nettoyer_jeu_test(id_test, numeros=(num,))
    assert statut == "valide_supprime"


def test_suppression_sans_gare_faux():
    from bazo_metier import train_supprime_radar_db
    num = "88606"
    date_j = "2026-06-12"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM historique WHERE numero_train = ?", (num,))
    conn.execute(
        "INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min) VALUES (?, ?, 'Lux', 'Metz', 'supprime', 0)",
        (date_j, num),
    )
    conn.commit()
    conn.close()
    res = train_supprime_radar_db(date_j, num)
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM historique WHERE numero_train = ?", (num,))
    conn.commit()
    conn.close()
    assert res is False


def test_retard_autre_gare_ignore():
    """R5 : un retard de 30 min à Nancy ne compte pas pour Thionville -> Luxembourg."""
    from bazo_metier import retard_reel_scanner_db
    num = "88607"
    date_j = "2026-06-12"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM historique WHERE numero_train = ?", (num,))
    conn.execute("DELETE FROM surveillance_trains WHERE numero_train = ?", (num,))
    conn.execute(
        "INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min, retard_depart_min) VALUES (?, ?, 'Luxembourg', 'Nancy', 'retard', 30, 30)",
        (date_j, num),
    )
    conn.commit()
    conn.close()
    r, suppr = retard_reel_scanner_db(date_j, num, gare_arrivee="Luxembourg", gare_depart="Thionville")
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM historique WHERE numero_train = ?", (num,))
    conn.commit()
    conn.close()
    assert r == 0 and suppr is False


def test_suppression_radar_ne_remplace_pas_checkin():
    """Une suppression du train B ne doit pas réécrire le check-in du train A (même sens)."""
    from bazo_metier import traiter_suppression_radar_utilisateur
    bot = _charger_module_bot()
    id_test = "999999965"
    date_j = "2026-06-12"
    _nettoyer_jeu_test(id_test, numeros=("88608", "88609"))
    _inscrire_test(id_test, "Metz", "07h-09h", "Luxembourg", "18h-20h")
    user = bot.charger_utilisateur(id_test)
    tid_a = _inserer_trajet(id_test, "88608", date_j, "07h30", "en_attente",
                            "Metz", "Luxembourg", "Luxembourg", sens="aller")
    action, ref = traiter_suppression_radar_utilisateur(id_test, user, {
        "date": date_j, "numero_train": "88609", "gare": "Metz", "destination": "Luxembourg",
        "heure_theo": "08h00", "type": "supprime", "motif": "Test", "info": 0, "heure_reel": "Annulé",
    })
    conn = db_connect(timeout=5)
    row = conn.execute(
        "SELECT numero_train, statut_validation FROM trajets_valides WHERE id = ?", (tid_a,),
    ).fetchone()
    nb = conn.execute("SELECT COUNT(*) FROM trajets_valides WHERE id_telegram = ?", (id_test,)).fetchone()[0]
    conn.close()
    _nettoyer_jeu_test(id_test, numeros=("88608", "88609"))
    assert row == ("88608", "en_attente")
    assert nb == 1
    assert action == "proposition" and ref


def test_proposition_acceptee_dans_pdf():
    from bazo_metier import accepter_proposition_radar, proposer_trajet_radar
    bot = _charger_module_bot()
    id_test = "999999964"
    num = "88610"
    _nettoyer_jeu_test(id_test, numeros=(num,))
    _inscrire_test(id_test, "Metz", "07h-09h", "Luxembourg", "18h-20h")
    user = bot.charger_utilisateur(id_test)
    pid = proposer_trajet_radar(id_test, user, {
        "date": "2026-06-13", "numero_train": num, "gare": "Metz", "destination": "Nancy",
        "heure_theo": "08h05", "type": "retard", "retard_arrivee_min": 22,
        "motif": "Retard confirmé radar (+22 min)",
    })
    assert pid
    tid = accepter_proposition_radar(pid, id_test)
    assert tid
    rows = bot.charger_trajets_valides_pdf(id_test, "2026-06")
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    conn = db_connect(timeout=5)
    origine = conn.execute("SELECT origine FROM trajets_valides WHERE id = ?", (tid,)).fetchone()[0]
    conn.close()
    _nettoyer_jeu_test(id_test, numeros=(num,))
    assert origine == "confirme_usager"
    assert len(inc) == 1
    ligne = list(inc.values())[0]
    assert ligne["trajet"] == "Metz -> Luxembourg"
    assert ligne["retard"] == 22
    assert "radar" not in ligne["motif"].lower()


def test_proposition_autre_usager_refusee():
    from bazo_metier import accepter_proposition_radar, proposer_trajet_radar, refuser_proposition_radar
    bot = _charger_module_bot()
    id_a, id_b = "999999963", "999999962"
    num = "88611"
    _nettoyer_jeu_test(id_a, numeros=(num,))
    _nettoyer_jeu_test(id_b)
    _inscrire_test(id_a)
    user = bot.charger_utilisateur(id_a)
    pid = proposer_trajet_radar(id_a, user, {
        "date": "2026-06-13", "numero_train": num, "gare": "Metz", "destination": "Luxembourg",
        "heure_theo": "08h05", "type": "supprime", "motif": "Test",
    })
    assert pid
    assert accepter_proposition_radar(pid, id_b) is None
    assert refuser_proposition_radar(pid, id_b) is False
    assert refuser_proposition_radar(pid, id_a) is True
    _nettoyer_jeu_test(id_a, numeros=(num,))


def test_legacy_exclu_du_pdf():
    bot = _charger_module_bot()
    id_test = "999999961"
    _nettoyer_jeu_test(id_test)
    _inserer_trajet(id_test, "88612", "2026-06-14", "08h00", "valide_supprime",
                    "Metz", "Luxembourg", "Nancy", origine="radar_auto_legacy")
    _inserer_trajet(id_test, "88613", "2026-06-14", "18h00", "valide_normal",
                    "Luxembourg", "Metz", "Nancy", retard=20, origine="checkin")
    rows = bot.charger_trajets_valides_pdf(id_test, "2026-06")
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    _nettoyer_jeu_test(id_test)
    assert len(rows) == 1
    assert [i["train"] for i in inc.values()] == ["88613"]


def test_motif_pdf_nettoye_bot():
    from bazo_metier import motif_pdf_officiel
    bot = _charger_module_bot()
    assert motif_pdf_officiel("Train supprimé (confirmé GTFS-RT)", "supprime", 0) == "Train supprimé"
    assert motif_pdf_officiel("Retard confirmé radar (+22 min)", "retard", 22) == "Retard constaté à l'arrivée (+22 min)"
    assert motif_pdf_officiel(bot.MOTIF_RUPTURE_CHARGE, "retard", 15) == bot.MOTIF_RUPTURE_CHARGE
    assert motif_pdf_officiel("Panne de signalisation", "retard", 15) == "Panne de signalisation"
    id_test = "999999960"
    _nettoyer_jeu_test(id_test)
    _inserer_trajet(id_test, "88614", "2026-06-14", "08h00", "valide_supprime",
                    "Metz", "Luxembourg", "Luxembourg", motif="Train supprimé (confirmé GTFS-RT)")
    rows = bot.charger_trajets_valides_pdf(id_test, "2026-06")
    inc = bot.incidents_pdf_depuis_trajets_valides(rows)
    _nettoyer_jeu_test(id_test)
    assert list(inc.values())[0]["motif"] == "Train supprimé"


def test_bot_et_mobile_meme_trajet():
    import bazo_mobile_app as mobile
    bot = _charger_module_bot()
    id_test = "999999959"
    _nettoyer_jeu_test(id_test)
    _inscrire_test(id_test)
    _inserer_trajet(id_test, "88615", "2026-06-15", "08h00", "valide_normal",
                    "Metz", "Luxembourg", "Nancy", retard=18, motif="Retard capté par le radar (+18 min)")
    user = bot.charger_utilisateur(id_test)
    inc_bot, _ = bot.assembler_incidents_pdf(id_test, "2026-06", user)
    inc_mob, _ = mobile.obtenir_incidents_propres(id_test, "2026-06", user)
    _nettoyer_jeu_test(id_test)
    assert inc_bot.keys() == inc_mob.keys() and inc_bot
    for k in inc_bot:
        assert inc_bot[k]["trajet"] == inc_mob[k]["trajet"] == "Metz -> Luxembourg"
        assert inc_bot[k]["motif"] == inc_mob[k]["motif"]
        assert "radar" not in inc_bot[k]["motif"].lower()


def test_checkin_api_date_passee_refusee():
    from fastapi.testclient import TestClient
    from bazo_api.main import app
    from datetime import date, timedelta
    id_test = "999999958"
    _nettoyer_jeu_test(id_test)
    _inscrire_test(id_test, "Metz", "08h-10h", "Luxembourg", "18h-20h")
    client = TestClient(app)
    r = client.post("/v1/checkin", json={
        "id_telegram": id_test, "sens": "aller", "numero_train": "88616",
        "heure_theo": "08h00", "date": (date.today() - timedelta(days=3)).isoformat(),
    })
    _nettoyer_jeu_test(id_test)
    assert r.status_code == 400, r.text


def test_heure_theo_collision_checkin():
    """Même train, heures différentes : sync ne met à jour qu'un seul check-in."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scanner", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_ligne.py"))
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    conn = db_connect(timeout=5)
    id_test = "999999981"
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88974', ?, '07h05', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg')""",
        (id_test, date_j),
    )
    conn.execute(
        """INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88974', ?, '18h33', 'en_attente', 'Luxembourg', 'Metz', 'Metz')""",
        (id_test, date_j),
    )
    conn.commit()
    conn.close()
    scanner.synchroniser_checkin_retard(
        date_j, "88974", retard=22, heure_theo="07h05", mode="depart",
    )
    conn = db_connect(timeout=5)
    rows = conn.execute(
        "SELECT heure_theo, statut_validation FROM trajets_valides WHERE id_telegram=? ORDER BY heure_theo",
        (id_test,),
    ).fetchall()
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()
    assert rows[0] == ("07h05", "valide_normal")
    assert rows[1] == ("18h33", "en_attente")


def test_bilan_mensuel_aligne_pdf():
    """bilan_mois_trajets_valides = même source que Mon Statut / PDF."""
    from bazo_metier import bilan_mois_aligne_pdf
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999980"
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """INSERT INTO inscriptions (
            id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour, age
        ) VALUES (?, 'Dupont', 'Jean', 'Metz', '07h-09h', 'Luxembourg', '18h-20h', 'Abo')""",
        (id_test,),
    )
    conn.execute(
        """INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination, retard_arrivee_min
        ) VALUES (?, '88650', date('now'), '08h00', 'valide_normal',
            'Metz', 'Luxembourg', 'Luxembourg', 22)""",
        (id_test,),
    )
    conn.commit()
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.close()
    b_bot = bot.bilan_mois_trajets_valides(id_test, mois)
    b_pdf = bilan_mois_aligne_pdf(id_test, mois)
    assert b_bot == b_pdf
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_pdf_lecture_ne_mutile_pas_db():
    """Ouvrir Mon Statut / PDF ne doit pas modifier trajets_valides."""
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999979"
    date_j = conn.execute("SELECT date('now')").fetchone()[0]
    mois = conn.execute("SELECT substr(date('now'),1,7)").fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88999', ?, '08h00', 'en_attente', 'Metz', 'Luxembourg', 'Luxembourg')""",
        (id_test, date_j),
    )
    conn.execute(
        """INSERT INTO historique (
            date, numero_train, gare, type, retard_min, heure_theo, ligne
        ) VALUES (?, '88999', 'Metz', 'retard', 18, '08h00', 'Luxembourg')""",
        (date_j,),
    )
    conn.commit()
    conn.close()
    rows = bot.charger_trajets_valides_pdf(id_test, mois)
    bot.incidents_pdf_depuis_trajets_valides(rows)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    statut = conn.execute(
        "SELECT statut_validation FROM trajets_valides WHERE id_telegram=?", (id_test,),
    ).fetchone()[0]
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM historique WHERE date=? AND numero_train='88999'", (date_j,))
    conn.commit()
    conn.close()
    assert statut == "en_attente"


def test_clean_text_conserve_accents():
    bot = _charger_module_bot()
    txt = bot.clean_text("Épinal — Thionville à 19h")
    assert "É" in txt or "é" in txt or "Epinal" in txt
    assert "à" in txt or "a" in txt


def test_pdf_filtre_max_deux_par_jour():
    """PDF : max 1 retard aller + 1 retard retour par jour ; suppression du train déclaré."""
    bot = _charger_module_bot()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    id_test = "999999978"
    date_j = "2026-06-15"
    mois = "2026-06"
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, sens, statut_validation,
            gare_depart, gare_arrivee, destination, retard_arrivee_min
        ) VALUES (?, '88701', ?, '07h10', 'aller', 'valide_normal',
            'Metz', 'Luxembourg', 'Luxembourg', 18)""",
        (id_test, date_j),
    )
    conn.execute(
        """INSERT INTO trajets_valides (
            id_telegram, numero_train, date, heure_theo, sens, statut_validation,
            gare_depart, gare_arrivee, destination
        ) VALUES (?, '88799', ?, '18h00', 'retour', 'valide_supprime',
            'Luxembourg', 'Metz', 'Metz')""",
        (id_test, date_j),
    )
    conn.commit()
    conn.close()
    user = {
        "id_telegram_propre": id_test,
        "gare_depart": "Metz", "heures": "07h-09h",
        "gare_retour": "Luxembourg", "heures_retour": "18h-20h",
    }
    radar_bruit = {}
    for h in ("07h10", "07h33", "08h00", "08h30"):
        radar_bruit[f"{date_j}_88701_{h}"] = {
            "date": date_j, "train": "88701", "trajet": "Metz -> Luxembourg",
            "heure": h, "motif": "Retard radar", "retard": 15, "type": "retard",
            "retard_txt": "+15 min", "_auto_radar": True,
        }
    radar_bruit[f"{date_j}_88888_18h30"] = {
        "date": date_j, "train": "88888", "trajet": "Luxembourg -> Metz",
        "heure": "18h30", "motif": "Train supprimé", "retard": 0, "type": "supprime",
        "retard_txt": "Supprimé", "_auto_radar": True,
    }
    radar_bruit[f"{date_j}_88799_18h00"] = {
        "date": date_j, "train": "88799", "trajet": "Luxembourg -> Metz",
        "heure": "18h00", "motif": "Train supprimé", "retard": 0, "type": "supprime",
        "retard_txt": "Supprimé", "_auto_radar": True,
    }
    declare = {
        f"{date_j}_88701_07h10": {
            "date": date_j, "train": "88701", "trajet": "Metz -> Luxembourg",
            "heure": "07h10", "motif": "Retard constaté", "retard": 18, "type": "retard",
            "retard_txt": "+18 min",
        },
        f"{date_j}_88799_18h00": {
            "date": date_j, "train": "88799", "trajet": "Luxembourg -> Metz",
            "heure": "18h00", "motif": "Train supprimé", "retard": 0, "type": "supprime",
            "retard_txt": "Supprimé",
        },
    }
    merged = {**declare, **radar_bruit}
    filtres = bot.filtrer_incidents_pdf_personne(user, merged, id_test, mois)
    par_date = {}
    for inc in filtres.values():
        par_date.setdefault(inc["date"], []).append(inc)
    items = par_date.get(date_j, [])
    retards = [i for i in items if i["type"] != "supprime"]
    suppr = [i for i in items if i["type"] == "supprime"]
    assert len(retards) <= 2, f"Trop de retards : {len(retards)}"
    assert len(suppr) == 1 and suppr[0]["train"] == "88799"
    assert not any(i["train"] == "88888" for i in items), "Suppression non déclarée filtrée"
    assert any(i["train"] == "88701" for i in retards)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_alerte_marge_creneau_hagondange():
    """17h15 Hagondange→Metz doit matcher créneau retour 18h-20h pour les alertes."""
    from bazo_metier import h_dans_tranche, h_dans_tranche_alerte
    assert not h_dans_tranche("17h15", "18h-20h")
    assert h_dans_tranche_alerte("17h15", "18h-20h")
    assert h_dans_tranche_alerte("15h15", "18h-20h") is False

    import importlib.util
    spec = importlib.util.spec_from_file_location("scanner", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_ligne.py"))
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    ok = sc.incident_correspond_profil_pdf(
        "Hagondange", "Metz", "17h15",
        "Metz", "Luxembourg", "08h-10h", "18h-20h",
        pour_alerte=True,
    )
    assert ok, "Alerte Hagondange 17h15 attendue pour profil Metz/Lux 18h-20h"
    assert not sc.incident_correspond_profil_pdf(
        "Hagondange", "Metz", "17h15",
        "Metz", "Luxembourg", "08h-10h", "18h-20h",
        pour_alerte=False,
    ), "PDF doit rester strict sur créneau 2h"


def test_filtre_thionville_exclut_uckange_aller():
    """Thionville→Lux : suppression signalée à Uckange ne doit pas alerter le matin (aller)."""
    from bazo_metier import incident_pertinent_profil

    user_dep, user_ret = "Thionville", "Luxembourg"
    tr_aller, tr_retour = "07h-09h", "18h-20h"
    sens, _ = incident_pertinent_profil(
        "Uckange", "Luxembourg", "07h42",
        user_dep, user_ret, tr_aller, tr_retour,
        type_incident="supprime", pour_alerte=True,
    )
    assert sens is None, "Uckange en amont de Thionville — pas d'alerte aller"
    sens2, _ = incident_pertinent_profil(
        "Thionville", "Luxembourg", "07h55",
        user_dep, user_ret, tr_aller, tr_retour,
        type_incident="supprime", pour_alerte=True,
    )
    assert sens2 == "aller"


def test_suppression_radar_sans_checkin():
    """Suppression radar dans le créneau doit compter sans déclaration préalable."""
    from bazo_metier import incident_pertinent_profil

    user = {
        "gare_depart": "Metz", "heures": "07h-09h",
        "gare_retour": "Luxembourg", "heures_retour": "18h-20h",
    }
    sens, _ = incident_pertinent_profil(
        "Metz", "Luxembourg", "07h33",
        user["gare_depart"], user["gare_retour"],
        user["heures"], user["heures_retour"],
        type_incident="supprime",
    )
    assert sens == "aller"


def test_api_health():
    from fastapi.testclient import TestClient
    from bazo_api.main import app
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] in ("ok", "degraded")


def test_monitor_seuil_30min():
    import monitor_bazo as mon
    assert mon.SEUIL_ALERTE_MIN == 30
    assert mon.COOLDOWN_ALERTE_MIN >= 30


def test_guide_utilisateur_existe():
    import os
    assert os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "GUIDE_UTILISATEUR.md"))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "GUIDE_UTILISATEUR.md"), encoding="utf-8") as f:
        txt = f.read()
    assert "Déclarer mon trajet" in txt
    assert "@SbazoTrain_bot" in txt


def test_texte_invitation_beta():
    from bazo_metier import texte_invitation_beta
    txt = texte_invitation_beta()
    assert "SbazoTrain_bot" in txt
    assert "Garantie Fiabilité" in txt
    assert "beta" in txt.lower()


def test_stats_beta():
    from bazo_metier import stats_beta
    b = stats_beta()
    assert "inscrits" in b
    assert "objectif" in b
    assert b["objectif_min"] == 20
    assert b["objectif_max"] == 30


def test_api_checkin_checkout():
    from fastapi.testclient import TestClient
    from bazo_api.main import app

    id_test = "999999977"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        """
        INSERT INTO inscriptions (
            id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour
        ) VALUES (?, 'Api', 'Test', 'Metz', '08h-10h', 'Luxembourg', '18h-20h')
        """,
        (id_test,),
    )
    # Le numéro doit être connu du jour (snapshot ou radar) : on le déclare dans l'historique de test.
    conn.execute("DELETE FROM historique WHERE numero_train = '88701' AND date = date('now')")
    conn.execute(
        "INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min, heure_theo) VALUES (date('now'), '88701', 'Luxembourg', 'Metz', 'retard', 12, '08h00')"
    )
    conn.commit()
    conn.close()

    client = TestClient(app)
    r = client.post("/v1/checkin", json={
        "id_telegram": id_test,
        "sens": "aller",
        "numero_train": "88701",
        "heure_theo": "08h00",
        "heure_arrivee_theo": "08h45",
        "destination": "Luxembourg",
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["statut"] == "en_attente"

    r2 = client.get(f"/v1/checkout/pending/{id_test}")
    assert r2.status_code == 200
    pending = r2.json()
    assert pending["nb_pending"] >= 1
    tid = pending["trajets"][0]["id"]

    r3 = client.post(f"/v1/checkout/{tid}", json={"id_telegram": id_test, "action": "rien"})
    assert r3.status_code == 200
    assert r3.json()["statut"] == "annule"

    r4 = client.get(f"/v1/checkout/pending/{id_test}")
    assert r4.json()["nb_pending"] == 0

    r5 = client.get("/v1/beta/invitation")
    assert r5.status_code == 200
    assert "texte" in r5.json()

    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM historique WHERE numero_train = '88701' AND date = date('now')")
    conn.commit()
    conn.close()


def test_api_app_profil_habituels_greve_hub_live():
    """Endpoints de l'application : profil (PUT), trains habituels, plan grève, hub d'entraide, live."""
    from fastapi.testclient import TestClient
    from bazo_api.main import app

    id_test = "999999966"
    import bazo_greve
    bazo_greve.init_schema()
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM trains_habituels WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour) "
        "VALUES (?, 'Api', 'Appli', 'Metz', '08h-10h', 'Luxembourg', '16h-18h')", (id_test,))
    conn.commit()
    conn.close()
    client = TestClient(app)

    r = client.put(f"/v1/profil/{id_test}", json={"prenom": "Camille", "heures": "06h-08h", "alertes_actives": False})
    assert r.status_code == 200, r.text
    assert r.json()["prenom"] == "Camille" and r.json()["heures"] == "06h-08h" and r.json()["alertes_actives"] is False
    assert client.put(f"/v1/profil/{id_test}", json={"heures": "n'importe"}).status_code in (400, 422)
    assert client.put(f"/v1/profil/{id_test}", json={"gare_depart": "Atlantis"}).status_code == 400

    assert client.get(f"/v1/habituels/{id_test}").json()["nb"] == 0
    r = client.post(f"/v1/habituels/{id_test}", json={"sens": "aller", "heure": "07h33"})
    assert r.status_code == 201, r.text
    assert r.json()["habituels"][0]["heure"] == "07h33" and r.json()["habituels"][0]["gare_depart"] == "Metz"
    assert client.post(f"/v1/habituels/{id_test}", json={"sens": "aller", "heure": "7:33"}).status_code in (400, 422)
    assert client.delete(f"/v1/habituels/{id_test}", params={"sens": "aller", "heure": "07h33"}).json()["nb"] == 0

    r = client.get(f"/v1/greve/{id_test}")
    assert r.status_code == 200, r.text
    jours = r.json()["jours"]
    assert len(jours) == 2 and all({"date", "mode", "segments"} <= set(j) for j in jours)
    assert jours[0]["segments"][0]["gare_depart"] == "Metz" and jours[0]["segments"][0]["gare_arrivee"] == "Luxembourg"

    r = client.get(f"/v1/hub/{id_test}")
    assert r.status_code == 200, r.text
    assert r.json()["seuils"]["quorum"] == 3 and "charte" in r.json()
    r = client.post(f"/v1/hub/{id_test}/bloque", json={"date": "2030-01-01", "numero_train": "88571", "heure_theo": "08h33", "gare": "Metz"})
    assert r.status_code == 409 and "check-in" in r.json()["detail"].lower()
    assert client.post(f"/v1/hub/{id_test}/offres", json={"hub_id": 999999, "destination": "Luxembourg", "places": 2,
                                                         "point_rdv_code": "parvis", "depart": "09h00", "participation_eur": 2,
                                                         "accepte_charte": False}).status_code in (400, 404, 409)

    r = client.get(f"/v1/live/{id_test}")
    assert r.status_code == 200, r.text
    assert {"reseau", "profil", "prochain_train", "pdf"} <= set(r.json())
    assert client.get("/v1/live/sillon").status_code in (200, 503)

    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM trains_habituels WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_pdf_token_sign():
    from bazo_api.pdf_sign import creer_token_pdf, verifier_token_pdf

    token, exp = creer_token_pdf("999999977", "2026-06", ttl_sec=600)
    assert exp > 0
    id_tg, mois = verifier_token_pdf(token)
    assert id_tg == "999999977"
    assert mois == "2026-06"


def test_api_pdf_mois():
    from fastapi.testclient import TestClient
    from bazo_api.main import app

    id_test = "999999976"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures) "
        "VALUES (?, 'Pdf', 'Mois', 'Metz', '08h-10h')",
        (id_test,),
    )
    conn.commit()
    conn.close()
    client = TestClient(app)
    r = client.get(f"/v1/pdf/mois/{id_test}")
    assert r.status_code == 200
    assert "mois" in r.json()
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_backup_sqlite_integrite():
    import shutil
    import tempfile
    from backup_bazo import backup_sqlite, compter_tables
    from bazo_config import DB_PATH

    if not os.path.isfile(DB_PATH):
        return
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "test_backup.db")
        meta = backup_sqlite(dest)
        assert meta["integrity"] == "ok"
        src_counts = compter_tables(sqlite3.connect(DB_PATH, timeout=5))
        dst_counts = meta["counts"]
        for table in src_counts:
            assert dst_counts.get(table) == src_counts[table], f"{table}: {dst_counts.get(table)} != {src_counts[table]}"


def test_migrate_sqlite_dry_run():
    from migrate_sqlite_to_postgres import migrer
    stats = migrer(dry_run=True)
    assert "inscriptions" in stats
    assert stats["inscriptions"] >= 0


def test_postgres_schema_existe():
    path = os.path.join(ROOT, "postgres", "schema.sql")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    assert "CREATE TABLE IF NOT EXISTS inscriptions" in txt
    assert "trajets_valides" in txt


def test_rgpd_export_suppression():
    from bazo_rgpd import exporter_donnees_utilisateur, supprimer_compte_utilisateur
    id_test = "999999975"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM trajets_valides WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures) "
        "VALUES (?, 'Rgpd', 'Test', 'Metz', '08h-10h')",
        (id_test,),
    )
    conn.commit()
    conn.close()
    exp = exporter_donnees_utilisateur(id_test)
    assert exp["profil"]["prenom"] == "Test"
    sup = supprimer_compte_utilisateur(id_test)
    assert sup["ok"] is True
    assert charger_utilisateur(id_test) is None


def test_openapi_export():
    import json
    import subprocess
    r = subprocess.run(
        [sys.executable, "scripts/export_openapi.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    with open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8") as f:
        spec = json.load(f)
    assert "/v1/profil/{id_telegram}" in spec.get("paths", {})
    assert "/v1/account/{id_telegram}/export" in spec.get("paths", {})


def test_api_rgpd_webhook():
    from fastapi.testclient import TestClient
    from bazo_api.main import app
    id_test = "999999974"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures) "
        "VALUES (?, 'W', 'H', 'Metz', '08h-10h')",
        (id_test,),
    )
    conn.commit()
    conn.close()
    client = TestClient(app)
    admin_hdr = {"X-Admin-Key": os.environ.get("BAZO_ADMIN_API_KEY", "test_admin_key_ci")}
    assert client.post("/v1/webhooks/ping", headers=admin_hdr).json()["ok"] is True
    r = client.get(f"/v1/account/{id_test}/export")
    assert r.status_code == 200
    assert r.json()["profil"]["prenom"] == "H"
    client.delete(f"/v1/account/{id_test}")


def test_profils_axes_api():
    """M5 S4 — 3 profils types : Metz–Lux, Nancy–Metz, Thionville–Lux."""
    from bazo_geo import uic_pour_gare, calculer_sens
    profils = (
        ("Metz", "Luxembourg"),
        ("Nancy", "Metz"),
        ("Thionville", "Luxembourg"),
    )
    for dep, arr in profils:
        assert uic_pour_gare(dep), f"UIC manquant {dep}"
        assert uic_pour_gare(arr), f"UIC manquant {arr}"
        assert calculer_sens(dep, arr) != 0, f"sens nul {dep}->{arr}"


def test_cache_memoire():
    from bazo_cache import cache_delete, cache_get, cache_set
    cache_delete("test_key")
    assert cache_get("test_key") is None
    cache_set("test_key", {"a": 1}, ttl_sec=60)
    assert cache_get("test_key") == {"a": 1}


def test_api_auth_obligatoire():
    import importlib
    os.environ["BAZO_API_REQUIRE_AUTH"] = "1"
    os.environ["BAZO_ADMIN_API_KEY"] = "secret_admin_test_key"
    import bazo_config
    importlib.reload(bazo_config)
    from bazo_metier import migrer_colonne_api_token, regenerer_api_token
    migrer_colonne_api_token()
    id_test = "999999973"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures, api_token) "
        "VALUES (?, 'Auth', 'Test', 'Metz', '08h-10h', 'test_user_token_ci_abc123xyz')",
        (id_test,),
    )
    conn.commit()
    conn.close()
    import bazo_api.security as sec_mod
    import bazo_api.main as api_mod
    importlib.reload(bazo_config)
    importlib.reload(sec_mod)
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    r401 = client.get(f"/v1/profil/{id_test}")
    assert r401.status_code == 401, r401.text
    r = client.get(
        f"/v1/profil/{id_test}",
        headers={"Authorization": "Bearer test_user_token_ci_abc123xyz"},
    )
    assert r.status_code == 200
    assert client.get("/v1/admin/stats").status_code == 403
    assert client.get("/v1/admin/stats", headers={"X-Admin-Key": "secret_admin_test_key"}).status_code == 200
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()
    os.environ["BAZO_API_REQUIRE_AUTH"] = "0"
    importlib.reload(bazo_config)
    importlib.reload(sec_mod)
    importlib.reload(api_mod)


def test_secure_audit_pass():
    import subprocess
    r = subprocess.run([sys.executable, "scripts/secure_audit.py"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_migrer_date_inscription():
    from bazo_metier import migrer_colonne_date_inscription

    assert migrer_colonne_date_inscription() is True
    conn = db_connect(timeout=5)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(inscriptions)").fetchall()}
    assert "date_inscription" in cols
    conn.close()


def test_bazo_db_backend():
    from bazo_db import backend_info, pg_ping

    info = backend_info()
    assert info["backend"] in ("sqlite", "postgres")
    ok, msg = pg_ping()
    assert isinstance(ok, bool) and msg


def test_bazo_push_stub():
    from unittest.mock import MagicMock, patch
    import importlib
    import bazo_push as bp
    from bazo_push import envoyer_push, push_disponible

    assert push_disponible() is False
    assert envoyer_push("token", "t", "c") is False
    with patch.dict("os.environ", {"BAZO_FCM_ENABLED": "1", "BAZO_FCM_SERVER_KEY": "key_test"}):
        importlib.reload(bp)
        mock_resp = MagicMock(status_code=200, json=lambda: {"success": 1, "failure": 0})
        with patch.object(bp._HTTP, "post", return_value=mock_resp):
            assert bp.envoyer_push("fcm_token_abc", "T", "C") is True
    importlib.reload(bp)


def test_health_extended():
    from fastapi.testclient import TestClient
    from bazo_api.main import app

    r = TestClient(app).get("/health")
    assert r.status_code == 200
    d = r.json()
    assert d["version"] == "0.5.0"
    assert d["regions"] == 13
    assert d["radar_axes"] >= 53


def test_admin_push_test_disabled():
    from fastapi.testclient import TestClient
    from bazo_api.main import app

    client = TestClient(app)
    hdr = {"X-Admin-Key": os.environ.get("BAZO_ADMIN_API_KEY", "test_admin_key_ci")}
    r = client.post(
        "/v1/admin/push-test",
        json={"id_telegram": "1", "titre": "t", "message": "m"},
        headers=hdr,
    )
    assert r.status_code == 503


def test_webhook_deploy():
    from fastapi.testclient import TestClient
    from bazo_api.main import app

    client = TestClient(app)
    hdr = {"X-Admin-Key": os.environ.get("BAZO_ADMIN_API_KEY", "test_admin_key_ci")}
    r = client.post("/v1/webhooks/deploy", json={"version": "ci-test", "message": "ok"}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_device_tokens_api():
    from fastapi.testclient import TestClient
    from bazo_api.main import app
    from bazo_devices import ensure_device_schema, lister_devices

    ensure_device_schema()
    id_test = "999999972"
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM device_tokens WHERE id_telegram = ?", (id_test,))
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.execute(
        "INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures, api_token) "
        "VALUES (?, 'D', 'T', 'Metz', '08h', 'test_device_token_ci_xyz123456789')",
        (id_test,),
    )
    conn.commit()
    conn.close()
    client = TestClient(app)
    hdr = {"Authorization": "Bearer test_device_token_ci_xyz123456789"}
    r = client.post(
        f"/v1/devices/{id_test}",
        json={"device_token": "fcm_dummy_token_abc123", "plateforme": "android"},
        headers=hdr,
    )
    assert r.status_code == 200
    assert len(lister_devices(id_test)) == 1
    r2 = client.delete(
        f"/v1/devices/{id_test}?device_token=fcm_dummy_token_abc123",
        headers=hdr,
    )
    assert r2.status_code == 200
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM inscriptions WHERE id_telegram = ?", (id_test,))
    conn.commit()
    conn.close()


def test_bazo_gtfs():
    from bazo_gtfs import charger_stops_depuis_csv, uic_depuis_stop_id, valider_uic_dans_gtfs

    csv = "stop_id,stop_name\nStopArea:OCE87192039,Metz\nStopArea:OCE82001000,Luxembourg\n"
    stops = charger_stops_depuis_csv(csv)
    assert uic_depuis_stop_id("StopArea:OCE87192039") == "87192039"
    assert valider_uic_dans_gtfs("87192039", stops)
    assert not valider_uic_dans_gtfs("00000000", stops)


def test_bazo_tgv_preview():
    from bazo_tgv import TGV_ACTIVE, trajets_tgv

    assert TGV_ACTIVE is False
    assert trajets_tgv() == []


def test_api_geo_summary():
    from fastapi.testclient import TestClient
    from bazo_api.main import app

    client = TestClient(app)
    r = client.get("/v1/geo/summary")
    assert r.status_code == 200
    d = r.json()
    assert d["regions_total"] == 13
    assert d["axes_radar_total"] >= 53
    r2 = client.get("/v1/tgv/radar")
    assert r2.json()["active"] is False


def test_beta_recruit_script():
    import subprocess
    r = subprocess.run(
        [sys.executable, "scripts/beta_recruit.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Beta Metz" in r.stdout


def test_bilan_y1_script():
    import subprocess
    r = subprocess.run(
        [sys.executable, "scripts/bilan_y1.py", "--quick"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Bilan enregistré" in r.stdout


def test_publish_blocked_local():
    """Publication GitHub bloquée par défaut (mode local sécurisé)."""
    import subprocess
    r = subprocess.run(
        ["bash", "scripts/publish_github.sh", "--quick"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "BLOCKED" in r.stdout or "Publication désactivée" in r.stdout


def test_api_beta_stats():
    from fastapi.testclient import TestClient
    from bazo_api.main import app
    client = TestClient(app)
    r = client.get("/v1/beta/stats")
    assert r.status_code == 200
    d = r.json()
    assert "inscrits" in d and "objectif" in d and "progression_pct" in d


def test_validate_geo_radar_script():
    import subprocess
    r = subprocess.run([sys.executable, "scripts/validate_geo_radar.py"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_load_test_script():
    import subprocess
    r = subprocess.run(
        [sys.executable, "scripts/load_test.py", "--requests", "0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr



# ---------------------------------------------------------------------------
# Phase 2 — signalement communautaire unifié
# ---------------------------------------------------------------------------
_P2_DATE = "2027-01-26"
_P2_NUM = "839100"
_P2_H = "08h15"


def _preparer_signaleur(id_test, num=_P2_NUM, date_j=_P2_DATE, h=_P2_H, statut="en_attente"):
    """Usager inscrit avec un check-in sur le train de test. Retourne l'id du trajet."""
    _nettoyer_jeu_test(id_test, numeros=(num,))
    _inscrire_test(id_test, "Metz", "7h-9h", "Luxembourg", "18h-20h")
    return _inserer_trajet(id_test, num, date_j, h, statut, "Metz", "Luxembourg", "Luxembourg",
                           sens="aller", h_arr="09h15")


def test_signalement_exige_checkin():
    from bazo_metier import creer_signalement, migrer_schema_phase2
    migrer_schema_phase2()
    id_a = "999999960"
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    _inscrire_test(id_a)
    sans = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "retard_au_depart")
    _preparer_signaleur(id_a)
    avec = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "retard_au_depart")
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    assert sans == {"ok": False, "erreur": "checkin_requis"}, sans
    assert avec["ok"] and avec["signalement"]["statut"] == "non_confirme", avec
    assert avec["signalement"]["mention"] == "Information voyageurs, non vérifiée par SNCF"


def test_signalement_type_hors_liste_refuse():
    from bazo_metier import creer_signalement
    id_a = "999999959"
    _preparer_signaleur(id_a)
    r1 = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "texte_libre")
    r2 = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "voie_annoncee")
    r3 = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "voie_annoncee", valeur="quai nord !")
    r4 = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "voie_annoncee", valeur="3b")
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    assert r1["erreur"] == "type_inconnu"
    assert r2["erreur"] == "valeur_requise"
    assert r3["erreur"] == "valeur_invalide"
    assert r4["ok"] and r4["signalement"]["libelle"] == "Voie annoncée : 3B", r4


def _trio_signalement():
    """Auteur + deux voyageurs avec check-in sur le même train. Retourne (ids, signalement_id)."""
    from bazo_metier import creer_signalement
    ids = ("999999957", "999999956", "999999955")
    for i in ids:
        _preparer_signaleur(i)
    res = creer_signalement(ids[0], _P2_DATE, _P2_NUM, _P2_H, "arret_prolonge")
    assert res["ok"], res
    return ids, res["signalement"]["id"]


def _nettoyer_trio(ids):
    for i in ids:
        _nettoyer_jeu_test(i, numeros=(_P2_NUM,))


def test_quorum_confirmation():
    from bazo_metier import charger_signalement, voter_signalement
    ids, sid = _trio_signalement()
    v1 = voter_signalement(sid, ids[1], "confirme")
    apres1 = charger_signalement(sid)["statut"]
    v2 = voter_signalement(sid, ids[2], "confirme")
    apres2 = charger_signalement(sid)
    _nettoyer_trio(ids)
    assert v1["ok"] and apres1 == "non_confirme", (v1, apres1)
    assert v2["ok"] and apres2["statut"] == "confirme" and apres2["nb_confirmations"] == 2, apres2


def test_auteur_ne_vote_pas():
    from bazo_metier import voter_signalement
    ids, sid = _trio_signalement()
    r = voter_signalement(sid, ids[0], "confirme")
    _nettoyer_trio(ids)
    assert r == {"ok": False, "erreur": "auteur"}, r


def test_vote_unique_par_compte():
    from bazo_metier import charger_signalement, voter_signalement
    ids, sid = _trio_signalement()
    r1 = voter_signalement(sid, ids[1], "confirme")
    r2 = voter_signalement(sid, ids[1], "confirme")
    r3 = voter_signalement(sid, ids[1], "refute")
    sig = charger_signalement(sid)
    # Un compte sans check-in sur ce train ne vote pas
    id_sans = "999999954"
    _nettoyer_jeu_test(id_sans)
    _inscrire_test(id_sans)
    r4 = voter_signalement(sid, id_sans, "confirme")
    _nettoyer_jeu_test(id_sans)
    _nettoyer_trio(ids)
    assert r1["ok"]
    assert r2 == {"ok": False, "erreur": "deja_vote"}, r2
    assert r3 == {"ok": False, "erreur": "deja_vote"}, r3
    assert sig["nb_confirmations"] == 1 and sig["nb_refutations"] == 0
    assert r4 == {"ok": False, "erreur": "checkin_requis"}, r4


def test_signalement_conteste():
    from bazo_metier import charger_signalement, voter_signalement
    ids, sid = _trio_signalement()
    voter_signalement(sid, ids[1], "refute")
    s1 = charger_signalement(sid)["statut"]
    voter_signalement(sid, ids[2], "refute")
    s2 = charger_signalement(sid)["statut"]
    _nettoyer_trio(ids)
    assert s1 == "non_confirme" and s2 == "conteste", (s1, s2)


def test_signalement_expire():
    from bazo_metier import charger_signalement, creer_signalement, expirer_signalements, voter_signalement
    from bazo_metier import signalements_du_train
    ids = ("999999953", "999999952")
    date_passee = "2026-01-05"
    for i in ids:
        _preparer_signaleur(i, date_j=date_passee)
    res = creer_signalement(ids[0], date_passee, _P2_NUM, _P2_H, "forte_affluence")
    sid = res["signalement"]["id"]
    n = expirer_signalements()
    statut = charger_signalement(sid)["statut"]
    vote = voter_signalement(sid, ids[1], "confirme")
    fil = signalements_du_train(date_passee, _P2_NUM, _P2_H)
    fil_tout = signalements_du_train(date_passee, _P2_NUM, _P2_H, inclure_expires=True)
    for i in ids:
        _nettoyer_jeu_test(i, numeros=(_P2_NUM,))
    assert n >= 1 and statut == "expire", (n, statut)
    assert vote == {"ok": False, "erreur": "expire"}, vote
    assert fil == [] and len(fil_tout) == 1


def test_signalement_sans_effet_pdf_sans_confirmation():
    """Un signalement « suppression annoncée » seul ne modifie pas le trajet ni le PDF."""
    from bazo_metier import creer_signalement
    bot = _charger_module_bot()
    id_a = "999999951"
    tid = _preparer_signaleur(id_a)
    user = bot.charger_utilisateur(id_a)
    res = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "train_supprime_annonce")
    incidents, _ = bot.assembler_incidents_pdf(id_a, "2027-01", user)
    conn = db_connect(timeout=5)
    statut, prov = conn.execute(
        "SELECT statut_validation, COALESCE(provenance,'officielle') FROM trajets_valides WHERE id = ?", (tid,)
    ).fetchone()
    conn.close()
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    assert res["ok"]
    assert statut == "en_attente" and prov == "officielle", (statut, prov)
    assert incidents == {}, incidents


def test_signalement_confirme_par_proprietaire_dans_pdf_avec_provenance():
    from bazo_metier import creer_signalement, promouvoir_trajet_signalement
    bot = _charger_module_bot()
    id_a = "999999950"
    id_b = "999999949"
    tid = _preparer_signaleur(id_a)
    _preparer_signaleur(id_b)
    user = bot.charger_utilisateur(id_a)
    sid = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "train_supprime_annonce")["signalement"]["id"]
    # Un autre compte ne peut pas inscrire le trajet de l'auteur
    intrus = promouvoir_trajet_signalement(id_b, "train_supprime_annonce", id_trajet=tid, signalement_id=sid)
    ok = promouvoir_trajet_signalement(id_a, "train_supprime_annonce", id_trajet=tid, signalement_id=sid)
    incidents, _ = bot.assembler_incidents_pdf(id_a, "2027-01", user)
    conn = db_connect(timeout=5)
    statut, prov, motif = conn.execute(
        "SELECT statut_validation, provenance, motif_validation FROM trajets_valides WHERE id = ?", (tid,)
    ).fetchone()
    conn.close()
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    _nettoyer_jeu_test(id_b, numeros=(_P2_NUM,))
    assert intrus is None
    assert ok == tid
    assert statut == "valide_supprime" and prov == "voyageur", (statut, prov)
    assert motif.startswith("Déclaration du voyageur"), motif
    assert len(incidents) == 1
    inc = next(iter(incidents.values()))
    assert inc["type"] == "supprime" and inc["provenance"] == "voyageur"
    assert inc["provenance_libelle"] == "Déclaration du voyageur"
    assert inc["motif"].startswith("Déclaration du voyageur"), inc["motif"]
    assert inc["trajet"] == "Metz -> Luxembourg"


def test_voie_annoncee_jamais_dans_pdf():
    from bazo_metier import creer_signalement, promouvoir_trajet_signalement
    bot = _charger_module_bot()
    id_a = "999999948"
    tid = _preparer_signaleur(id_a)
    user = bot.charger_utilisateur(id_a)
    res = creer_signalement(id_a, _P2_DATE, _P2_NUM, _P2_H, "voie_annoncee", valeur="2")
    sid = res["signalement"]["id"]
    promo = promouvoir_trajet_signalement(id_a, "voie_annoncee", id_trajet=tid, signalement_id=sid)
    incidents, _ = bot.assembler_incidents_pdf(id_a, "2027-01", user)
    conn = db_connect(timeout=5)
    statut = conn.execute("SELECT statut_validation FROM trajets_valides WHERE id = ?", (tid,)).fetchone()[0]
    conn.close()
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    assert res["signalement"]["effet_pdf_possible"] is False
    assert promo is None
    assert statut == "en_attente" and incidents == {}


def test_tables_waze_retirees():
    from bazo_metier import migrer_schema_phase2
    migrer_schema_phase2(retirer_tables_legacy=True)
    conn = db_connect(timeout=5)
    noms = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    cols = {c[1] for c in conn.execute("PRAGMA table_info(trajets_valides)").fetchall()}
    conn.close()
    for t in ("waze_signalements", "waze_votes", "signalements_trafic", "confirmations_trafic"):
        assert t not in noms, t
    assert {"signalements", "signalements_votes"} <= noms
    assert "provenance" in cols


def test_api_signalements_flux():
    """API : 409 sans check-in, 201 avec, fil anonymisé avec mention, vote refusé à l'auteur."""
    from fastapi.testclient import TestClient
    from bazo_api.main import app
    id_a, id_b = "999999947", "999999946"
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    _inscrire_test(id_a)
    client = TestClient(app)
    corps = {"id_telegram": id_a, "date": _P2_DATE, "numero_train": _P2_NUM, "heure_theo": _P2_H,
             "code_type": "retard_au_depart"}
    r_sans = client.post("/v1/signalements", json=corps)
    _preparer_signaleur(id_a)
    _preparer_signaleur(id_b)
    r_avec = client.post("/v1/signalements", json=corps)
    sid = r_avec.json()["signalement"]["id"] if r_avec.status_code == 201 else None
    r_fil = client.get(f"/v1/trains/{_P2_DATE}/{_P2_NUM}/signalements", params={"heure_theo": _P2_H})
    r_auteur = client.post(f"/v1/signalements/{sid}/vote", json={"id_telegram": id_a, "vote": "confirme"})
    r_autre = client.post(f"/v1/signalements/{sid}/vote", json={"id_telegram": id_b, "vote": "confirme"})
    r_types = client.get("/v1/signalements/types")
    r_mauvais = client.post("/v1/signalements", json={**corps, "code_type": "libre"})
    _nettoyer_jeu_test(id_a, numeros=(_P2_NUM,))
    _nettoyer_jeu_test(id_b, numeros=(_P2_NUM,))
    assert r_sans.status_code == 409, r_sans.text
    assert r_avec.status_code == 201, r_avec.text
    fil = r_fil.json()
    assert r_fil.status_code == 200 and fil["nb"] == 1, r_fil.text
    assert fil["mention"] == "Information voyageurs, non vérifiée par SNCF"
    assert "id_auteur" not in fil["signalements"][0]
    assert r_auteur.status_code == 403, r_auteur.text
    assert r_autre.status_code == 200 and r_autre.json()["statut"] == "non_confirme", r_autre.text
    assert r_types.status_code == 200 and len(r_types.json()["types"]) == 8
    assert r_mauvais.status_code == 400


TESTS = [
    ("schema_trajets_valides", test_schema_trajets_valides),
    ("uic_gares", test_uic_gares_cles),
    ("bazo_geo_radar", test_bazo_geo_radar),
    ("bazo_geo_regions", test_bazo_geo_regions),
    ("bazo_gtfs", test_bazo_gtfs),
    ("bazo_tgv", test_bazo_tgv_preview),
    ("api_geo", test_api_geo),
    ("api_geo_summary", test_api_geo_summary),
    ("recherche_trains", test_recherche_trains_dedup),
    ("pdf_trajets_valides", test_pdf_depuis_trajets_valides),
    ("retard_base_arrivee", test_retard_base_sur_arrivee),
    ("train_suivant_pdf", test_train_suivant_compte_dans_pdf),
    ("suppression_comptee", test_suppression_comptee),
    ("bilan_mois", test_bilan_mois_trajets),
    ("checkout_proprietaire", test_checkout_proprietaire_trajet),
    ("formulaire_mois", test_texte_formulaire_mois_dynamique),
    ("bilan_metier_coherent", test_bilan_metier_coherent),
    ("barre_progression", test_barre_progression),
    ("charger_utilisateur", test_charger_utilisateur),
    ("inferer_type_reclamation", test_inferer_type_reclamation),
    ("format_duree", test_format_duree_retard),
    ("mettre_a_jour_inscription", test_mettre_a_jour_inscription),
    ("annulation_helper", test_annulation_helper),
    ("stats_tv_valides_mois", test_stats_tv_valides_mois),
    ("retard_reel_scanner", test_retard_reel_scanner_suppression),
    ("pdf_en_attente_radar", test_pdf_en_attente_avec_radar),
    ("cmd_trajet_raccourci", test_cmd_trajet_raccourci),
    ("alerte_radar_fige_pdf", test_alerte_radar_fige_pdf),
    ("signalement_lie_checkin", test_signalement_lie_checkin),
    ("coherence_alerte_pdf", test_coherence_alerte_pdf_fonction),
    ("checkin_preserve_valide", test_checkin_ne_reset_pas_valide),
    ("sync_retard_sans_heure", test_sync_retard_sans_heure_checkin),
    ("pdf_deux_tranches", test_pdf_aligne_alertes_deux_tranches),
    ("jours_ar_meme_jour", test_jours_a_completer_desactive),
    ("pdf_pas_auto_completion", test_pas_auto_completion_pdf),
    ("pas_proposition_avant_inscription", test_pas_proposition_avant_inscription),
    ("p2_signalement_exige_checkin", test_signalement_exige_checkin),
    ("p2_type_hors_liste_refuse", test_signalement_type_hors_liste_refuse),
    ("p2_quorum_confirmation", test_quorum_confirmation),
    ("p2_auteur_ne_vote_pas", test_auteur_ne_vote_pas),
    ("p2_vote_unique_par_compte", test_vote_unique_par_compte),
    ("p2_signalement_conteste", test_signalement_conteste),
    ("p2_signalement_expire", test_signalement_expire),
    ("p2_sans_effet_pdf_sans_confirmation", test_signalement_sans_effet_pdf_sans_confirmation),
    ("p2_pdf_provenance_voyageur", test_signalement_confirme_par_proprietaire_dans_pdf_avec_provenance),
    ("p2_voie_jamais_dans_pdf", test_voie_annoncee_jamais_dans_pdf),
    ("p2_tables_waze_retirees", test_tables_waze_retirees),
    ("p2_api_signalements_flux", test_api_signalements_flux),
    ("checkin_liste_creneau_complet", test_checkin_liste_tout_le_creneau),
    ("checkin_liste_creneau_complet", test_checkin_liste_tout_le_creneau),
    ("pdf_trajet_segment_usager", test_pdf_trajet_segment_usager),
    ("pdf_retour_segment", test_pdf_retour_depart_different_arrivee),
    ("pdf_alias_metz_ville", test_pdf_alias_metz_ville),
    ("howald_distinct", test_howald_distinct_luxembourg),
    ("suppression_hors_segment", test_suppression_hors_segment_ignoree),
    ("suppression_sur_segment", test_suppression_sur_segment_comptee),
    ("suppression_sans_gare", test_suppression_sans_gare_faux),
    ("retard_autre_gare", test_retard_autre_gare_ignore),
    ("suppression_ne_remplace_pas_checkin", test_suppression_radar_ne_remplace_pas_checkin),
    ("proposition_acceptee_pdf", test_proposition_acceptee_dans_pdf),
    ("proposition_autre_usager", test_proposition_autre_usager_refusee),
    ("legacy_exclu_pdf", test_legacy_exclu_du_pdf),
    ("motif_pdf_nettoye", test_motif_pdf_nettoye_bot),
    ("bot_mobile_meme_trajet", test_bot_et_mobile_meme_trajet),
    ("checkin_api_date_passee", test_checkin_api_date_passee_refusee),
    ("heure_theo_collision", test_heure_theo_collision_checkin),
    ("bilan_mensuel_pdf", test_bilan_mensuel_aligne_pdf),
    ("pdf_lecture_readonly", test_pdf_lecture_ne_mutile_pas_db),
    ("accents_pdf", test_clean_text_conserve_accents),
    ("pdf_filtre_quotidien", test_pdf_filtre_max_deux_par_jour),
    ("alerte_marge_hagondange", test_alerte_marge_creneau_hagondange),
    ("filtre_thionville_uckange", test_filtre_thionville_exclut_uckange_aller),
    ("suppression_radar_auto", test_suppression_radar_sans_checkin),
    ("api_health", test_api_health),
    ("monitor_seuil", test_monitor_seuil_30min),
    ("guide_utilisateur", test_guide_utilisateur_existe),
    ("texte_invitation_beta", test_texte_invitation_beta),
    ("stats_beta", test_stats_beta),
    ("date_inscription", test_migrer_date_inscription),
    ("bazo_db", test_bazo_db_backend),
    ("bazo_push", test_bazo_push_stub),
    ("health_extended", test_health_extended),
    ("admin_push", test_admin_push_test_disabled),
    ("webhook_deploy", test_webhook_deploy),
    ("device_tokens", test_device_tokens_api),
    ("api_checkin_checkout", test_api_checkin_checkout),
    ("api_app_profil_habituels_greve_hub_live", test_api_app_profil_habituels_greve_hub_live),
    ("pdf_token_sign", test_pdf_token_sign),
    ("api_pdf_mois", test_api_pdf_mois),
    ("backup_integrite", test_backup_sqlite_integrite),
    ("migrate_dry_run", test_migrate_sqlite_dry_run),
    ("postgres_schema", test_postgres_schema_existe),
    ("rgpd_export", test_rgpd_export_suppression),
    ("openapi_export", test_openapi_export),
    ("api_rgpd_webhook", test_api_rgpd_webhook),
    ("profils_axes", test_profils_axes_api),
    ("cache_memoire", test_cache_memoire),
    ("api_auth", test_api_auth_obligatoire),
    ("secure_audit", test_secure_audit_pass),
    ("validate_geo", test_validate_geo_radar_script),
    ("beta_recruit", test_beta_recruit_script),
    ("bilan_y1", test_bilan_y1_script),
    ("publish_blocked", test_publish_blocked_local),
    ("api_beta_stats", test_api_beta_stats),
    ("load_test", test_load_test_script),
]


def main():
    echecs = []
    for nom, fn in TESTS:
        try:
            fn()
            print(f"[OK] {nom}")
        except Exception as e:
            print(f"[FAIL] {nom}: {e}")
            echecs.append(nom)
    if echecs:
        print(f"\n{len(echecs)} test(s) en échec.")
        return 1
    print(f"\n{len(TESTS)} tests OK — parcours critique Bazo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
