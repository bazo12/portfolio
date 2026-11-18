"""Logique métier partagée Bazo — seuils, bilans, préférences utilisateur."""
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bazo_config import DB_PATH, db_connect

tz_paris = ZoneInfo("Europe/Paris")

SEUIL_RETARD_MIN = 10
SEUIL_GF_RETARD_MIN = 120
SEUIL_GF_SUPPR = 3
MOTIF_RUPTURE_CHARGE = "Rupture de charge / Bus de substitution"

MOIS_FR = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]

CHAMPS_INSCRIPTION = frozenset({
    "nom", "prenom", "age", "gare_depart", "heures", "gare_retour", "heures_retour",
    "jours_actifs", "alertes_actives", "vacances_jusquau", "api_token",
})

CHAMPS_PROFIL_AUTORISES = {
    "mod_prenom": "prenom",
    "mod_nom": "nom",
    "mod_age": "age",
    "prenom": "prenom",
    "nom": "nom",
    "age": "age",
}


def format_duree_retard(minutes):
    """Retard lisible : '1h25' ou '18 min'."""
    m = int(minutes or 0)
    if m >= 60:
        return f"{m // 60}h{m % 60:02d}"
    return f"{m} min"


def profil_gares_configure(user):
    if not user:
        return False
    g1 = str(user.get("gare_depart") or "").strip()
    g2 = str(user.get("gare_retour") or "").strip()
    return g1 not in ("", "Non precise") and g2 not in ("", "Non precise")


def inferer_type_reclamation(user):
    if not user:
        return "abo"
    age = (user.get("age") or "").lower()
    if any(x in age for x in ("billet", "g30", "unité", "unite", "à l'unité", "a l'unite")):
        return "billet"
    return "abo"


def mettre_a_jour_inscription(id_telegram, **champs):
    """UPDATE ciblé — évite de réécrire toute la table inscriptions."""
    updates = {k: v for k, v in champs.items() if k in CHAMPS_INSCRIPTION}
    if not updates:
        return False
    try:
        conn = db_connect(timeout=10)
        cols = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [str(id_telegram)]
        conn.execute(f"UPDATE inscriptions SET {cols} WHERE id_telegram = ?", vals)
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def generer_api_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)


def migrer_colonne_api_token():
    """Ajoute api_token aux inscriptions + backfill."""
    try:
        conn = db_connect(timeout=10)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(inscriptions)").fetchall()}
        if "api_token" not in cols:
            conn.execute("ALTER TABLE inscriptions ADD COLUMN api_token TEXT")
            conn.commit()
        rows = conn.execute(
            "SELECT id_telegram FROM inscriptions WHERE api_token IS NULL OR api_token = ''"
        ).fetchall()
        for (id_tg,) in rows:
            conn.execute(
                "UPDATE inscriptions SET api_token = ? WHERE id_telegram = ?",
                (generer_api_token(), str(id_tg)),
            )
        conn.commit()
        conn.close()
        return len(rows)
    except Exception:
        return 0


def migrer_colonne_date_inscription():
    """Colonne date_inscription pour funnel beta."""
    try:
        conn = db_connect(timeout=10)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(inscriptions)").fetchall()}
        if "date_inscription" not in cols:
            conn.execute("ALTER TABLE inscriptions ADD COLUMN date_inscription TEXT")
            conn.commit()
        conn.execute(
            """
            UPDATE inscriptions SET date_inscription = datetime('now')
            WHERE date_inscription IS NULL OR date_inscription = ''
            """
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def verifier_api_token(token: str) -> str | None:
    if not token:
        return None
    try:
        conn = db_connect(timeout=5)
        row = conn.execute(
            "SELECT id_telegram FROM inscriptions WHERE api_token = ?", (str(token).strip(),)
        ).fetchone()
        conn.close()
        return str(row[0]) if row else None
    except Exception:
        return None


def regenerer_api_token(id_telegram: str) -> str | None:
    token = generer_api_token()
    if mettre_a_jour_inscription(id_telegram, api_token=token):
        return token
    return None


def creer_inscription_si_absent(id_telegram, nom="Voyageur", prenom="Voyageur"):
    """Crée un profil minimal si l'utilisateur n'existe pas encore."""
    try:
        conn = db_connect(timeout=10)
        ex = conn.execute(
            "SELECT 1 FROM inscriptions WHERE id_telegram = ?", (str(id_telegram),)
        ).fetchone()
        if not ex:
            now = datetime.now(tz_paris).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO inscriptions (
                    id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour,
                    age, jours_actifs, alertes_actives, api_token, date_inscription
                ) VALUES (?, ?, ?, 'Non precise', 'Non precise', 'Non precise', 'Non precise',
                    'Non précisé', '0,1,2,3,4', 1, ?, ?)""",
                (str(id_telegram), nom, prenom, generer_api_token(), now),
            )
            conn.commit()
        conn.close()
        from bazo_cache import cache_delete

        cache_delete("beta:stats")
        return True
    except Exception:
        return False


def train_sur_axes_ter(numero_train):
    """Filtre numéros TER locaux (exclut OCESN / nationaux parasites)."""
    n = str(numero_train or "").strip()
    if not n or n.startswith("OCESN"):
        return False
    return n.isdigit() and len(n) >= 5


# --- Géographie TER (bazo_geo — M2 S3) ---
from bazo_geo import (
    AXES,
    DEFAULT_REGION,
    TOUTES_LES_GARES,
    calculer_sens,
    get_index_gare,
    gares_equivalentes,
    gare_sur_segment,
    incident_sur_segment,
    index_gare_sur_liste,
    indices_sur_axe,
    nom_canonique_gare,
    nom_pour_uic,
    normaliser_cle_gare,
    rechercher_gares,
    stop_area_pour_gare,
    tous_uic_surveilles,
    terminus_corridor,
    trajets_radar,
    trajets_radar_multi,
    train_atteint_arrivee,
    uic_pour_gare,
)


def h_dans_tranche(heure_theo_str, tranche):
    """Créneau 2h inclusif au départ, exclusif à la fin (ex. 08h-10h → 08h00–09h59)."""
    if not tranche or tranche in ("Toute la journee", "Non precise"):
        return True
    m_train = minutes_heure_label(heure_theo_str)
    if m_train is None:
        return False
    try:
        parties = str(tranche).replace("h", "").split("-")
        m_debut = int(parties[0]) * 60
        m_fin = int(parties[1]) * 60
        return m_debut <= m_train < m_fin
    except Exception:
        return False


MARGE_CRENEAU_ALERTE_MIN = 60  # alerte jusqu'à 1 h avant le créneau (ex. 17h15 → créneau 18h-20h)


def minutes_heure_label(h_str):
    """Convertit '17h15' ou '17:15' en minutes depuis minuit."""
    try:
        s = str(h_str or "").strip().replace("h", ":")
        if ":" in s:
            hh, mm = s.split(":", 1)
            return int(hh) * 60 + int(mm[:2] or 0)
        return int(s) * 60
    except Exception:
        return None


def h_dans_tranche_alerte(heure_theo_str, tranche, marge_min=None):
    """Créneau élargi pour les alertes push (pas le PDF)."""
    if not tranche or tranche in ("Toute la journee", "Non precise"):
        return True
    marge = MARGE_CRENEAU_ALERTE_MIN if marge_min is None else marge_min
    try:
        m_train = minutes_heure_label(heure_theo_str)
        if m_train is None:
            return False
        parties = str(tranche).replace("h", "").split("-")
        m_debut = int(parties[0]) * 60 - marge
        m_fin = int(parties[1]) * 60
        return m_debut <= m_train < m_fin
    except Exception:
        return False


def incident_pertinent_profil(
    gare_incident,
    dest_incident,
    h_theo,
    gare_dep,
    gare_ret,
    tranche_aller,
    tranche_retour,
    *,
    type_incident="retard",
    pour_alerte=False,
):
    """
    Filtre unifié alerte + PDF : même axe que le profil, créneau 2h, sens cohérent.
    La direction commerciale doit être sur cet axe et aller au moins jusqu'à la
    gare d'arrivée de l'usager (un train vers Sarrebruck ou Forbach, vu à Metz,
    ne concerne pas un trajet Metz → Luxembourg).
    Suppressions : exclut les annonces en amont de la gare d'embarquement
    (ex. Uckange ignoré pour Thionville→Luxembourg le matin).
    Retourne (sens, tranche) ou (None, None).
    """
    if not gare_dep or gare_dep == "Non precise" or not gare_ret or gare_ret == "Non precise":
        return None, None
    resolved = indices_sur_axe(gare_dep, gare_ret)
    if not resolved:
        return None, None
    _axe, idx_dep, idx_ret = resolved
    idx_inc = index_gare_sur_liste(gare_incident, _axe)
    if idx_inc == -1:
        return None, None
    typ = str(type_incident or "retard").lower()
    # Suppression : même fenêtre que l'alerte, sinon le train annulé
    # est notifié puis absent du dossier (créneau strict trop court).
    tranche_ok = h_dans_tranche_alerte if (pour_alerte or typ == "supprime") else h_dans_tranche
    h = str(h_theo or "").replace("\n", " ").replace("\r", "").strip()

    def _match_sens(idx_board, idx_dest, gare_board, gare_user_arr, tranche):
        if idx_board == -1 or idx_dest == -1:
            return False
        if idx_board == idx_dest:
            if idx_inc != idx_board:
                return False
        elif idx_dest > idx_board:
            if not (idx_board <= idx_inc <= idx_dest):
                return False
            if typ == "supprime" and idx_inc < idx_board:
                return False
        else:
            if not (idx_dest <= idx_inc <= idx_board):
                return False
            if typ == "supprime" and idx_inc > idx_board:
                return False
        if not train_atteint_arrivee(dest_incident, gare_incident, gare_board, gare_user_arr, _axe):
            return False
        if not tranche or tranche in ("Non precise",):
            return False
        return tranche == "Toute la journee" or tranche_ok(h, tranche)

    if _match_sens(idx_dep, idx_ret, gare_dep, gare_ret, tranche_aller):
        return "aller", tranche_aller
    if _match_sens(idx_ret, idx_dep, gare_ret, gare_dep, tranche_retour):
        return "retour", tranche_retour
    return None, None


def train_supprime_radar_db(date_j, numero_train, gare_user=None, heure_theo=None, gare_arrivee=None):
    """
    True si le radar a enregistré une suppression de ce train, ce jour-là, à une gare
    du segment usager (règle R4). `gare_user` est la gare de départ de l'usager.
    Sans aucune gare de référence, la réponse est False : une suppression observée
    ailleurs sur la ligne ne concerne pas ce trajet.
    """
    if not date_j or not numero_train:
        return False
    if not gare_user and not gare_arrivee:
        return False
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            "SELECT gare FROM historique WHERE date=? AND numero_train=? AND type='supprime'",
            (str(date_j), str(numero_train)),
        ).fetchall()
        conn.close()
    except Exception:
        return False
    for (gare_hist,) in rows:
        if gare_sur_segment(gare_hist, gare_user, gare_arrivee):
            return True
    return False


def est_annulation(texte):
    """Annulation par bouton menu ou mot simple (pas de commande / requise)."""
    brut = (texte or "").strip()
    t = brut.lower()
    return (
        t in ("/annuler", "/cancel", "annuler", "menu", "retour", "menu principal")
        or brut in ("❌ Annuler", "🏠 Menu", "🔙 Retour")
    )


def calculer_retard_arrivee(heure_theo, heure_reelle):
    try:
        th = datetime.strptime(str(heure_theo).replace("h", ":"), "%H:%M")
        tr = datetime.strptime(str(heure_reelle).replace("h", ":"), "%H:%M")
        return max(0, int((tr - th).total_seconds() / 60))
    except Exception:
        return 0


MOTIF_ALERTE_RADAR = "Incident confirmé par alerte Bazo (radar)"
MOTIF_SIGNALEMENT_USAGER = "Signalement voyageur sur la ligne"


def _gares_similaires(a, b):
    if not a or not b:
        return False
    na = str(a).lower().replace("-", " ").strip()
    nb = str(b).lower().replace("-", " ").strip()
    return na in nb or nb in na


def _retard_historique_gare(conn, date_j, numero_train, gare_cible):
    """
    Retard max dans historique pour un train À CETTE GARE (règle R5).
    La colonne `ligne` (direction commerciale) n'est jamais utilisée : un retard observé
    à Nancy sur un train « direction Luxembourg » ne concerne pas l'arrivée à Luxembourg.
    """
    if not gare_cible:
        return 0
    rows = conn.execute(
        """
        SELECT gare, COALESCE(retard_arrivee_min, retard_depart_min, retard_min, 0)
        FROM historique
        WHERE date=? AND numero_train=? AND type='retard'
        """,
        (date_j, str(numero_train)),
    ).fetchall()
    best = 0
    for gare_hist, retard in rows:
        if gares_equivalentes(gare_hist, gare_cible):
            best = max(best, int(retard or 0))
    return best


def retard_reel_scanner_db(date_j, numero_train, gare_arrivee=None, gare_depart=None, heure_theo=None):
    """
    Retard / suppression depuis le radar, pour le segment usager (règles R4 et R5).
    Retard retenu : celui observé à la gare d'arrivée de l'usager ; à défaut, celui
    observé à sa gare de départ. Le retard du train à une autre gare n'est jamais retenu.
    """
    retard_arr = 0
    retard_dep = 0
    est_supprime = False
    try:
        conn = db_connect(timeout=10)
        est_supprime = train_supprime_radar_db(
            date_j, numero_train,
            gare_user=gare_depart,
            heure_theo=heure_theo,
            gare_arrivee=gare_arrivee,
        )
        rows = conn.execute(
            """SELECT gare_depart, gare_arrivee, retard_arrivee_min, retard_depart_min
               FROM surveillance_trains WHERE date=? AND numero_train=?""",
            (date_j, str(numero_train)),
        ).fetchall()
        for g_dep, g_arr, r_arr, r_dep in rows:
            if gare_arrivee and _gares_similaires(g_arr, gare_arrivee):
                retard_arr = max(retard_arr, int(r_arr or 0))
            if gare_depart and _gares_similaires(g_dep, gare_depart):
                retard_dep = max(retard_dep, int(r_dep or 0))
        if gare_arrivee:
            retard_arr = max(retard_arr, _retard_historique_gare(conn, date_j, numero_train, gare_arrivee))
        if gare_depart:
            retard_dep = max(retard_dep, _retard_historique_gare(conn, date_j, numero_train, gare_depart))
        conn.close()
    except Exception:
        pass
    retard = retard_arr if retard_arr > 0 else retard_dep
    return retard, est_supprime


# --- Libellés PDF partagés (règles R1, R6, R7) ---
_TERMES_MOTIF_INTERNES = (
    "gtfs-rt", "gtfs", "radar", "navitia", "confirmé par", "confirme par",
    "probable", "absent des données", "absent des donnees", "non précisé", "non precise",
    "auto",
)


def motif_pdf_officiel(motif_brut, type_inc, retard_min):
    """
    Motif en français usager, sans jargon interne. Fonction unique pour le bot,
    l'API et le mobile. Garde les motifs métier connus (rupture de charge, signalement).
    """
    m = str(motif_brut or "").replace("\n", " ").replace("\r", " ").strip()
    m_low = m.lower()
    if m in (MOTIF_RUPTURE_CHARGE, MOTIF_SIGNALEMENT_USAGER) or m.startswith("Déclaration du voyageur"):
        return m
    if m.startswith("Arrêt à ") and "changement de train forcé" in m_low:
        return m        # motif du report automatique (suppression partielle / train arrêté)
    if "train suivant" in m_low:
        return "Prise du train suivant (retard en cascade)"
    if any(t in m_low for t in _TERMES_MOTIF_INTERNES):
        m = ""
    if not m or len(m) < 4:
        if str(type_inc or "").lower() == "supprime":
            return "Train supprimé"
        return f"Retard constaté à l'arrivée (+{int(retard_min or 0)} min)"
    return m[:62]


def libelle_trajet_pdf(gare_depart, gare_arrivee):
    """`Metz -> Luxembourg` à partir du segment usager. ValueError si incohérent."""
    g_dep = nom_canonique_gare(gare_depart)
    g_arr = nom_canonique_gare(gare_arrivee)
    if not g_dep or not g_arr:
        raise ValueError(f"Segment incomplet : {gare_depart!r} -> {gare_arrivee!r}")
    if gares_equivalentes(g_dep, g_arr):
        raise ValueError(f"Départ et arrivée équivalents : {g_dep}")
    return f"{g_dep} -> {g_arr}"


# --- Schéma phase 1 : origine des trajets, direction commerciale, propositions radar ---
_SCHEMA_PHASE1_OK = False


def migrer_schema_phase1():
    """Colonnes `origine`, `direction_commerciale` et table `propositions_radar`. Idempotent."""
    global _SCHEMA_PHASE1_OK
    if _SCHEMA_PHASE1_OK:
        return
    try:
        conn = db_connect(timeout=10)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(trajets_valides)").fetchall()}
        if cols:
            if "origine" not in cols:
                conn.execute("ALTER TABLE trajets_valides ADD COLUMN origine TEXT DEFAULT 'checkin';")
            if "direction_commerciale" not in cols:
                conn.execute("ALTER TABLE trajets_valides ADD COLUMN direction_commerciale TEXT;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS propositions_radar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_telegram TEXT NOT NULL,
                date TEXT NOT NULL,
                numero_train TEXT NOT NULL,
                heure_theo TEXT NOT NULL,
                sens TEXT,
                tranche_horaire TEXT,
                gare_depart TEXT,
                gare_arrivee TEXT,
                direction_commerciale TEXT,
                type_incident TEXT NOT NULL,
                retard_min INTEGER DEFAULT 0,
                motif TEXT,
                statut TEXT NOT NULL DEFAULT 'proposee',
                created_ts INTEGER,
                decided_ts INTEGER,
                UNIQUE(id_telegram, date, numero_train, heure_theo)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prop_user_statut ON propositions_radar(id_telegram, statut, date)"
        )
        conn.commit()
        conn.close()
        _SCHEMA_PHASE1_OK = True
    except Exception as e:
        logging.getLogger("bazo.metier").warning("[SCHEMA] migration phase 1 : %s", e)


def proposer_trajet_radar(id_telegram, user, incident, sens=None, tranche=None):
    """
    Le radar a vu un incident dans le créneau de l'usager, sans check-in sur ce train :
    on propose « j'étais dans ce train » au lieu de fabriquer une ligne d'attestation (R3).
    Retourne l'identifiant de la proposition (existante ou créée), ou None.
    """
    if not id_telegram or not user or not incident:
        return None
    migrer_schema_phase1()
    date_j = str(incident.get("date") or "")
    num = str(incident.get("numero_train") or "")
    h_theo = str(incident.get("heure_theo") or "")
    if not date_j or not num or not h_theo:
        return None
    type_inc = str(incident.get("type") or "retard").lower()
    if sens is None:
        sens, tranche = incident_pertinent_profil(
            str(incident.get("gare") or ""), str(incident.get("destination") or ""), h_theo,
            user.get("gare_depart"), user.get("gare_retour"),
            user.get("heures"), user.get("heures_retour"),
            type_incident=type_inc,
        )
    if not sens:
        return None
    g_dep, g_arr = (
        (user.get("gare_depart"), user.get("gare_retour"))
        if sens == "aller"
        else (user.get("gare_retour"), user.get("gare_depart"))
    )
    retard = 0 if type_inc == "supprime" else int(
        incident.get("retard_arrivee_min") or incident.get("retard_depart_min") or incident.get("info") or 0
    )
    motif = motif_pdf_officiel(incident.get("motif"), type_inc, retard)
    try:
        conn = db_connect(timeout=10)
        conn.execute(
            """
            INSERT OR IGNORE INTO propositions_radar (
                id_telegram, date, numero_train, heure_theo, sens, tranche_horaire,
                gare_depart, gare_arrivee, direction_commerciale, type_incident,
                retard_min, motif, statut, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposee', ?)
            """,
            (
                str(id_telegram), date_j, num, h_theo, sens,
                tranche or user.get("heures" if sens == "aller" else "heures_retour"),
                nom_canonique_gare(g_dep), nom_canonique_gare(g_arr),
                str(incident.get("destination") or "")[:60] or None,
                type_inc, retard, motif, int(time.time()),
            ),
        )
        row = conn.execute(
            "SELECT id FROM propositions_radar WHERE id_telegram=? AND date=? AND numero_train=? AND heure_theo=?",
            (str(id_telegram), date_j, num, h_theo),
        ).fetchone()
        conn.commit()
        conn.close()
        return int(row[0]) if row else None
    except Exception as e:
        logging.getLogger("bazo.metier").warning("[PROPOSITION] %s", e)
        return None


def lister_propositions_radar(id_telegram, mois=None, statut="proposee"):
    migrer_schema_phase1()
    try:
        conn = db_connect(timeout=10)
        sql = """
            SELECT id, date, numero_train, heure_theo, sens, gare_depart, gare_arrivee,
                   type_incident, retard_min, motif
            FROM propositions_radar WHERE id_telegram = ? AND statut = ?
        """
        params = [str(id_telegram), statut]
        if mois:
            sql += " AND date LIKE ?"
            params.append(f"{mois}%")
        sql += " ORDER BY date DESC, heure_theo DESC"
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except Exception:
        return []
    return [
        {
            "id": r[0], "date": r[1], "numero_train": r[2], "heure_theo": r[3], "sens": r[4],
            "gare_depart": r[5], "gare_arrivee": r[6], "type_incident": r[7],
            "retard_min": int(r[8] or 0), "motif": r[9],
        }
        for r in rows
    ]


def _charger_proposition(conn, id_proposition, id_user):
    return conn.execute(
        """
        SELECT id, id_telegram, date, numero_train, heure_theo, sens, tranche_horaire,
               gare_depart, gare_arrivee, direction_commerciale, type_incident, retard_min, motif, statut
        FROM propositions_radar WHERE id = ? AND id_telegram = ?
        """,
        (int(id_proposition), str(id_user)),
    ).fetchone()


def accepter_proposition_radar(id_proposition, id_user):
    """« J'étais dans ce train » : crée le trajet avec origine confirme_usager. Retourne l'id trajet."""
    migrer_schema_phase1()
    try:
        conn = db_connect(timeout=10)
        p = _charger_proposition(conn, id_proposition, id_user)
        if not p or p[13] != "proposee":
            conn.close()
            return None
        (_pid, id_tg, date_j, num, h_theo, sens, tranche, g_dep, g_arr, dir_com, type_inc, retard, motif, _st) = p
        if type_inc == "supprime":
            statut, h_reel, retard_v = "valide_supprime", "Annulé", 0
        else:
            statut, h_reel, retard_v = "valide_normal", None, int(retard or 0)
        conn.execute(
            """
            INSERT INTO trajets_valides (
                id_telegram, numero_train, date, heure_theo, heure_arrivee_theo,
                heure_reelle_arrivee, statut_validation, tranche_horaire, sens,
                gare_depart, gare_arrivee, destination, retard_arrivee_min, motif_validation,
                origine, direction_commerciale
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirme_usager', ?)
            ON CONFLICT(id_telegram, date, numero_train, heure_theo) DO UPDATE SET
                statut_validation = excluded.statut_validation,
                retard_arrivee_min = MAX(COALESCE(trajets_valides.retard_arrivee_min, 0), excluded.retard_arrivee_min),
                motif_validation = COALESCE(NULLIF(trajets_valides.motif_validation, ''), excluded.motif_validation),
                origine = CASE WHEN trajets_valides.origine = 'checkin' THEN 'checkin' ELSE 'confirme_usager' END,
                direction_commerciale = COALESCE(trajets_valides.direction_commerciale, excluded.direction_commerciale)
            """,
            (
                id_tg, num, date_j, h_theo, h_reel, statut, tranche, sens,
                g_dep, g_arr, dir_com or g_arr, retard_v, motif, dir_com,
            ),
        )
        tid = conn.execute(
            "SELECT id FROM trajets_valides WHERE id_telegram=? AND date=? AND numero_train=? AND heure_theo=?",
            (id_tg, date_j, num, h_theo),
        ).fetchone()
        conn.execute(
            "UPDATE propositions_radar SET statut='acceptee', decided_ts=? WHERE id=?",
            (int(time.time()), int(id_proposition)),
        )
        conn.commit()
        conn.close()
        return int(tid[0]) if tid else None
    except Exception as e:
        logging.getLogger("bazo.metier").warning("[PROPOSITION] acceptation : %s", e)
        return None


def refuser_proposition_radar(id_proposition, id_user):
    migrer_schema_phase1()
    try:
        conn = db_connect(timeout=10)
        p = _charger_proposition(conn, id_proposition, id_user)
        if not p or p[13] != "proposee":
            conn.close()
            return False
        conn.execute(
            "UPDATE propositions_radar SET statut='refusee', decided_ts=? WHERE id=?",
            (int(time.time()), int(id_proposition)),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def persister_incident_depuis_alerte(incident):
    """Secours : persiste l'incident API dans historique (alerte → PDF durable)."""
    try:
        date_j = str(incident.get("date") or "")
        num = str(incident.get("numero_train") or "")
        gare = str(incident.get("gare") or "")
        if not date_j or not num or not gare:
            return
        type_inc = str(incident.get("type") or "retard").lower()
        motif = str(incident.get("motif") or "Incident confirmé par alerte Bazo (radar)")[:120]
        h_theo = str(incident.get("heure_theo") or "")
        h_reel = str(incident.get("heure_reel") or "")
        dest = str(incident.get("destination") or "")
        conn = db_connect(timeout=8)
        if h_theo:
            row = conn.execute(
                "SELECT id, retard_min, motif FROM historique WHERE date=? AND numero_train=? AND gare=? AND heure_theo=?",
                (date_j, num, gare, h_theo),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, retard_min, motif FROM historique WHERE date=? AND numero_train=? AND gare=?",
                (date_j, num, gare),
            ).fetchone()
        if type_inc == "supprime":
            if row:
                conn.execute(
                    """UPDATE historique SET type='supprime', retard_min=0, retard_depart_min=0,
                       retard_arrivee_min=0, heure_reel='Annulé', motif=? WHERE id=?""",
                    (motif, row[0]),
                )
            else:
                conn.execute(
                    """INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min,
                       retard_depart_min, retard_arrivee_min, motif, heure_theo, heure_reel)
                       VALUES (?, ?, ?, ?, 'supprime', 0, 0, 0, ?, ?, 'Annulé')""",
                    (date_j, num, dest, gare, motif, h_theo),
                )
        else:
            r_dep = int(incident.get("retard_depart_min") or incident.get("info") or 0)
            r_arr = int(incident.get("retard_arrivee_min") or r_dep)
            r_max = max(r_dep, r_arr)
            if row:
                ancien = int(row[1] or 0)
                if r_max > ancien:
                    conn.execute(
                        """UPDATE historique SET type='retard', retard_min=?, retard_depart_min=?,
                           retard_arrivee_min=?, heure_reel=?, motif=? WHERE id=?""",
                        (r_max, r_dep, r_arr, h_reel, motif, row[0]),
                    )
            elif r_max >= SEUIL_RETARD_MIN:
                conn.execute(
                    """INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min,
                       retard_depart_min, retard_arrivee_min, motif, heure_theo, heure_reel)
                       VALUES (?, ?, ?, ?, 'retard', ?, ?, ?, ?, ?, ?)""",
                    (date_j, num, dest, gare, r_max, r_dep, r_arr, motif, h_theo, h_reel),
                )
        conn.commit()
        conn.close()
    except Exception:
        pass


def activer_surveillance_checkin(date_j, numero_train, gare_dep, gare_arr, destination, heure_theo):
    """Enregistre le train check-in pour suivi arrivée par le radar."""
    try:
        conn = db_connect(timeout=10)
        conn.execute(
            """
            INSERT OR IGNORE INTO surveillance_trains
                (date, numero_train, gare_depart, gare_arrivee, destination, heure_theo, statut)
            VALUES (?, ?, ?, ?, ?, ?, 'surveillance')
            """,
            (
                date_j, str(numero_train), str(gare_dep or ""), str(gare_arr or ""),
                str(destination or gare_arr or ""), str(heure_theo or ""),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def lier_checkins_retards_radar(date_j=None):
    """À chaque scan : relie TOUS les check-ins du jour aux retards radar (même numéro de train)."""
    if date_j is None:
        date_j = datetime.now(tz_paris).strftime("%Y-%m-%d")
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """
            SELECT id, numero_train, heure_theo, gare_depart, gare_arrivee, statut_validation
            FROM trajets_valides
            WHERE date = ? AND statut_validation IN ('en_attente', 'valide_normal')
            """,
            (date_j,),
        ).fetchall()
        for tid, num, h_theo, g_dep, g_arr, statut in rows:
            r_scan, suppr = retard_reel_scanner_db(date_j, num, g_arr, g_dep, heure_theo=h_theo)
            retard = int(r_scan or 0)
            if suppr:
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET statut_validation = 'valide_supprime',
                        motif_validation = COALESCE(NULLIF(motif_validation, ''), 'Train supprimé (annulation SNCF)'),
                        heure_reelle_arrivee = 'Annulé',
                        retard_arrivee_min = 0
                    WHERE id = ?
                    """,
                    (tid,),
                )
            elif retard >= SEUIL_RETARD_MIN:
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET statut_validation = CASE
                            WHEN statut_validation = 'en_attente' THEN 'valide_normal'
                            ELSE statut_validation END,
                        retard_arrivee_min = MAX(COALESCE(retard_arrivee_min, 0), ?),
                        motif_validation = COALESCE(
                            NULLIF(motif_validation, ''),
                            'Retard capté par le radar (+' || ? || ' min)'
                        )
                    WHERE id = ?
                    """,
                    (retard, retard, tid),
                )
        conn.commit()
        conn.close()
    except Exception:
        pass


def promouvoir_trajet_alerte_radar(id_telegram, date_j, numero_train, heure_theo, incident):
    """Garantie alerte→PDF : fige l'incident dès qu'une alerte ciblée check-in est envoyée."""
    if not id_telegram or not date_j or not numero_train or not heure_theo:
        return False
    persister_incident_depuis_alerte(incident)
    try:
        conn = db_connect(timeout=10)
        row = conn.execute(
            """
            SELECT id, statut_validation, gare_depart, gare_arrivee, retard_arrivee_min
            FROM trajets_valides
            WHERE id_telegram = ? AND date = ? AND numero_train = ? AND heure_theo = ?
            """,
            (str(id_telegram), date_j, str(numero_train), str(heure_theo)),
        ).fetchone()
        if not row:
            conn.close()
            return False
        tid, statut, g_dep, g_arr, r_db = row
        type_inc = str(incident.get("type") or "").lower()
        motif_inc = str(incident.get("motif") or "Non précisé")[:120]
        if type_inc == "supprime":
            conn.execute(
                """
                UPDATE trajets_valides
                SET statut_validation = 'valide_supprime',
                    motif_validation = ?,
                    heure_reelle_arrivee = 'Annulé',
                    retard_arrivee_min = 0
                WHERE id = ?
                """,
                (motif_inc or "Train supprimé (annulation SNCF)", tid),
            )
        else:
            r_dep = int(incident.get("retard_depart_min") or incident.get("info") or 0)
            r_scan, _ = retard_reel_scanner_db(date_j, numero_train, g_arr, g_dep)
            retard = max(int(r_db or 0), r_dep, r_scan)
            h_reel = str(incident.get("heure_reel") or "").strip() or None
            if retard >= SEUIL_RETARD_MIN:
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET statut_validation = 'valide_normal',
                        retard_arrivee_min = MAX(COALESCE(retard_arrivee_min, 0), ?),
                        motif_validation = COALESCE(NULLIF(motif_validation, ''), ?),
                        heure_reelle_arrivee = COALESCE(heure_reelle_arrivee, ?)
                    WHERE id = ?
                    """,
                    (retard, MOTIF_ALERTE_RADAR, h_reel, tid),
                )
            elif statut == "en_attente" and r_dep > 0:
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET motif_validation = COALESCE(motif_validation, ?)
                    WHERE id = ?
                    """,
                    (f"Retard départ détecté ({r_dep} min) — arrivée en cours de suivi", tid),
                )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def traiter_suppression_radar_utilisateur(id_telegram, user, incident):
    """
    Suppression vue par le radar dans le créneau d'un abonné.
    - Si l'usager a déclaré CE train ce jour-là et que la gare de l'incident est sur son
      segment : le trajet déclaré passe à `valide_supprime`. Retourne ("maj", id_trajet).
    - Sinon : aucune ligne d'attestation n'est fabriquée ; on crée une proposition
      « j'étais dans ce train ». Retourne ("proposition", id_proposition).
    - Hors profil : retourne (None, None).
    Ne modifie jamais le numéro de train d'un check-in existant (règle R3).
    """
    if not id_telegram or not user or not incident:
        return None, None
    id_tg = str(id_telegram)
    date_j = str(incident.get("date") or "")
    num = str(incident.get("numero_train") or "")
    gare = str(incident.get("gare") or "")
    dest = str(incident.get("destination") or "")
    h_theo = str(incident.get("heure_theo") or "")
    if not date_j or not num or not h_theo:
        return None, None
    sens, tranche = incident_pertinent_profil(
        gare, dest, h_theo,
        user.get("gare_depart"), user.get("gare_retour"),
        user.get("heures"), user.get("heures_retour"),
        type_incident="supprime",
        pour_alerte=True,
    )
    if not sens:
        return None, None
    persister_incident_depuis_alerte(incident)
    motif = motif_pdf_officiel(incident.get("motif"), "supprime", 0)
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """
            SELECT id, gare_depart, gare_arrivee FROM trajets_valides
            WHERE id_telegram = ? AND date = ? AND numero_train = ?
              AND statut_validation IN ('en_attente', 'valide_normal', 'valide_supprime')
            """,
            (id_tg, date_j, num),
        ).fetchall()
        for tid, g_dep_t, g_arr_t in rows:
            if gare_sur_segment(gare, g_dep_t, g_arr_t):
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET statut_validation = 'valide_supprime',
                        motif_validation = COALESCE(NULLIF(motif_validation, ''), ?),
                        heure_reelle_arrivee = 'Annulé',
                        retard_arrivee_min = 0
                    WHERE id = ?
                    """,
                    (motif, tid),
                )
                conn.commit()
                conn.close()
                return "maj", int(tid)
        conn.close()
    except Exception:
        return None, None
    pid = proposer_trajet_radar(id_tg, user, incident, sens=sens, tranche=tranche)
    return ("proposition", pid) if pid else (None, None)


def enregistrer_suppression_radar_utilisateur(id_telegram, user, incident):
    """Alias rétrocompatible : voir traiter_suppression_radar_utilisateur."""
    action, _ = traiter_suppression_radar_utilisateur(id_telegram, user, incident)
    return action is not None


# =============================================================================
# Phase 2 — Signalement communautaire unifié (section 4, option C de la spec)
# =============================================================================
# Couche officielle : le radar (données temps réel SNCF) est la seule source des
# retards et suppressions inscrits au PDF sans action de l'usager.
# Couche communautaire : une seule table `signalements`, taxonomie fermée,
# rattachée au check-in de l'auteur, quorum par les autres voyageurs du même train.

MENTION_COMMUNAUTAIRE = "Information voyageurs, non vérifiée par SNCF"
PROVENANCE_OFFICIELLE = "officielle"
PROVENANCE_VOYAGEUR = "voyageur"
LIBELLE_PROVENANCE = {
    PROVENANCE_OFFICIELLE: "Données temps réel SNCF",
    PROVENANCE_VOYAGEUR: "Déclaration du voyageur",
}

# code -> (libellé, effet PDF possible, valeur attendue)
TYPES_SIGNALEMENT = {
    "retard_au_depart":       {"libelle": "Retard au départ",            "effet_pdf": "retard",    "valeur": False},
    "train_supprime_annonce": {"libelle": "Suppression annoncée",        "effet_pdf": "supprime",  "valeur": False},
    "bus_substitution":       {"libelle": "Bus de substitution",         "effet_pdf": "bus",       "valeur": False},
    "arret_prolonge":         {"libelle": "Arrêt prolongé",              "effet_pdf": "retard",    "valeur": False},
    "voie_annoncee":          {"libelle": "Voie annoncée",               "effet_pdf": None,        "valeur": True},
    "forte_affluence":        {"libelle": "Forte affluence",             "effet_pdf": None,        "valeur": False},
    "rame_courte":            {"libelle": "Rame courte",                 "effet_pdf": None,        "valeur": False},
    "je_suis_bloque":         {"libelle": "Je suis bloqué à bord / en gare", "effet_pdf": None,    "valeur": False},
}
QUORUM_CONFIRMATION = 2
ECART_CONTESTATION = 2
DUREE_VIE_SIGNALEMENT_SEC = 3 * 3600
_RE_VOIE = re.compile(r"^[0-9]{1,2}[A-Za-z]?$|^[A-Za-z]$")

_SCHEMA_PHASE2_OK = False
_TABLES_LEGACY_SIGNALEMENT = ("waze_signalements", "waze_votes", "signalements_trafic", "confirmations_trafic")


def libelle_type_signalement(code_type, valeur=None):
    info = TYPES_SIGNALEMENT.get(str(code_type or ""))
    if not info:
        return str(code_type or "")
    if info["valeur"] and valeur:
        return f"{info['libelle']} : {valeur}"
    return info["libelle"]


def types_signalement_publics():
    """Liste pour les clients (bot, API, mobile) : code, libellé, valeur attendue, effet PDF."""
    return [
        {"code": c, "libelle": v["libelle"], "valeur_requise": v["valeur"], "effet_pdf": bool(v["effet_pdf"])}
        for c, v in TYPES_SIGNALEMENT.items()
    ]


def migrer_schema_phase2(retirer_tables_legacy=False):
    """Tables `signalements`, `signalements_votes`, colonne `trajets_valides.provenance` (additif).
    Avec `retirer_tables_legacy=True` (démarrage du bot uniquement), retire les anciennes tables
    communautaires si elles sont vides, pour ne jamais casser un service encore sur l'ancien code."""
    global _SCHEMA_PHASE2_OK
    if _SCHEMA_PHASE2_OK and not retirer_tables_legacy:
        return
    try:
        migrer_schema_phase1()
        conn = db_connect(timeout=10)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(trajets_valides)").fetchall()}
        if cols and "provenance" not in cols:
            conn.execute(
                f"ALTER TABLE trajets_valides ADD COLUMN provenance TEXT DEFAULT '{PROVENANCE_OFFICIELLE}';"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signalements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                numero_train TEXT NOT NULL,
                heure_theo TEXT NOT NULL,
                gare_uic TEXT NOT NULL,
                id_auteur TEXT NOT NULL,
                id_trajet INTEGER NOT NULL,
                code_type TEXT NOT NULL,
                valeur TEXT,
                photo_chemin TEXT,
                statut TEXT NOT NULL DEFAULT 'non_confirme',
                nb_confirmations INTEGER NOT NULL DEFAULT 0,
                nb_refutations INTEGER NOT NULL DEFAULT 0,
                created_ts INTEGER NOT NULL,
                expire_ts INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sig_train ON signalements(date, numero_train, gare_uic)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sig_auteur ON signalements(id_auteur, date)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signalements_votes (
                signalement_id INTEGER NOT NULL,
                id_utilisateur TEXT NOT NULL,
                vote TEXT NOT NULL CHECK (vote IN ('confirme', 'refute')),
                id_trajet INTEGER NOT NULL,
                created_ts INTEGER NOT NULL,
                PRIMARY KEY (signalement_id, id_utilisateur)
            )
            """
        )
        for table in (_TABLES_LEGACY_SIGNALEMENT if retirer_tables_legacy else ()):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                continue
            if n == 0:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            else:
                logging.getLogger("bazo.metier").warning(
                    "[SCHEMA] table %s non vide (%s lignes) : conservée", table, n
                )
        conn.commit()
        conn.close()
        _SCHEMA_PHASE2_OK = True
    except Exception as e:
        logging.getLogger("bazo.metier").warning("[SCHEMA] migration phase 2 : %s", e)


def _trajet_checkin_sur_train(conn, id_utilisateur, date_j, numero_train, heure_theo=None):
    """Trajet déclaré (check-in ou confirmé) de cet usager sur ce train, sinon None."""
    sql = """
        SELECT id, gare_depart, gare_arrivee, statut_validation, retard_arrivee_min, heure_theo
        FROM trajets_valides
        WHERE id_telegram = ? AND date = ? AND numero_train = ?
          AND statut_validation != 'annule'
          AND COALESCE(origine, 'checkin') IN ('checkin', 'confirme_usager')
    """
    params = [str(id_utilisateur), str(date_j), str(numero_train)]
    if heure_theo:
        sql += " AND heure_theo = ?"
        params.append(str(heure_theo))
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _ts_expiration(date_j, heure_theo):
    try:
        h, m = str(heure_theo).lower().replace(":", "h").split("h")[:2]
        dt = datetime.strptime(f"{date_j} {int(h):02d}:{int(m or 0):02d}", "%Y-%m-%d %H:%M").replace(tzinfo=tz_paris)
        return int(dt.timestamp()) + DUREE_VIE_SIGNALEMENT_SEC
    except Exception:
        return int(time.time()) + DUREE_VIE_SIGNALEMENT_SEC


def _dict_signalement(row):
    (sid, date_j, num, h_theo, gare_uic, id_auteur, id_trajet, code, valeur, photo,
     statut, nb_c, nb_r, created, expire) = row
    return {
        "id": int(sid), "date": date_j, "numero_train": num, "heure_theo": h_theo,
        "gare_uic": gare_uic, "id_auteur": str(id_auteur), "id_trajet": int(id_trajet),
        "code_type": code, "valeur": valeur, "libelle": libelle_type_signalement(code, valeur),
        "photo": bool(photo), "statut": statut,
        "nb_confirmations": int(nb_c or 0), "nb_refutations": int(nb_r or 0),
        "created_ts": int(created or 0), "expire_ts": int(expire or 0),
        "mention": MENTION_COMMUNAUTAIRE,
        "effet_pdf_possible": bool((TYPES_SIGNALEMENT.get(code) or {}).get("effet_pdf")),
    }


_COLS_SIG = ("id, date, numero_train, heure_theo, gare_uic, id_auteur, id_trajet, code_type, valeur, "
             "photo_chemin, statut, nb_confirmations, nb_refutations, created_ts, expire_ts")


def creer_signalement(id_auteur, date_j, numero_train, heure_theo, code_type, valeur=None, photo_chemin=None):
    """
    Crée un signalement communautaire. Exige un check-in de l'auteur sur ce train.
    Retourne {"ok": True, "signalement": {...}} ou {"ok": False, "erreur": code}.
    Codes d'erreur : type_inconnu, valeur_requise, valeur_invalide, checkin_requis.
    """
    code = str(code_type or "").strip()
    info = TYPES_SIGNALEMENT.get(code)
    if not info:
        return {"ok": False, "erreur": "type_inconnu"}
    val = None
    if info["valeur"]:
        val = str(valeur or "").strip().upper()
        if not val:
            return {"ok": False, "erreur": "valeur_requise"}
        if not _RE_VOIE.match(val):
            return {"ok": False, "erreur": "valeur_invalide"}
    migrer_schema_phase2()
    try:
        from bazo_geo import uic_pour_gare
    except Exception:  # pragma: no cover
        uic_pour_gare = lambda *_a, **_k: None  # noqa: E731
    conn = db_connect(timeout=10)
    try:
        traj = _trajet_checkin_sur_train(conn, id_auteur, date_j, numero_train, heure_theo)
        if not traj:
            return {"ok": False, "erreur": "checkin_requis"}
        tid, g_dep, _g_arr, _st, _r, h_theo_trajet = traj
        heure_theo = heure_theo or h_theo_trajet
        gare_uic = uic_pour_gare(g_dep or "") or (g_dep or "inconnue")
        now_ts = int(time.time())
        if _ts_expiration(date_j, heure_theo) < now_ts:
            return {"ok": False, "erreur": "fenetre_close"}
        deja = conn.execute(
            f"SELECT {_COLS_SIG} FROM signalements WHERE id_auteur=? AND date=? AND numero_train=? "
            "AND heure_theo=? AND code_type=? AND statut != 'expire'",
            (str(id_auteur), str(date_j), str(numero_train), str(heure_theo), code),
        ).fetchone()
        if deja:
            return {"ok": True, "signalement": _dict_signalement(deja), "existant": True}
        cur = conn.execute(
            """
            INSERT INTO signalements (
                date, numero_train, heure_theo, gare_uic, id_auteur, id_trajet, code_type, valeur,
                photo_chemin, statut, created_ts, expire_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'non_confirme', ?, ?)
            """,
            (str(date_j), str(numero_train), str(heure_theo), str(gare_uic), str(id_auteur), int(tid),
             code, val, photo_chemin, now_ts, _ts_expiration(date_j, heure_theo)),
        )
        sid = cur.lastrowid
        conn.commit()
        row = conn.execute(f"SELECT {_COLS_SIG} FROM signalements WHERE id=?", (sid,)).fetchone()
        return {"ok": True, "signalement": _dict_signalement(row), "existant": False}
    finally:
        conn.close()


def recalculer_statut_signalement(signalement_id, conn=None):
    """Recalcule compteurs et statut (quorum). `expire` n'est jamais réouvert."""
    own = conn is None
    if own:
        conn = db_connect(timeout=10)
    try:
        nb_c = conn.execute(
            "SELECT COUNT(*) FROM signalements_votes WHERE signalement_id=? AND vote='confirme'", (signalement_id,)
        ).fetchone()[0]
        nb_r = conn.execute(
            "SELECT COUNT(*) FROM signalements_votes WHERE signalement_id=? AND vote='refute'", (signalement_id,)
        ).fetchone()[0]
        row = conn.execute("SELECT statut FROM signalements WHERE id=?", (signalement_id,)).fetchone()
        if not row:
            return None
        statut = row[0]
        if statut != "expire":
            if nb_r - nb_c >= ECART_CONTESTATION:
                statut = "conteste"
            elif nb_c >= QUORUM_CONFIRMATION:
                statut = "confirme"
            else:
                statut = "non_confirme"
        conn.execute(
            "UPDATE signalements SET nb_confirmations=?, nb_refutations=?, statut=? WHERE id=?",
            (nb_c, nb_r, statut, signalement_id),
        )
        if own:
            conn.commit()
        return statut
    finally:
        if own:
            conn.close()


def voter_signalement(signalement_id, id_utilisateur, vote):
    """
    Vote confirme/refute. Exige un check-in du votant sur le même train, interdit à l'auteur,
    un seul vote par compte. Retourne {"ok": True, "statut": ...} ou {"ok": False, "erreur": code}.
    Codes : vote_invalide, introuvable, expire, auteur, checkin_requis, deja_vote.
    """
    vote = str(vote or "").strip().lower()
    if vote not in ("confirme", "refute"):
        return {"ok": False, "erreur": "vote_invalide"}
    migrer_schema_phase2()
    conn = db_connect(timeout=10)
    try:
        row = conn.execute(
            "SELECT id_auteur, date, numero_train, heure_theo, statut FROM signalements WHERE id=?",
            (int(signalement_id),),
        ).fetchone()
        if not row:
            return {"ok": False, "erreur": "introuvable"}
        id_auteur, date_j, num, h_theo, statut = row
        if statut == "expire":
            return {"ok": False, "erreur": "expire"}
        if str(id_auteur) == str(id_utilisateur):
            return {"ok": False, "erreur": "auteur"}
        traj = _trajet_checkin_sur_train(conn, id_utilisateur, date_j, num, h_theo)
        if not traj:
            return {"ok": False, "erreur": "checkin_requis"}
        deja = conn.execute(
            "SELECT 1 FROM signalements_votes WHERE signalement_id=? AND id_utilisateur=?",
            (int(signalement_id), str(id_utilisateur)),
        ).fetchone()
        if deja:
            return {"ok": False, "erreur": "deja_vote"}
        conn.execute(
            "INSERT INTO signalements_votes (signalement_id, id_utilisateur, vote, id_trajet, created_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(signalement_id), str(id_utilisateur), vote, int(traj[0]), int(time.time())),
        )
        statut = recalculer_statut_signalement(int(signalement_id), conn=conn)
        conn.commit()
        return {"ok": True, "statut": statut}
    finally:
        conn.close()


def expirer_signalements(now_ts=None):
    """Passe en `expire` les signalements dont l'heure théorique + 3 h est dépassée."""
    migrer_schema_phase2()
    now_ts = int(now_ts or time.time())
    try:
        conn = db_connect(timeout=10)
        n = conn.execute(
            "UPDATE signalements SET statut='expire' WHERE statut != 'expire' AND expire_ts < ?", (now_ts,)
        ).rowcount
        conn.commit()
        conn.close()
        return n
    except Exception:
        return 0


def purger_photos_signalements(jours=30):
    """Supprime du disque et de la base les photos de signalement de plus de `jours` jours."""
    import os
    migrer_schema_phase2()
    limite = int(time.time()) - int(jours) * 86400
    n = 0
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            "SELECT id, photo_chemin FROM signalements WHERE photo_chemin IS NOT NULL AND created_ts < ?",
            (limite,),
        ).fetchall()
        for sid, chemin in rows:
            try:
                if chemin and os.path.exists(chemin):
                    os.remove(chemin)
            except OSError:
                pass
            conn.execute("UPDATE signalements SET photo_chemin=NULL WHERE id=?", (sid,))
            n += 1
        conn.commit()
        conn.close()
    except Exception:
        pass
    return n


def signalements_du_train(date_j, numero_train, heure_theo=None, inclure_expires=False):
    """Fil du train : signalements communautaires, toujours accompagnés de la mention."""
    migrer_schema_phase2()
    try:
        conn = db_connect(timeout=10)
        sql = f"SELECT {_COLS_SIG} FROM signalements WHERE date=? AND numero_train=?"
        params = [str(date_j), str(numero_train)]
        if heure_theo:
            sql += " AND heure_theo=?"
            params.append(str(heure_theo))
        if not inclure_expires:
            sql += " AND statut != 'expire'"
        sql += " ORDER BY created_ts DESC"
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [_dict_signalement(r) for r in rows]
    except Exception:
        return []


def charger_signalement(signalement_id):
    migrer_schema_phase2()
    try:
        conn = db_connect(timeout=10)
        row = conn.execute(f"SELECT {_COLS_SIG} FROM signalements WHERE id=?", (int(signalement_id),)).fetchone()
        conn.close()
        return _dict_signalement(row) if row else None
    except Exception:
        return None


def signalements_communautaires_recents(limite=5):
    """Fil communautaire : derniers signalements non expirés (48 h), anonymisés."""
    migrer_schema_phase2()
    try:
        from bazo_geo import nom_pour_uic
    except Exception:  # pragma: no cover
        nom_pour_uic = lambda *_a, **_k: None  # noqa: E731
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """
            SELECT date, heure_theo, numero_train, gare_uic, code_type, valeur, statut
            FROM signalements
            WHERE statut IN ('non_confirme', 'confirme') AND date >= date('now', '-2 days')
            ORDER BY id DESC LIMIT ?
            """,
            (int(limite),),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    out = []
    for date_j, h_theo, num, gare_uic, code, valeur, statut in rows:
        gare = nom_pour_uic(gare_uic) or gare_uic
        out.append((date_j, h_theo, num, gare, libelle_type_signalement(code, valeur), statut))
    return out


def promouvoir_trajet_signalement(
    id_telegram, type_signalement, retard_min=0, numero_train=None, gare=None, id_trajet=None,
    signalement_id=None,
):
    """
    Effet PDF d'un signalement, uniquement sur action explicite du propriétaire du trajet.
    La ligne porte la provenance « Déclaration du voyageur » sauf si le radar confirme
    déjà l'incident (provenance officielle conservée). Retourne l'id du trajet ou None.
    """
    migrer_schema_phase2()
    code = str(type_signalement or "").lower()
    alias = {"supprime": "train_supprime_annonce", "suppression": "train_supprime_annonce",
             "bus": "bus_substitution", "panne": "bus_substitution", "retard": "retard_au_depart"}
    code = alias.get(code, code)
    info = TYPES_SIGNALEMENT.get(code)
    if not info or not info["effet_pdf"]:
        return None
    effet = info["effet_pdf"]
    date_j = datetime.now(tz_paris).strftime("%Y-%m-%d")
    try:
        conn = db_connect(timeout=10)
        if id_trajet:
            row = conn.execute(
                """
                SELECT id, numero_train, heure_theo, gare_arrivee, retard_arrivee_min, date, gare_depart
                FROM trajets_valides
                WHERE id = ? AND id_telegram = ? AND statut_validation != 'annule'
                  AND COALESCE(origine, 'checkin') IN ('checkin', 'confirme_usager')
                """,
                (int(id_trajet), str(id_telegram)),
            ).fetchone()
        else:
            sql = """
                SELECT id, numero_train, heure_theo, gare_arrivee, retard_arrivee_min, date, gare_depart
                FROM trajets_valides
                WHERE id_telegram = ? AND date = ? AND statut_validation IN ('en_attente', 'valide_normal')
                  AND COALESCE(origine, 'checkin') IN ('checkin', 'confirme_usager')
            """
            params = [str(id_telegram), date_j]
            if numero_train:
                sql += " AND numero_train = ?"
                params.append(str(numero_train))
            sql += " ORDER BY heure_theo DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
        if not row:
            conn.close()
            return None
        tid, num, h_theo, g_arr, r_db, date_j, g_dep = row
        if signalement_id:
            sig = conn.execute(
                "SELECT id_auteur FROM signalements WHERE id=? AND id_trajet=?", (int(signalement_id), int(tid))
            ).fetchone()
            if not sig or str(sig[0]) != str(id_telegram):
                conn.close()
                return None
        r_scan, suppr_scan = retard_reel_scanner_db(date_j, num, g_arr, g_dep, heure_theo=h_theo)
        libelle = libelle_type_signalement(code)
        motif_voyageur = f"{LIBELLE_PROVENANCE[PROVENANCE_VOYAGEUR]} : {libelle}"
        if effet == "supprime":
            if suppr_scan:
                prov, motif = PROVENANCE_OFFICIELLE, "Train supprimé"
            else:
                prov, motif = PROVENANCE_VOYAGEUR, motif_voyageur
            conn.execute(
                """
                UPDATE trajets_valides
                SET statut_validation = 'valide_supprime', motif_validation = ?,
                    heure_reelle_arrivee = 'Annulé', retard_arrivee_min = 0, provenance = ?
                WHERE id = ?
                """,
                (motif, prov, tid),
            )
        elif effet == "bus":
            r = max(int(retard_min or 0), int(r_db or 0), int(r_scan or 0), SEUIL_RETARD_MIN)
            conn.execute(
                """
                UPDATE trajets_valides
                SET statut_validation = 'valide_bus_panne',
                    retard_arrivee_min = MAX(COALESCE(retard_arrivee_min, 0), ?),
                    motif_validation = ?, provenance = ?
                WHERE id = ?
                """,
                (r, MOTIF_RUPTURE_CHARGE, PROVENANCE_VOYAGEUR, tid),
            )
        else:  # retard
            if suppr_scan:
                conn.execute(
                    "UPDATE trajets_valides SET statut_validation='valide_supprime', motif_validation='Train supprimé', "
                    "heure_reelle_arrivee='Annulé', retard_arrivee_min=0, provenance=? WHERE id=?",
                    (PROVENANCE_OFFICIELLE, tid),
                )
            elif int(r_scan or 0) >= SEUIL_RETARD_MIN:
                conn.execute(
                    "UPDATE trajets_valides SET statut_validation='valide_normal', "
                    "retard_arrivee_min=MAX(COALESCE(retard_arrivee_min,0), ?), provenance=? WHERE id=?",
                    (int(r_scan), PROVENANCE_OFFICIELLE, tid),
                )
            else:
                r = max(int(retard_min or 0), int(r_db or 0), SEUIL_RETARD_MIN)
                conn.execute(
                    "UPDATE trajets_valides SET statut_validation='valide_normal', "
                    "retard_arrivee_min=MAX(COALESCE(retard_arrivee_min,0), ?), motif_validation=?, provenance=? "
                    "WHERE id=?",
                    (r, motif_voyageur, PROVENANCE_VOYAGEUR, tid),
                )
        conn.commit()
        conn.close()
        return int(tid)
    except Exception as e:
        logging.getLogger("bazo.metier").warning("[SIGNALEMENT] promotion : %s", e)
        return None


def sync_trajets_valides_depuis_radar(id_telegram, mois_filtre=None):
    """Persiste retards/suppressions radar API — appelé avant PDF, bilan, healthcheck."""
    try:
        migrer_schema_phase2()
        conn = db_connect(timeout=10)
        sql = """
            SELECT id, numero_train, date, heure_theo, gare_depart, gare_arrivee, heure_arrivee_theo,
                   statut_validation, retard_arrivee_min, motif_validation
            FROM trajets_valides
            WHERE id_telegram = ? AND statut_validation IN ('en_attente', 'valide_normal', 'valide_supprime')
        """
        params = [str(id_telegram)]
        if mois_filtre:
            sql += " AND date LIKE ?"
            params.append(f"{mois_filtre}%")
        rows = conn.execute(sql, params).fetchall()
        for tid, num, date_j, h_theo, g_dep, g_arr, h_arr, statut, r_db, motif in rows:
            r_scan, suppr = retard_reel_scanner_db(date_j, num, g_arr, g_dep, heure_theo=h_theo)
            motif_api = (
                motif if motif and "GTFS" not in str(motif).upper()
                else None
            )
            if suppr and statut in ("en_attente", "valide_normal"):
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET statut_validation = 'valide_supprime',
                        motif_validation = COALESCE(?, 'Train supprimé (annulation SNCF)'),
                        heure_reelle_arrivee = 'Annulé',
                        retard_arrivee_min = 0,
                        provenance = 'officielle'
                    WHERE id = ?
                    """,
                    (motif_api or "Train supprimé (annulation SNCF)", tid),
                )
            elif r_scan >= SEUIL_RETARD_MIN:
                nouveau_motif = motif_api or f"Retard confirmé radar (+{r_scan} min)"
                if motif and "GTFS" in str(motif).upper() and r_scan >= SEUIL_RETARD_MIN:
                    nouveau_motif = f"Retard confirmé radar (+{r_scan} min)"
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET statut_validation = CASE
                            WHEN statut_validation IN ('en_attente', 'valide_supprime') THEN 'valide_normal'
                            ELSE statut_validation END,
                        retard_arrivee_min = MAX(COALESCE(retard_arrivee_min, 0), ?),
                        motif_validation = CASE
                            WHEN motif_validation IS NOT NULL AND motif_validation != ''
                                 AND UPPER(motif_validation) NOT LIKE '%GTFS%'
                                 AND motif_validation NOT LIKE 'Déclaration du voyageur%'
                            THEN motif_validation
                            ELSE ?
                        END,
                        provenance = 'officielle'
                    WHERE id = ?
                    """,
                    (r_scan, nouveau_motif, tid),
                )
            elif (r_db or 0) >= SEUIL_RETARD_MIN and statut == "en_attente":
                conn.execute(
                    """
                    UPDATE trajets_valides
                    SET statut_validation = 'valide_normal'
                    WHERE id = ?
                    """,
                    (tid,),
                )
        conn.commit()
        conn.close()
    except Exception:
        pass


def lier_checkins_retards_periode(date_debut, date_fin):
    """Relie check-ins à l'historique radar sur une période (réparation mensuelle)."""
    try:
        conn = db_connect(timeout=10)
        dates = conn.execute(
            """
            SELECT DISTINCT date FROM trajets_valides
            WHERE date >= ? AND date <= ? AND statut_validation != 'annule'
            ORDER BY date
            """,
            (date_debut, date_fin),
        ).fetchall()
        conn.close()
        for (d,) in dates:
            lier_checkins_retards_radar(d)
        return len(dates)
    except Exception:
        return 0


def _backfill_historique_depuis_trajets(mois):
    """Secours : trajets_valides avec retard figé mais historique vide → historique."""
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """
            SELECT date, numero_train, gare_depart, gare_arrivee, destination,
                   heure_theo, retard_arrivee_min, statut_validation, motif_validation
            FROM trajets_valides
            WHERE date LIKE ? AND statut_validation LIKE 'valide%'
              AND COALESCE(retard_arrivee_min, 0) >= ?
            """,
            (f"{mois}%", SEUIL_RETARD_MIN),
        ).fetchall()
        n = 0
        for date_j, num, g_dep, g_arr, dest, h_theo, retard, statut, motif in rows:
            gare = g_dep or g_arr
            ex = conn.execute(
                "SELECT 1 FROM historique WHERE date=? AND numero_train=? AND gare=?",
                (date_j, num, gare),
            ).fetchone()
            if ex:
                continue
            type_inc = "supprime" if statut == "valide_supprime" else "retard"
            persister_incident_depuis_alerte({
                "date": date_j, "numero_train": num, "gare": gare,
                "destination": dest or g_arr, "heure_theo": h_theo,
                "type": type_inc, "motif": motif or MOTIF_ALERTE_RADAR,
                "info": retard, "retard_depart_min": retard, "retard_arrivee_min": retard,
                "heure_reel": "",
            })
            n += 1
        conn.close()
        return n
    except Exception:
        return 0


def reparer_tous_utilisateurs_mois(mois):
    """Réparation complète alerte/PDF pour un mois (API prioritaire, toutes gares usager)."""
    _backfill_historique_depuis_trajets(mois)
    lier_checkins_retards_periode(f"{mois}-01", f"{mois}-31")
    n = 0
    try:
        conn = db_connect(timeout=10)
        uids = conn.execute("SELECT id_telegram FROM inscriptions").fetchall()
        conn.close()
        for (uid,) in uids:
            if not uid:
                continue
            # R3 / R8 : la réparation qualifie les trajets déclarés, elle n'en crée aucun.
            sync_trajets_valides_depuis_radar(uid, mois)
            n += 1
        return n
    except Exception:
        try:
            conn = db_connect(timeout=10)
            uids = conn.execute(
                """
                SELECT DISTINCT id_telegram FROM trajets_valides
                WHERE date LIKE ? AND statut_validation != 'annule'
                """,
                (f"{mois}%",),
            ).fetchall()
            conn.close()
            for (uid,) in uids:
                if uid:
                    sync_trajets_valides_depuis_radar(uid, mois)
            return len(uids)
        except Exception:
            return 0


def verifier_coherence_alerte_pdf(jours=60):
    """Détecte check-ins avec incident radar non figé dans trajets_valides (alerte/PDF désalignés)."""
    try:
        conn = db_connect(timeout=10)
        n = conn.execute(
            """
            SELECT COUNT(*) FROM trajets_valides tv
            WHERE tv.statut_validation = 'en_attente'
              AND tv.date >= date('now', ?)
              AND (
                EXISTS (
                  SELECT 1 FROM historique h
                  WHERE h.date = tv.date AND h.numero_train = tv.numero_train
                    AND h.type = 'supprime'
                    AND (tv.heure_theo IS NULL OR tv.heure_theo = '' OR h.heure_theo = tv.heure_theo)
                )
                OR EXISTS (
                  SELECT 1 FROM historique h
                  WHERE h.date = tv.date AND h.numero_train = tv.numero_train
                    AND h.type = 'retard'
                    AND COALESCE(h.retard_arrivee_min, h.retard_min, 0) >= ?
                    AND (tv.heure_theo IS NULL OR tv.heure_theo = '' OR h.heure_theo = tv.heure_theo)
                )
                OR EXISTS (
                  SELECT 1 FROM surveillance_trains s
                  WHERE s.date = tv.date AND s.numero_train = tv.numero_train
                    AND COALESCE(s.retard_arrivee_min, s.retard_depart_min, 0) >= ?
                    AND (tv.heure_theo IS NULL OR tv.heure_theo = '' OR s.heure_theo = tv.heure_theo)
                )
              )
            """,
            (f"-{int(jours)} days", SEUIL_RETARD_MIN, SEUIL_RETARD_MIN),
        ).fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return -1


def accumuler_trajet_bilan(statut, retard):
    """Retourne (retard_ajout, suppr_ajout, incident_ajout) pour un trajet validé."""
    statut = str(statut).strip()
    retard = int(retard or 0)
    if statut == "valide_supprime":
        return 0, 1, 1
    if statut in ("valide_bus_panne", "valide_report"):
        return max(retard, SEUIL_RETARD_MIN), 0, 1
    if statut == "valide_normal" and retard >= SEUIL_RETARD_MIN:
        return retard, 0, 1
    return 0, 0, 0


def bilan_mois_trajets_valides_db(id_telegram, mois=None):
    """Bilan mensuel depuis trajets_valides — source unique bot + radar."""
    if mois is None:
        mois = datetime.now(tz_paris).strftime("%Y-%m")
    total_retard = 0
    total_suppr = 0
    nb_incidents = 0
    nb_trajets = 0
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute("""
            SELECT statut_validation, retard_arrivee_min, heure_arrivee_theo, heure_reelle_arrivee
            FROM trajets_valides
            WHERE id_telegram = ? AND date LIKE ? AND statut_validation LIKE 'valide%'
        """, (str(id_telegram), f"{mois}%")).fetchall()
        conn.close()
    except Exception:
        return 0, 0, 0, 0
    for statut, retard_db, h_arr_theo, h_reelle in rows:
        retard = int(retard_db) if retard_db is not None else 0
        if h_reelle and h_arr_theo:
            retard = max(retard, calculer_retard_arrivee(h_arr_theo, h_reelle))
        nb_trajets += 1
        r_add, s_add, i_add = accumuler_trajet_bilan(statut, retard)
        total_retard += r_add
        total_suppr += s_add
        nb_incidents += i_add
    return total_retard, total_suppr, nb_incidents, nb_trajets


def utilisateur_peut_recevoir_alertes(user, now_paris=None):
    if now_paris is None:
        now_paris = datetime.now(tz_paris)
    if user.get("alertes_actives", 1) == 0:
        return False
    vac = user.get("vacances_jusquau")
    if vac:
        try:
            if now_paris.date() <= datetime.strptime(str(vac), "%Y-%m-%d").date():
                return False
        except Exception:
            pass
    jours = str(user.get("jours_actifs") or "0,1,2,3,4")
    jours_list = [int(x.strip()) for x in jours.split(",") if x.strip().isdigit()]
    if not jours_list:
        return True
    return now_paris.weekday() in jours_list


def eligibilite_gf_message(total_retard, total_suppr):
    """Texte statut + conseil pour Mon Statut / bilan mensuel."""
    if total_retard >= SEUIL_GF_RETARD_MIN or total_suppr >= SEUIL_GF_SUPPR:
        return (
            "🟢 *SEUIL GARANTIE FIABILITÉ ATTEINT*",
            "Tu peux générer ton PDF (📂 Mon PDF Instantané) et déposer ta demande "
            "sur le portail SNCF. Vérifie que tes incidents concernent bien tes axes abonnés.",
        )
    if total_retard > 0 or total_suppr > 0:
        return (
            "🟠 *EN COURS D'ACQUISITION*",
            f"Il te manque encore {max(0, SEUIL_GF_RETARD_MIN - total_retard)} min de retard cumulé "
            f"ou {max(0, SEUIL_GF_SUPPR - total_suppr)} suppression(s) pour la Garantie Fiabilité TER.",
        )
    return (
        "🟢 *MOIS SANS INCIDENT ÉLIGIBLE*",
        f"Aucun retard ≥ {SEUIL_RETARD_MIN} min ni suppression validée ce mois. Bonne nouvelle !",
    )


def normaliser_champ_profil(champ):
    return CHAMPS_PROFIL_AUTORISES.get(str(champ).strip())


def barre_progression_gf(total_retard, total_suppr, largeur=12):
    """Barre visuelle Telegram vers les seuils GF (retard + suppressions)."""
    pct_r = min(100, int(100 * total_retard / SEUIL_GF_RETARD_MIN)) if SEUIL_GF_RETARD_MIN else 0
    pct_s = min(100, int(100 * total_suppr / SEUIL_GF_SUPPR)) if SEUIL_GF_SUPPR else 0
    pct = max(pct_r, pct_s)
    rempli = int(largeur * pct / 100)
    barre = "█" * rempli + "░" * (largeur - rempli)
    return f"`{barre}` {pct}%"


def libelle_mois(code_yyyy_mm):
    try:
        y, m = code_yyyy_mm.split("-")
        return f"{MOIS_FR[int(m) - 1]} {y}"
    except Exception:
        return code_yyyy_mm


def charger_utilisateur(id_telegram):
    """Charge un seul profil (évite de relire toute la table inscriptions)."""
    try:
        conn = db_connect(timeout=10)
        row = conn.execute("""
            SELECT id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour, age,
                   jours_actifs, alertes_actives, vacances_jusquau
            FROM inscriptions WHERE id_telegram = ?
        """, (str(id_telegram),)).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    return {
        "id_telegram_propre": row[0], "nom": row[1], "prenom": row[2], "gare_depart": row[3],
        "heures": row[4], "gare_retour": row[5], "heures_retour": row[6], "age": row[7],
        "jours_actifs": row[8], "alertes_actives": row[9], "vacances_jusquau": row[10],
    }


def stats_rapport_db():
    """Statistiques globales pour admin / maintenance."""
    try:
        conn = db_connect(timeout=10)
        stats = {
            "inscrits": conn.execute("SELECT COUNT(*) FROM inscriptions").fetchone()[0],
            "alertes_on": conn.execute("SELECT COUNT(*) FROM inscriptions WHERE alertes_actives=1").fetchone()[0],
            "trajets_valides": conn.execute(
                "SELECT COUNT(*) FROM trajets_valides WHERE statut_validation LIKE 'valide%'"
            ).fetchone()[0],
            "checkin_jour": conn.execute(
                "SELECT COUNT(*) FROM trajets_valides WHERE date = date('now')"
            ).fetchone()[0],
            "historique": conn.execute("SELECT COUNT(*) FROM historique").fetchone()[0],
            "pending_rapports": conn.execute(
                "SELECT COUNT(*) FROM rapports_incidents WHERE statut='en_attente'"
            ).fetchone()[0],
        }
        mois = datetime.now(tz_paris).strftime("%Y-%m")
        stats["eligible_gf"] = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT id_telegram,
                       SUM(CASE WHEN statut_validation = 'valide_supprime' THEN 1 ELSE 0 END) AS suppr,
                       SUM(COALESCE(retard_arrivee_min, 0)) AS retard
                FROM trajets_valides WHERE date LIKE ?
                GROUP BY id_telegram
                HAVING suppr >= ? OR retard >= ?
            )
            """,
            (f"{mois}%", SEUIL_GF_SUPPR, SEUIL_GF_RETARD_MIN),
        ).fetchone()[0]
        stats["tv_mois"] = conn.execute(
            "SELECT COUNT(*) FROM trajets_valides WHERE date LIKE ?", (f"{mois}%",)
        ).fetchone()[0]
        stats["tv_valides_mois"] = conn.execute(
            "SELECT COUNT(*) FROM trajets_valides WHERE date LIKE ? AND statut_validation LIKE 'valide%'",
            (f"{mois}%",),
        ).fetchone()[0]
        stats["tv_en_attente_mois"] = conn.execute(
            "SELECT COUNT(*) FROM trajets_valides WHERE date LIKE ? AND statut_validation = 'en_attente'",
            (f"{mois}%",),
        ).fetchone()[0]
        stats["suppr_mois"] = conn.execute(
            "SELECT COUNT(*) FROM trajets_valides WHERE date LIKE ? AND statut_validation='valide_supprime'",
            (f"{mois}%",),
        ).fetchone()[0]
        stats["incidents_jour"] = conn.execute(
            "SELECT COUNT(*) FROM historique WHERE date = date('now')"
        ).fetchone()[0]
        stats["ocesn_restants"] = conn.execute(
            "SELECT COUNT(*) FROM historique WHERE COALESCE(numero_train,'') LIKE 'OCESN%'"
        ).fetchone()[0]
        stats["rap_valides"] = conn.execute(
            "SELECT COUNT(*) FROM rapports_incidents WHERE statut='valide'"
        ).fetchone()[0]
        conn.close()
        return stats
    except Exception:
        return {}


BETA_OBJECTIF = 25  # cible 20–30 testeurs Metz–Luxembourg


def texte_invitation_beta():
    """Message de partage pour la beta (bot + API)."""
    return (
        "Salut ! Je teste Bazo, un bot Telegram pour les usagers TER Metz–Luxembourg (beta). "
        "Il m'alerte des retards et suppressions en temps réel, et génère mon attestation PDF "
        "pour la Garantie Fiabilité SNCF — check-in le matin, validation le soir en 1 clic. "
        "Gratuit pour l'instant — cherche 20–30 testeurs : https://t.me/SbazoTrain_bot"
    )


def stats_beta():
    """KPI beta : inscrits, actifs 7 jours, progression vers l'objectif."""
    s = stats_rapport_db() or {}
    inscrits = int(s.get("inscrits", 0))
    actifs_7j = 0
    inscrits_7j = 0
    try:
        conn = db_connect(timeout=10)
        actifs_7j = conn.execute(
            """
            SELECT COUNT(DISTINCT id_telegram) FROM trajets_valides
            WHERE date >= date('now', '-7 days')
            """
        ).fetchone()[0]
        cols = {c[1] for c in conn.execute("PRAGMA table_info(inscriptions)").fetchall()}
        if "date_inscription" in cols:
            inscrits_7j = conn.execute(
                """
                SELECT COUNT(*) FROM inscriptions
                WHERE date_inscription >= datetime('now', '-7 days')
                """
            ).fetchone()[0]
        conn.close()
    except Exception:
        pass
    objectif = BETA_OBJECTIF
    pct = min(100, int(inscrits * 100 / objectif)) if objectif else 0
    return {
        "inscrits": inscrits,
        "inscrits_7j": inscrits_7j,
        "actifs_7j": actifs_7j,
        "checkin_jour": int(s.get("checkin_jour", 0)),
        "objectif": objectif,
        "objectif_min": 20,
        "objectif_max": 30,
        "progression_pct": pct,
        "atteint": inscrits >= 20,
    }


def lister_checkout_pending(id_telegram, date_j=None):
    """Trajets du jour en attente de check-out pour un utilisateur."""
    date_j = date_j or datetime.now(tz_paris).strftime("%Y-%m-%d")
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """
            SELECT id, numero_train, heure_theo, heure_arrivee_theo, sens,
                   gare_depart, gare_arrivee, destination, tranche_horaire
            FROM trajets_valides
            WHERE id_telegram = ? AND date = ? AND statut_validation = 'en_attente'
            ORDER BY heure_theo
            """,
            (str(id_telegram), date_j),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    return [
        {
            "id": r[0],
            "numero_train": r[1],
            "heure_theo": r[2],
            "heure_arrivee_theo": r[3],
            "sens": r[4],
            "gare_depart": r[5],
            "gare_arrivee": r[6],
            "destination": r[7],
            "tranche_horaire": r[8],
        }
        for r in rows
    ]


def bilan_mois_aligne_pdf(id_telegram, mois=None):
    """Bilan GF identique Mon Statut / PDF (radar + déclarations)."""
    if mois is None:
        mois = datetime.now(tz_paris).strftime("%Y-%m")
    try:
        from bot_interactif import assembler_incidents_pdf, bilan_depuis_incidents_pdf, charger_utilisateur
        user = charger_utilisateur(id_telegram) or {}
        incidents, rows = assembler_incidents_pdf(str(id_telegram), mois, user)
        return bilan_depuis_incidents_pdf(incidents, len(rows))
    except Exception:
        return bilan_mois_trajets_valides_db(id_telegram, mois)


def purger_fantomes_historique():
    """Supprime les faux positifs « train absent temps réel » de l'historique."""
    try:
        conn = db_connect(timeout=15)
        n = conn.execute(
            """
            DELETE FROM historique
            WHERE type = 'supprime'
              AND (
                motif LIKE '%absent des données temps réel%'
                OR motif LIKE '%probable suppression%'
              )
            """
        ).rowcount
        conn.commit()
        conn.close()
        return n
    except Exception:
        return 0


def nettoyer_lignes_parasites_ocesn():
    """Supprime les lignes GTFS parasites OCESN (hors axes TER locaux). Retourne nb supprimées."""
    try:
        conn = db_connect(timeout=15)
        n_hist = conn.execute(
            "DELETE FROM historique WHERE COALESCE(numero_train,'') LIKE 'OCESN%'"
        ).rowcount
        conn.commit()
        conn.close()
        return n_hist
    except Exception:
        return 0
