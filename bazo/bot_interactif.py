import telebot
from telebot import types
import json
import os
import sqlite3
import uuid
import random
import re
import logging
import threading
import time
import subprocess
import requests
from fpdf import FPDF
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bazo_config import (
    ADMIN_CHAT_ID, API_BASE_URL, DB_PATH, TOKEN_SNCF, TOKEN_TELEGRAM, db_connect, log_quota_sncf, make_session,
)
from bazo_metier import (
    SEUIL_GF_RETARD_MIN,
    SEUIL_GF_SUPPR,
    SEUIL_RETARD_MIN,
    MOIS_FR,
    MOTIF_ALERTE_RADAR,
    MOTIF_RUPTURE_CHARGE,
    AXES,
    TOUTES_LES_GARES,
    barre_progression_gf,
    bilan_mois_trajets_valides_db,
    calculer_retard_arrivee,
    calculer_sens,
    charger_utilisateur,
    creer_inscription_si_absent,
    eligibilite_gf_message,
    est_annulation,
    format_duree_retard,
    get_index_gare,
    h_dans_tranche,
    inferer_type_reclamation,
    incident_pertinent_profil,
    libelle_mois,
    activer_surveillance_checkin,
    promouvoir_trajet_signalement,
    retard_reel_scanner_db,
    sync_trajets_valides_depuis_radar,
    reparer_tous_utilisateurs_mois,
    lier_checkins_retards_periode,
    train_sur_axes_ter,
    verifier_coherence_alerte_pdf,
    mettre_a_jour_inscription,
    normaliser_champ_profil,
    profil_gares_configure,
    stats_rapport_db,
    utilisateur_peut_recevoir_alertes,
    accepter_proposition_radar,
    libelle_trajet_pdf,
    lister_propositions_radar,
    migrer_schema_phase1,
    migrer_schema_phase2,
    motif_pdf_officiel,
    MENTION_COMMUNAUTAIRE,
    TYPES_SIGNALEMENT,
    LIBELLE_PROVENANCE,
    PROVENANCE_VOYAGEUR,
    charger_signalement,
    creer_signalement,
    expirer_signalements,
    libelle_type_signalement,
    signalements_du_train,
    voter_signalement,
    nom_canonique_gare,
    proposer_trajet_radar,
    refuser_proposition_radar,
)

TRAINS_PAR_PAGE = 8
BOUTON_AIDE = "📖 Aide"
BOUTON_ANNULER = "❌ Annuler"
BOUTON_MENU = "🏠 Menu"
HINT_ANNULER = "Appuie sur *❌ Annuler* en bas de l'écran pour revenir au menu."
TOUS_BOUTONS_MENU = frozenset({
    "📊 Mon Statut", "📂 Mon PDF Instantané", "🚆 Déclarer mon trajet",
    "⚠️ Signaler un incident", "🔴 Trafic radar", "💸 Bazo - Ton Allié Réclamation",
    "⚙️ Configuration Aller-Retour", "📅 Jours & Vacances", "✏️ Modifier mon profil",
    "📢 Partager à un ami", "💬 Contact / Bug", BOUTON_AIDE, BOUTON_ANNULER, BOUTON_MENU,
})

# ==========================================
# 1. CONFIGURATION
# ==========================================
TOKEN = TOKEN_TELEGRAM
tz_paris = ZoneInfo("Europe/Paris")
LIEN_SNCF_RECLAMATION = "https://www.sncf-voyageurs.com/fr/contactez-nous/demande-et-reclamation/"
def texte_formulaire_abo(mois_libelle):
    """Texte copier-coller SNCF — Garantie Fiabilité TER Grand Est (abonnement)."""
    return (
        f"Bonjour, en tant qu'abonné TER sur les lignes du réseau Grand Est, je sollicite "
        f"l'application de la Garantie Fiabilité pour le mois de {mois_libelle}. "
        f"Vous trouverez ci-joint l'attestation PDF de mes incidents de transport "
        f"(retards à l'arrivée ≥ 10 minutes et suppressions) établie à partir de mes trajets "
        f"déclarés. Je reste disponible pour tout complément d'information."
    )

def texte_formulaire_billet():
    """Texte copier-coller SNCF — billet TER à l'unité (retard/suppression)."""
    return (
        "Bonjour, suite aux perturbations constatées sur mon trajet TER "
        "(détail dans l'attestation PDF Bazo ci-jointe), je sollicite "
        "l'indemnisation prévue par les conditions générales de vente TER "
        "pour retard significatif à l'arrivée ou suppression de train. "
        "Je reste disponible pour tout complément d'information."
    )
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger_checkin = logging.getLogger("bazo.checkin")
bot = telebot.TeleBot(TOKEN, num_threads=8)

# Session HTTP partagée (keep-alive + retries) pour tous les appels API SNCF.
HTTP = make_session()

# Cache mémoire des horaires théoriques (réduit la latence + les appels Navitia répétés)
_cache_trains = {}
_cache_trains_lock = threading.Lock()
TTL_CACHE_TRAINS_SEC = 300

_etats_checkin = {}
_user_ctx = {}
_etats_lock = threading.Lock()
TTL_ETAT_SEC = 3600

def nettoyer_etat_utilisateur(chat_id):
    """Réinitialise états temporaires (équivalent context.user_data.clear())."""
    cid = str(chat_id)
    with _etats_lock:
        _etats_checkin.pop(cid, None)
        _user_ctx.pop(cid, None)
    try:
        bot.clear_step_handler_by_chat_id(int(chat_id))
    except Exception:
        pass

def ajouter_bouton_retour(markup):
    markup.add(types.InlineKeyboardButton("🔙 Retour au menu", callback_data="menu_principal"))
    return markup

def utilisateur_a_prenom(user):
    if not user:
        return False
    p = (user.get("prenom") or "").strip()
    return len(p) >= 2 and p.lower() != "voyageur"

def message_accueil(prenom=None):
    p = (prenom or "").strip()
    if p and p.lower() != "voyageur":
        return (
            f"👋 Bonjour *{p}* !\n"
            f"Bazo surveille tes trains TER et prépare ton dossier SNCF.\n\n"
            f"Que souhaites-tu faire aujourd'hui ?"
        )
    return (
        "👋 Bienvenue sur Bazo !\n"
        "Je t'aide à suivre tes trains et constituer ton dossier de réclamation.\n\n"
        "Que souhaites-tu faire aujourd'hui ?"
    )

def message_guide_client():
    """Guide rapide — surveillance créneaux 2h aller/retour."""
    return (
        "📖 *Comment utiliser Bazo (2 min)*\n\n"
        "⚙️ *Configure* tes gares + créneaux *2h* (aller / retour).\n"
        "   → Alertes et PDF sur *tes* horaires (+ 1 h de marge pour l'alerte).\n"
        "   → Gares intermédiaires sur ton axe (ex. Hagondange) incluses.\n\n"
        "🚆 *Optionnel* : Déclarer mon trajet → sens puis train.\n\n"
        "📂 *Mon PDF* : 100 % automatique (radar + tes déclarations).\n"
        "   Choisis le mois → PDF + texte SNCF envoyés directement.\n\n"
        "📍 *Signaler un incident* → sens, train, type (liste fermée), photo facultative ; "
        "les autres voyageurs du train confirment.\n"
        "💸 *Réclamation* : texte SNCF prêt à copier-coller.\n\n"
        f"💡 *Garantie Fiabilité TER* : {SEUIL_GF_RETARD_MIN} min cumulées OU {SEUIL_GF_SUPPR} suppressions/mois.\n"
        f"Bouton *{BOUTON_ANNULER}* ou *{BOUTON_MENU}* pour revenir au menu."
    )

def envoyer_menu_accueil(chat_id, user=None, edit_message_id=None):
    texte = message_accueil(user.get("prenom") if user else None)
    if edit_message_id:
        try:
            bot.edit_message_text("🏠 *Menu principal*", chat_id, edit_message_id, parse_mode="Markdown")
        except Exception:
            pass
    bot.send_message(chat_id, texte, reply_markup=menu_principal(), parse_mode="Markdown")

def sanitiser_chaine(texte):
    if texte is None:
        return ""
    t = str(texte).replace("\n", " ").replace("\r", " ").replace("\t", " ")
    t = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", t)
    return " ".join(t.split()).strip()

def clean_text(text):
    """Prépare le texte PDF — conserve les accents (latin-1 SNCF)."""
    if not text:
        return ""
    return sanitiser_chaine(text).encode("latin-1", "replace").decode("latin-1")

def _sens_depuis_incident_pdf(user, inc):
    """Déduit aller/retour depuis la ligne trajet d'un incident PDF."""
    trajet = str(inc.get("trajet") or "")
    if " -> " not in trajet:
        return None
    g_dep = trajet.split(" -> ")[0].strip()
    for sens in ("aller", "retour"):
        gd, _ = gares_pour_sens(user, sens)
        if _gare_correspond(g_dep, gd):
            return sens
    return None
try:
    conn = db_connect(timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(inscriptions)")
    cols = [c[1] for c in cursor.fetchall()]
    if "prenom" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN prenom TEXT DEFAULT 'Voyageur';")
    if "age" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN age TEXT DEFAULT 'Non précisé';")
    if "jours_actifs" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN jours_actifs TEXT DEFAULT '0,1,2,3,4';")
    if "alertes_actives" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN alertes_actives INTEGER DEFAULT 1;")
    if "vacances_jusquau" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN vacances_jusquau TEXT;")
    if "api_token" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN api_token TEXT;")
    if "date_inscription" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN date_inscription TEXT;")
    from bazo_metier import migrer_colonne_api_token, migrer_colonne_date_inscription
    migrer_colonne_api_token()
    migrer_colonne_date_inscription()
    cursor.execute("PRAGMA table_info(historique)")
    hcols = [c[1] for c in cursor.fetchall()]
    if "retard_depart_min" not in hcols:
        cursor.execute("ALTER TABLE historique ADD COLUMN retard_depart_min INTEGER;")
    if "retard_arrivee_min" not in hcols:
        cursor.execute("ALTER TABLE historique ADD COLUMN retard_arrivee_min INTEGER;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS surveillance_trains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            numero_train TEXT NOT NULL,
            gare_depart TEXT NOT NULL,
            gare_arrivee TEXT NOT NULL,
            destination TEXT,
            heure_theo TEXT,
            retard_depart_min INTEGER,
            retard_arrivee_min INTEGER,
            statut TEXT DEFAULT 'surveillance',
            motif TEXT,
            UNIQUE(date, numero_train, gare_depart)
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trajets_valides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_telegram TEXT NOT NULL,
            numero_train TEXT NOT NULL,
            date TEXT NOT NULL,
            heure_theo TEXT NOT NULL,
            heure_arrivee_theo TEXT,
            heure_reelle_arrivee TEXT,
            statut_validation TEXT DEFAULT 'en_attente',
            tranche_horaire TEXT,
            sens TEXT,
            gare_depart TEXT,
            gare_arrivee TEXT,
            destination TEXT,
            retard_arrivee_min INTEGER,
            motif_validation TEXT,
            UNIQUE(id_telegram, date, numero_train, heure_theo)
        );
    """)
    cursor.execute("PRAGMA table_info(trajets_valides)")
    _tvcols = [c[1] for c in cursor.fetchall()]
    if "heure_arrivee_theo" not in _tvcols:
        cursor.execute("ALTER TABLE trajets_valides ADD COLUMN heure_arrivee_theo TEXT;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rapports_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            heure TEXT,
            id_telegram TEXT NOT NULL,
            prenom TEXT,
            message TEXT,
            statut TEXT DEFAULT 'en_attente',
            retard_min INTEGER DEFAULT 0,
            traite_par TEXT,
            date_traitement TEXT
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_user_date ON trajets_valides(id_telegram, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_date ON trajets_valides(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hist_date ON historique(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_surv_date ON surveillance_trains(date, statut)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rap_user_statut ON rapports_incidents(id_telegram, statut)")
    cursor.execute("PRAGMA table_info(rapports_incidents)")
    rcols = [c[1] for c in cursor.fetchall()]
    for col, typedef in (
        ("type_signalement", "TEXT"),
        ("photo_file_id", "TEXT"),
        ("numero_train", "TEXT"),
        ("gare", "TEXT"),
        ("id_trajet", "INTEGER"),
    ):
        if col not in rcols:
            cursor.execute(f"ALTER TABLE rapports_incidents ADD COLUMN {col} {typedef};")
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    conn.commit()
    conn.close()
    migrer_schema_phase1()
    migrer_schema_phase2(retirer_tables_legacy=True)
except Exception as e:
    logging.error(f"[INIT] Migration schéma DB échouée : {e}")

# GESTION PDF ATTESTATION
class PDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 14)
        self.set_text_color(0, 85, 164)
        self.cell(0, 10, clean_text('Attestation Mensuelle de Retards Personnalisée'), 0, 1, 'C')
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(100, 100, 100)
        date_str = datetime.now().strftime("%d/%m/%Y")
        txt = (
            f'Attestation usager - Generee le {date_str} via Bazo '
            f'(service tiers independant, non affilie SNCF)'
        )
        self.cell(0, 10, clean_text(txt), 0, 0, 'C')

def charger_inscriptions():
    conn = db_connect(timeout=10)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour, age,
               jours_actifs, alertes_actives, vacances_jusquau
        FROM inscriptions
    """)
    utilisateurs = [{
        "id_telegram_propre": r[0], "nom": r[1], "prenom": r[2], "gare_depart": r[3], "heures": r[4],
        "gare_retour": r[5], "heures_retour": r[6], "age": r[7], "jours_actifs": r[8],
        "alertes_actives": r[9], "vacances_jusquau": r[10],
    } for r in cursor.fetchall()]
    conn.close()
    return utilisateurs

def sauvegarder_inscriptions(data):
    conn = db_connect(timeout=10)
    cursor = conn.cursor()
    for u in data:
        cursor.execute("""
            INSERT INTO inscriptions (id_telegram, nom, prenom, gare_depart, heures, gare_retour, heures_retour, age,
                jours_actifs, alertes_actives, vacances_jusquau)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_telegram) DO UPDATE SET
                nom=excluded.nom, prenom=excluded.prenom, gare_depart=excluded.gare_depart, heures=excluded.heures,
                gare_retour=excluded.gare_retour, heures_retour=excluded.heures_retour, age=excluded.age,
                jours_actifs=excluded.jours_actifs, alertes_actives=excluded.alertes_actives,
                vacances_jusquau=excluded.vacances_jusquau
        """, (str(u["id_telegram_propre"]), u["nom"], u.get("prenom", "Voyageur"), u["gare_depart"], u["heures"],
              u.get("gare_retour", "Non precise"), u.get("heures_retour", "Non precise"), u.get("age", "Non précisé"),
              u.get("jours_actifs", "0,1,2,3,4"), u.get("alertes_actives", 1), u.get("vacances_jusquau")))
    conn.commit()
    conn.close()

# ==========================================
# 2. LES MENUS
# ==========================================
def menu_principal():
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add("📊 Mon Statut", "📂 Mon PDF Instantané")
    markup.add("🚆 Déclarer mon trajet", "⚠️ Signaler un incident")
    markup.add("🔴 Trafic radar", "💸 Bazo - Ton Allié Réclamation")
    markup.add("⚙️ Configuration Aller-Retour", "📅 Jours & Vacances")
    markup.add("✏️ Modifier mon profil", "📢 Partager à un ami")
    markup.add("💬 Contact / Bug", BOUTON_AIDE)
    markup.add(BOUTON_ANNULER, BOUTON_MENU)
    return markup


def annuler_et_revenir_menu(chat_id):
    """Retour menu — bouton ❌ Annuler / 🏠 Menu (pas de commande à taper)."""
    nettoyer_etat_utilisateur(chat_id)
    user = charger_utilisateur(chat_id)
    bot.send_message(
        chat_id,
        f"↩️ Retour au menu.\n\n{message_accueil(user.get('prenom') if user else None)}",
        reply_markup=menu_principal(),
        parse_mode="Markdown",
    )


def debut_inscription(chat_id, utilisateur_tg=None):
    """Première utilisation : prénom uniquement, tout se fait par boutons ensuite."""
    set_etat_checkin(str(chat_id), {"phase": "onboarding_prenom"})
    bot.send_message(
        chat_id,
        "👋 *Bienvenue sur Bazo !*\n\n"
        "🧪 *Beta Metz–Lux* — service gratuit pour usagers TER.\n\n"
        "Comment t'appelles-tu ? (écris ton prénom)\n\n"
        f"{HINT_ANNULER}",
        reply_markup=menu_principal(),
        parse_mode="Markdown",
    )


def utilisateur_doit_sinscrire(id_user, chat_id, utilisateur_tg=None):
    """True si inscription lancée (utilisateur absent)."""
    user = charger_utilisateur(id_user)
    if user and utilisateur_a_prenom(user):
        return False
    debut_inscription(chat_id, utilisateur_tg)
    return True

# --- Grille horaire SNCF (3 modèles) ---
TRANCHES_PAR_MODELE = {
    "semaine": ["04h-06h", "06h-08h", "08h-10h", "10h-12h", "12h-14h", "14h-16h", "16h-18h", "18h-20h", "20h-22h"],
    "samedi": ["06h-08h", "08h-10h", "10h-12h", "12h-14h", "14h-16h", "16h-18h", "18h-20h", "20h-22h"],
    "dimanche": ["08h-10h", "10h-12h", "12h-14h", "14h-16h", "16h-18h", "18h-20h"],
}
TRANCHE_JOURNEE_COMPLETE = "04h-22h"

def modele_grille_jour(now=None):
    if now is None:
        now = datetime.now(tz_paris)
    wd = now.weekday()
    if wd == 5:
        return "samedi"
    if wd == 6:
        return "dimanche"
    return "semaine"

def stop_area_pour_gare(nom_gare):
    from bazo_geo import stop_area_pour_gare as _geo_stop
    return _geo_stop(nom_gare)

def resoudre_stop_area(nom_gare):
    from bazo_geo import resoudre_stop_area as _geo_resoudre
    return _geo_resoudre(
        nom_gare, http_get=HTTP.get, token_sncf=TOKEN_SNCF, log=logging.getLogger("bazo.checkin"),
    )

def gares_pour_sens(user, sens):
    """Aller = domicile (gare_depart) → travail (gare_retour). Retour = inverse."""
    if not user:
        return None, None
    if sens == "aller":
        return user.get("gare_depart"), user.get("gare_retour")
    return user.get("gare_retour"), user.get("gare_depart")

def _gare_correspond(nom_a, nom_b):
    if not nom_a or not nom_b:
        return False
    a = str(nom_a).lower().replace("-", " ").strip()
    b = str(nom_b).lower().replace("-", " ").strip()
    if a in b or b in a:
        return True
    return get_index_gare(nom_a) == get_index_gare(nom_b) and get_index_gare(nom_a) != -1

def datetimes_pour_tranche(tranche, date_ref=None):
    """Plusieurs horaires de requête Navitia (début, milieu, fin de tranche)."""
    if not tranche or tranche in ("Toute la journee", "Non precise"):
        heures = list(range(5, 22, 2))
    else:
        try:
            h_debut = int(tranche.split("-")[0].replace("h", "").strip())
            h_fin = int(tranche.split("-")[1].replace("h", "").strip())
            if h_fin - h_debut >= 6:
                heures = sorted({h_debut, (h_debut + h_fin) // 2, h_fin - 1, h_debut + 2, h_fin - 3})
            else:
                heures = sorted({h_debut, (h_debut + h_fin) // 2, max(h_debut, h_fin - 1)})
        except Exception:
            heures = [7, 8, 9]
    if date_ref is None:
        ref = datetime.now(tz_paris)
        ajouter_jour_si_passe = True
    elif isinstance(date_ref, str):
        ref = datetime.strptime(date_ref, "%Y-%m-%d").replace(tzinfo=tz_paris)
        ajouter_jour_si_passe = False
    else:
        ref = date_ref
        ajouter_jour_si_passe = False
    now = datetime.now(tz_paris)
    result = []
    for h in heures:
        dt = ref.replace(hour=h, minute=0, second=0, microsecond=0)
        if ajouter_jour_si_passe and dt < now - timedelta(hours=2):
            dt += timedelta(days=1)
        result.append(dt.strftime("%Y%m%dT%H%M%S"))
    return result


def _normaliser_ville_inc(nom):
    nom = str(nom).replace("\n", " ").replace("\r", "").strip()
    if "Luxemb" in nom:
        return "Luxembourg"
    return nom


def deduire_destination_finale_inc(gare_raw, dest_raw, sens_aller):
    dest_finale = str(dest_raw).split("(")[0].strip()
    if dest_finale == gare_raw or len(dest_finale) < 3:
        for _nom_axe, gares in AXES.items():
            if gare_raw.lower() in [g.lower() for g in gares]:
                dest_finale = gares[-1].title() if sens_aller == 1 else gares[0].title()
                break
    if dest_finale == gare_raw:
        dest_finale = "Fin de ligne"
    return dest_finale


def sens_et_tranche_profil(user, gare_raw, dest_raw, h_theo_raw, type_incident="retard"):
    """Quel sens (aller/retour) et créneau 2h matchent cette perturbation."""
    return incident_pertinent_profil(
        gare_raw, dest_raw, h_theo_raw,
        user.get("gare_depart"), user.get("gare_retour"),
        user.get("heures"), user.get("heures_retour"),
        type_incident=type_incident,
        pour_alerte=False,
    )


def incident_correspond_profil(user, gare_raw, dest_raw, h_theo_raw, type_incident="retard"):
    """Perturbation sur l'axe + dans le créneau 2h configuré (aller ou retour)."""
    sens, _ = sens_et_tranche_profil(user, gare_raw, dest_raw, h_theo_raw, type_incident=type_incident)
    return sens is not None


def _train_sur_axes_ter(numero_train):
    return train_sur_axes_ter(numero_train)


def _trajet_deja_dans_pdf(id_tg, date_j, num, h_theo, cles_existantes):
    cle = f"{date_j}_{num}_{h_theo}"
    if cle in cles_existantes:
        return True
    try:
        conn = db_connect(timeout=5)
        row = conn.execute(
            """
            SELECT 1 FROM trajets_valides
            WHERE id_telegram = ? AND date = ? AND numero_train = ? AND heure_theo = ?
              AND statut_validation != 'annule'
            """,
            (str(id_tg), date_j, str(num), str(h_theo)),
        ).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


def _formater_ligne_incident_pdf(date_j, num, g_dep, dest, h_theo, motif_pdf, type_inc, retard=0):
    """Ligne d'incident PDF normalisée (aller + retour, toutes tranches profil)."""
    if "Luxemb" in dest:
        dest = "Luxembourg"
    if "Luxemb" in g_dep:
        g_dep = "Luxembourg"
    trajet_final = f"{g_dep} -> {dest}" if g_dep and dest else (g_dep or dest or "Trajet")
    return {
        "date": date_j,
        "train": str(num),
        "trajet": trajet_final,
        "heure": str(h_theo),
        "motif": sanitiser_chaine(motif_pdf)[:60],
        "retard_txt": "Supprimé" if type_inc == "supprime" else f"+{retard} min",
        "retard": 0 if type_inc == "supprime" else int(retard),
        "type": type_inc,
        "_auto_radar": True,
    }


def incidents_radar_creneaux_pdf(user, mois, incidents_existants=None):
    """
    Perturbations radar (suppressions + retards) alignées alertes/PDF :
    - Suppressions : gare de départ usager, créneau aller OU retour
    - Retards : même filtre que les alertes profil (axe + créneau 2h)
    """
    id_tg = str(user.get("id_telegram_propre") or user.get("id_telegram") or "")
    if not id_tg:
        return {}
    incidents_existants = incidents_existants or {}
    cles_existantes = set(incidents_existants.keys())
    out = {}
    greve_par_date = {}
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """
            SELECT date, numero_train, type, gare, COALESCE(ligne, gare), heure_theo, motif,
                   MAX(COALESCE(retard_arrivee_min, retard_depart_min, retard_min, 0))
            FROM historique
            WHERE date LIKE ? AND type IN ('supprime', 'retard')
              AND numero_train NOT LIKE 'OCESN%'
            GROUP BY date, numero_train, type, gare, ligne, heure_theo, motif
            """,
            (f"{mois}%",),
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    for date_j, num, typ, gare, dest, h_theo, motif_hist, retard_hist in rows:
        if not _train_sur_axes_ter(num):
            continue
        typ = str(typ).lower()
        cle = f"{date_j}_{num}_{h_theo}"
        if _trajet_deja_dans_pdf(id_tg, date_j, num, h_theo, cles_existantes):
            continue
        if typ == "supprime":
            sens, _tr = sens_et_tranche_profil(user, gare, dest, h_theo, type_incident="supprime")
            if not sens:
                continue
            g_dep, _g_arr = gares_pour_sens(user, sens)
            greve_par_date[date_j] = greve_par_date.get(date_j, 0) + 1
            motif_pdf = motif_hist or "Train supprimé (annulation SNCF)"
            if greve_par_date[date_j] >= 3 and "grève" not in motif_pdf.lower() and "greve" not in motif_pdf.lower():
                motif_pdf = "Grève / mouvement social — train supprimé"
            out[cle] = _formater_ligne_incident_pdf(
                date_j, num, g_dep, dest, h_theo, motif_pdf, "supprime",
            )
        elif typ == "retard":
            retard = int(retard_hist or 0)
            if retard < SEUIL_RETARD_MIN:
                continue
            if not incident_correspond_profil(user, gare, dest, h_theo):
                continue
            sens, _tr = sens_et_tranche_profil(user, gare, dest, h_theo, type_incident="retard")
            g_dep, _g_arr = gares_pour_sens(user, sens) if sens else (gare, dest)
            motif_pdf = motif_hist or f"Retard détecté par le radar (+{retard} min)"
            out[cle] = _formater_ligne_incident_pdf(
                date_j, num, g_dep or gare, dest, h_theo, motif_pdf, "retard", retard,
            )
    return out


def incidents_suppressions_creneau_pdf(user, mois, incidents_existants=None):
    """Alias rétrocompat — suppressions uniquement."""
    return {
        k: v for k, v in incidents_radar_creneaux_pdf(user, mois, incidents_existants).items()
        if v.get("type") == "supprime"
    }


def auto_completer_trajets_radar_mois(id_telegram, mois, user=None):
    """
    Règle R3 : le radar ne fabrique plus de ligne d'attestation. Pour chaque (jour, sens)
    du créneau usager où le radar a vu un incident sans check-in correspondant, on crée
    une proposition « j'étais dans ce train » (table propositions_radar).
    Retourne le nombre de propositions créées.
    """
    if user is None:
        user = charger_utilisateur(id_telegram) or {}
    id_tg = str(id_telegram)
    if not id_tg or not mois:
        return 0
    n = 0
    try:
        migrer_schema_phase1()
        conn = db_connect(timeout=15)
        # Jamais de proposition pour un jour antérieur à l'inscription de l'usager.
        r_insc = conn.execute(
            "SELECT substr(date_inscription, 1, 10) FROM inscriptions WHERE id_telegram = ?", (id_tg,),
        ).fetchone()
        date_inscription = str((r_insc[0] if r_insc else None) or user.get("date_inscription") or "")[:10]
        rows = conn.execute(
            """
            SELECT date, numero_train, type, gare, COALESCE(ligne, gare), heure_theo, motif,
                   MAX(COALESCE(retard_arrivee_min, retard_depart_min, retard_min, 0))
            FROM historique
            WHERE date LIKE ? AND type IN ('retard', 'supprime')
              AND numero_train NOT LIKE 'OCESN%'
            GROUP BY date, numero_train, type, gare, ligne, heure_theo, motif
            """,
            (f"{mois}%",),
        ).fetchall()
        candidats = {}
        for date_j, num, typ, gare, dest, h_theo, motif, retard in rows:
            if date_inscription and str(date_j) < date_inscription:
                continue
            if not _train_sur_axes_ter(num):
                continue
            if not incident_correspond_profil(user, gare, dest, h_theo, type_incident=typ):
                continue
            sens, tranche = sens_et_tranche_profil(user, gare, dest, h_theo, type_incident=typ)
            if not sens:
                continue
            typ = str(typ).lower()
            retard = int(retard or 0)
            if typ == "retard" and retard < SEUIL_RETARD_MIN:
                continue
            cle = (date_j, sens)
            score = (2 if typ == "supprime" else 1, retard, str(h_theo))
            cur = candidats.get(cle)
            if cur and score <= cur["score"]:
                continue
            g_dep, g_arr = gares_pour_sens(user, sens)
            candidats[cle] = {
                "score": score,
                "date": date_j,
                "sens": sens,
                "tranche": tranche or user.get("heures" if sens == "aller" else "heures_retour", ""),
                "num": str(num),
                "h_theo": str(h_theo),
                "g_dep": g_dep,
                "g_arr": g_arr,
                "dest": dest,
                "typ": typ,
                "retard": retard,
                "motif": motif or MOTIF_ALERTE_RADAR,
            }
        for info in candidats.values():
            date_j = info["date"]
            sens = info["sens"]
            row = conn.execute(
                """
                SELECT 1 FROM trajets_valides
                WHERE id_telegram = ? AND date = ? AND sens = ?
                  AND statut_validation IN (
                    'en_attente', 'valide_normal', 'valide_supprime', 'valide_bus_panne', 'valide_report'
                  )
                """,
                (id_tg, date_j, sens),
            ).fetchone()
            if row:
                continue
            row2 = conn.execute(
                """
                SELECT 1 FROM trajets_valides
                WHERE id_telegram = ? AND date = ? AND numero_train = ? AND heure_theo = ?
                  AND statut_validation != 'annule'
                """,
                (id_tg, date_j, info["num"], info["h_theo"]),
            ).fetchone()
            if row2:
                continue
            deja = conn.execute(
                "SELECT 1 FROM propositions_radar WHERE id_telegram=? AND date=? AND numero_train=? AND heure_theo=?",
                (id_tg, date_j, info["num"], info["h_theo"]),
            ).fetchone()
            if deja:
                continue
            pid = proposer_trajet_radar(
                id_tg, user,
                {
                    "date": date_j, "numero_train": info["num"], "heure_theo": info["h_theo"],
                    "gare": info["g_dep"], "destination": info["dest"], "type": info["typ"],
                    "retard_arrivee_min": info["retard"], "motif": info["motif"],
                },
                sens=sens, tranche=info["tranche"],
            )
            if pid:
                n += 1
        conn.close()
    except Exception as e:
        logging.warning("[PDF-AUTO] auto_completer_trajets_radar_mois : %s", e)
    return n


def preparer_dossier_pdf_mois(id_telegram, mois, user=None):
    """
    Qualifie les trajets déclarés avec le radar avant génération PDF (règle R8 :
    aucune création de trajet à la lecture). Les incidents radar sans check-in
    deviennent des propositions à confirmer, jamais des lignes d'attestation.
    """
    if user is None:
        user = charger_utilisateur(id_telegram) or {}
    auto_completer_trajets_radar_mois(id_telegram, mois, user)  # ne crée que des propositions
    lier_checkins_retards_periode(f"{mois}-01", f"{mois}-31")
    sync_trajets_valides_depuis_radar(id_telegram, mois)


def assembler_incidents_pdf(id_telegram, mois, user=None):
    """
    Point unique PDF / Mon Statut / API / mobile : trajets déclarés ou confirmés par
    l'usager, qualifiés par le radar (règles R1 à R9). Aucun client ne corrige après coup.
    """
    if user is None:
        user = charger_utilisateur(id_telegram) or {}
    preparer_dossier_pdf_mois(id_telegram, mois, user)
    rows_tv = charger_trajets_valides_pdf(id_telegram, mois)
    incidents = incidents_pdf_depuis_trajets_valides(rows_tv)
    incidents = filtrer_incidents_pdf_personne(user, incidents, id_telegram, mois)
    return incidents, rows_tv


def _gares_depuis_trajet_incident(inc):
    trajet = str(inc.get("trajet") or "")
    if " -> " in trajet:
        a, b = trajet.split(" -> ", 1)
        return a.strip(), b.strip()
    return "", ""


def _trajets_declares_pdf(id_telegram, mois, user):
    """(date, sens) -> {(numero_train, heure_theo), ...}"""
    declares = {}
    try:
        conn = db_connect(timeout=8)
        rows = conn.execute(
            """
            SELECT date, sens, numero_train, heure_theo, gare_depart, gare_arrivee
            FROM trajets_valides
            WHERE id_telegram = ? AND date LIKE ? AND statut_validation != 'annule'
            """,
            (str(id_telegram), f"{mois}%"),
        ).fetchall()
        conn.close()
    except Exception:
        return declares
    g_all = str(user.get("gare_depart") or "")
    for date_j, sens_db, num, h_theo, g_dep, g_arr in rows:
        sens = sens_db if sens_db in ("aller", "retour") else None
        if not sens:
            if g_all and g_dep:
                sens = "aller" if _gares_similaires(g_dep, g_all) else "retour"
            else:
                sens = "aller"
        declares.setdefault((str(date_j), sens), set()).add((str(num), str(h_theo)))
    return declares


def _train_declare_utilisateur(id_tg, date_j, num, h_theo):
    """True si l'usager a déclaré ce train (check-in) pour cette date."""
    if not id_tg:
        return False
    try:
        conn = db_connect(timeout=5)
        row = conn.execute(
            """
            SELECT 1 FROM trajets_valides
            WHERE id_telegram = ? AND date = ? AND numero_train = ?
              AND heure_theo = ? AND statut_validation != 'annule'
            """,
            (str(id_tg), str(date_j), str(num), str(h_theo)),
        ).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


def _sens_incident(user, inc):
    if inc.get("sens") in ("aller", "retour"):
        return inc["sens"]
    sens = _sens_depuis_incident_pdf(user, inc)
    if sens:
        return sens
    g_dep, _ = _gares_depuis_trajet_incident(inc)
    g_all = (user or {}).get("gare_depart")
    return "aller" if g_all and _gares_similaires(g_dep, g_all) else "retour"


def _incident_declare_par_usager(inc, declares, user):
    date_j = str(inc.get("date") or "")
    num = str(inc.get("train") or "")
    h = str(inc.get("heure") or "")
    sens = _sens_incident(user, inc)
    decl = declares.get((date_j, sens), set())
    return any(num == d[0] and h == d[1] for d in decl)


def filtrer_incidents_pdf_personne(user, incidents, id_telegram=None, mois=None):
    """
    PDF personnalisé :
    - Créneaux 2h du profil uniquement
    - Max 2 incidents/jour : 1 retard aller + 1 retard retour + suppressions radar auto
    """
    if not incidents:
        return {}
    id_tg = str(id_telegram or user.get("id_telegram_propre") or user.get("id_telegram") or "")
    declares = _trajets_declares_pdf(id_tg, mois, user) if id_tg and mois else {}

    candidats = {}
    for cle, inc in incidents.items():
        if not inc.get("_auto_radar"):
            candidats[cle] = inc
            continue
        # Lignes radar non déclarées (plus produites en production, règle R3) : filtre profil strict.
        g_dep, dest = _gares_depuis_trajet_incident(inc)
        h = str(inc.get("heure") or "")
        if not g_dep or not incident_correspond_profil(user, g_dep, dest, h or "08h00", type_incident=inc.get("type", "retard")):
            continue
        candidats[cle] = inc

    final = {}
    par_date = {}
    for cle, inc in candidats.items():
        par_date.setdefault(inc["date"], []).append((cle, inc))

    for _date_j, items in par_date.items():
        suppressions = [(c, i) for c, i in items if i.get("type") == "supprime"]
        retards = [(c, i) for c, i in items if i.get("type") != "supprime"]

        suppr_par_sens = {}
        for c, i in suppressions:
            sens = _sens_incident(user, i)
            score = (2 if _incident_declare_par_usager(i, declares, user) else 0, str(i.get("heure") or ""))
            cur = suppr_par_sens.get(sens)
            if cur is None or score > cur[0]:
                suppr_par_sens[sens] = (score, c, i)
        for _score, c, i in suppr_par_sens.values():
            final[c] = i

        retard_par_sens = {}
        for c, i in retards:
            sens = _sens_incident(user, i)
            cur = retard_par_sens.get(sens)
            if cur is None or int(i.get("retard") or 0) > int(cur[1].get("retard") or 0):
                retard_par_sens[sens] = (c, i)
        for c, i in retard_par_sens.values():
            final[c] = i

    return final


def bilan_depuis_incidents_pdf(incidents, nb_trajets_declares=0):
    """Bilan GF aligné sur le contenu réel du PDF (aller + retour, radar inclus)."""
    total_retard = 0
    total_suppr = 0
    nb_retards_eligibles = 0
    for inc in incidents.values():
        if inc.get("type") == "supprime":
            total_suppr += 1
        else:
            retard = int(inc.get("retard") or 0)
            if retard >= SEUIL_RETARD_MIN:
                total_retard += retard
                nb_retards_eligibles += 1
    nb_incidents = total_suppr + nb_retards_eligibles
    return total_retard, total_suppr, nb_incidents, nb_trajets_declares


def jours_a_completer_mois(user, mois):
    """
    Complétion manuelle désactivée — le radar remplit automatiquement le dossier PDF.
    Conservé pour compat tests ; retourne toujours [].
    """
    return []


def lister_mois_pdf_eligibles(id_telegram, user_data):
    """Mois avec incidents déclarés OU perturbations dans les créneaux 2h du profil."""
    mois_set = {m for m, _ in lister_mois_attestation(id_telegram, user_data)}
    try:
        conn = db_connect(timeout=10)
        mois_hist = conn.execute(
            """
            SELECT DISTINCT substr(date, 1, 7) FROM historique
            WHERE date >= date('now', '-4 months') AND numero_train NOT LIKE 'OCESN%'
            """,
        ).fetchall()
        conn.close()
        for (m,) in mois_hist:
            if not m:
                continue
            if incidents_radar_creneaux_pdf(user_data, m):
                mois_set.add(m)
    except Exception:
        pass
    resultat = []
    for m in sorted(mois_set, reverse=True):
        try:
            y, mo = m.split("-")
            lib = f"{MOIS_FR[int(mo) - 1]} {y}"
        except Exception:
            lib = m
        resultat.append((m, lib))
    return resultat


def menu_jours_completer(jours, mois):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for date_j, info in jours:
        sens_lbl = "Aller" if info.get("sens") == "aller" else "Retour"
        nb = info.get("nb", 1)
        if info.get("suppr"):
            lib = f"❌ {date_j} {sens_lbl} — {nb} train(s) supprimé(s)"
        else:
            lib = f"⏱ {date_j} {sens_lbl} — retard +{info.get('retard', '?')} min"
        sens_cb = info.get("sens", "")
        cb = f"comp_jour_{date_j}_{sens_cb}" if sens_cb else f"comp_jour_{date_j}"
        markup.add(types.InlineKeyboardButton(lib, callback_data=cb))
    markup.add(types.InlineKeyboardButton(
        "📂 Générer le PDF sans compléter", callback_data=f"comp_skip_{mois}",
    ))
    return ajouter_bouton_retour(markup)


def menu_sens_completer(user, date_j):
    g_dom = user.get("gare_depart", "?")
    g_trav = user.get("gare_retour", "?")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        f"➡️ Aller ({g_dom} → {g_trav}) — {user.get('heures', '?')}",
        callback_data=f"comp_sens_aller_{date_j}",
    ))
    markup.add(types.InlineKeyboardButton(
        f"⬅️ Retour ({g_trav} → {g_dom}) — {user.get('heures_retour', '?')}",
        callback_data=f"comp_sens_retour_{date_j}",
    ))
    return ajouter_bouton_retour(markup)


def menu_trains_completer(trains, page=0):
    markup = types.InlineKeyboardMarkup(row_width=1)
    debut = page * TRAINS_PAR_PAGE
    fin = debut + TRAINS_PAR_PAGE
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Précédent", callback_data=f"comp_page_{page - 1}"))
    if fin < len(trains):
        nav.append(types.InlineKeyboardButton(
            f"➡️ Suite ({len(trains) - fin})", callback_data=f"comp_page_{page + 1}",
        ))
    if nav:
        markup.row(*nav)
    for i, t in enumerate(trains[debut:fin]):
        idx = debut + i
        arr = t.get("heure_arrivee_theo")
        trajet_h = f"{t['heure_theo']}→{arr}" if arr else t["heure_theo"]
        lib = f"{t['numero_train']} · {trajet_h}"
        markup.add(types.InlineKeyboardButton(lib, callback_data=f"comp_pick_{idx}"))
    markup.add(types.InlineKeyboardButton("⏭ Passer ce jour", callback_data="comp_passer_jour"))
    return ajouter_bouton_retour(markup)

def menu_sens_checkin(user):
    g_dom = user.get("gare_depart", "?")
    g_trav = user.get("gare_retour", "?")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        f"➡️ Sens Aller ({g_dom} → {g_trav})",
        callback_data="ck_sens_aller",
    ))
    markup.add(types.InlineKeyboardButton(
        f"⬅️ Sens Retour ({g_trav} → {g_dom})",
        callback_data="ck_sens_retour",
    ))
    return ajouter_bouton_retour(markup)

def set_etat_checkin(chat_id, data, merge=False):
    with _etats_lock:
        cle = str(chat_id)
        if merge and cle in _etats_checkin:
            _etats_checkin[cle] = {**_etats_checkin[cle], **data, "ts": time.time()}
        else:
            _etats_checkin[cle] = {**data, "ts": time.time()}

def get_etat_checkin(chat_id):
    with _etats_lock:
        etat = _etats_checkin.get(str(chat_id))
        if not etat:
            return None
        if time.time() - etat.get("ts", 0) > TTL_ETAT_SEC:
            del _etats_checkin[str(chat_id)]
            return None
        return etat

def pop_etat_checkin(chat_id):
    with _etats_lock:
        return _etats_checkin.pop(str(chat_id), None)

def _section_est_liaison_ferree(section):
    """Exclut TGV/OUIGO/autocar uniquement — pas de filtre sur le nom TER."""
    if section.get("type") != "public_transport":
        return False
    info = section.get("display_informations", {})
    blob = " ".join([
        str(info.get("commercial_mode", "")),
        str(info.get("network", "")),
        str(info.get("physical_mode", "")),
        str(info.get("name", "") or info.get("label", "")),
    ]).upper()
    # Ne pas utiliser "INTERCIT" seul : il matche "Intercités" des TER régionaux (FLUO TER / Intercités).
    if any(x in blob for x in ["TGV", "OUIGO", "EUROSTAR", "LYRIA", "ICE "]):
        return False
    if re.search(r"\bINTERCIT[EÉ]S\b", blob) and "TER" not in blob and "FLUO" not in blob:
        return False
    if "AUTOCAR" in blob or ("BUS" in blob and "TRAIN" not in blob):
        return False
    return True

def _heure_depuis_navitia(h_str):
    if not h_str or len(h_str) < 15:
        return None
    return datetime.strptime(h_str, "%Y%m%dT%H%M%S").strftime("%Hh%M")

def _minutes_heure_label(heure_label):
    """Convertit '07h03' en minutes pour tri / comparaison."""
    try:
        parties = str(heure_label).lower().replace("h", ":").split(":")
        return int(parties[0]) * 60 + int(parties[1])
    except Exception:
        return 24 * 60

def _heure_depart_origine(section, dep_journey, tranche):
    """Heure de départ la plus tôt à la gare d'origine (journey + 1er arrêt), dans la tranche."""
    candidats = []
    h_j = _heure_depuis_navitia(dep_journey)
    if h_j and h_dans_tranche(h_j, tranche):
        candidats.append(h_j)
    arrets = section.get("stop_date_times", [])
    if arrets:
        for cle_dt in ("departure_date_time", "base_departure_date_time"):
            h = _heure_depuis_navitia(arrets[0].get(cle_dt))
            if h and h_dans_tranche(h, tranche):
                candidats.append(h)
    if not candidats:
        return None
    return min(candidats, key=_minutes_heure_label)

def _heure_arrivee_destination(section, arr_journey=None):
    """Heure d'arrivée THÉORIQUE au terminus de la section (gare de destination de la recherche)."""
    arrets = section.get("stop_date_times", [])
    if arrets:
        dernier = arrets[-1]
        for cle_dt in ("base_arrival_date_time", "arrival_date_time"):
            h = _heure_depuis_navitia(dernier.get(cle_dt))
            if h:
                return h
    return _heure_depuis_navitia(arr_journey)

def _dedoublonner_trains_par_numero(trains):
    """Un seul enregistrement par numéro de train — heure de départ minimale à l'origine."""
    par_numero = {}
    for t in trains:
        numero = t.get("numero_train") or "Train"
        if numero not in par_numero:
            par_numero[numero] = t
            continue
        if _minutes_heure_label(t["heure_theo"]) < _minutes_heure_label(par_numero[numero]["heure_theo"]):
            par_numero[numero] = t
    resultat = list(par_numero.values())
    resultat.sort(key=lambda x: _minutes_heure_label(x["heure_theo"]))
    return resultat

def _parser_trains_de_journeys(donnees, tranche, gare_dep, gare_arr):
    bruts = []
    nb_journeys = len(donnees.get("journeys", []))
    logging.info(f"[CHECKIN] Parsing {nb_journeys} journeys pour tranche {tranche}")
    for journey in donnees.get("journeys", []):
        statut_j = str(journey.get("status", "") or "").lower()
        if statut_j == "deleted":
            continue
        # Train supprimé ce jour-là (grève, incident) : il reste proposé — c'est le train théorique de l'usager.
        supprime_j = statut_j in ("cancelled", "no_service")
        dep_j = journey.get("departure_date_time") or journey.get("requested_date_time")
        sections_pt = [s for s in journey.get("sections", []) if _section_est_liaison_ferree(s)]
        if not sections_pt:
            continue
        section = sections_pt[0]
        info = section.get("display_informations", {})
        numero = sanitiser_chaine(
            info.get("headsign") or info.get("trip_short_name") or info.get("code") or "Train"
        )
        h_theo = _heure_depart_origine(section, dep_j, tranche)
        if not h_theo:
            continue
        arr_j = journey.get("arrival_date_time")
        h_arrivee_theo = _heure_arrivee_destination(section, arr_j)
        # Direction commerciale du train (terminus affiché, ex. « Nancy ») : information
        # d'affichage uniquement. Le segment usager reste gare_dep -> gare_arr (règles R1, R2).
        direction = sanitiser_chaine(info.get("direction", "") or gare_arr)
        arrets = section.get("stop_date_times", [])
        gare_board = sanitiser_chaine(
            (arrets[0].get("stop_point", {}) if arrets else {}).get("name", gare_dep)
        )
        if gare_board and gare_board != gare_dep:
            logging.debug(f"[CHECKIN] embarquement Navitia « {gare_board} » pour gare demandée « {gare_dep} »")
        bruts.append({
            "numero_train": numero,
            "heure_theo": h_theo,
            "heure_arrivee_theo": h_arrivee_theo,
            "destination": direction,
            "direction_commerciale": direction,
            "gare_depart": nom_canonique_gare(gare_dep) or gare_dep,
            "gare_arrivee": nom_canonique_gare(gare_arr) or gare_arr,
            "supprime": supprime_j,
        })
    trains = _dedoublonner_trains_par_numero(bruts)
    logging.info(f"[CHECKIN] {len(bruts)} segments bruts → {len(trains)} trains uniques après dédoublonnage")
    return trains

def _bornes_tranche_min(tranche):
    """(début, fin) en minutes depuis minuit ; journée entière si tranche absente ou illisible."""
    if not tranche or tranche in ("Toute la journee", "Non precise"):
        return 0, 24 * 60
    try:
        d, f = str(tranche).replace("h", "").split("-")[:2]
        return int(d) * 60, int(f) * 60
    except Exception:
        return 0, 24 * 60


def _journeys_navitia(sid_dep, sid_arr, dt, freshness="base_schedule", direct=True):
    """Un appel Navitia (compté dans le quota). Retourne la liste JSON ou None si erreur."""
    from bazo_quota import RESERVE_CHECKIN, peut_appeler
    if not peut_appeler("checkin", reserve=RESERVE_CHECKIN):
        logging.warning("[CHECKIN] Quota SNCF jour presque atteint — réserve check-in")
        return None
    url = (
        f"https://api.sncf.com/v1/coverage/sncf/journeys"
        f"?from=stop_area:SNCF:{sid_dep}&to=stop_area:SNCF:{sid_arr}"
        f"&count=20&data_freshness={freshness}&datetime={dt}&datetime_represents=departure"
        # Les TGV (exclus du check-in TER) ne doivent pas « dominer » et masquer des TER plus lents.
        + "&forbidden_uris[]=physical_mode:LongDistanceTrain"
        + ("&max_nb_transfers=0" if direct else "")
    )
    logging.info(f"[CHECKIN] GET {url}")
    try:
        r = HTTP.get(url, auth=(TOKEN_SNCF, ""), timeout=25)
    except Exception as e:
        logging.error(f"[CHECKIN] Exception requête Navitia : {e}")
        return None
    try:
        from bazo_sources_rt import compter_appel_sncf
        compter_appel_sncf("checkin")
        log_quota_sncf(r, "checkin")
    except Exception:
        pass
    if r.status_code == 429:
        logging.warning("[CHECKIN] Quota API SNCF atteint (429) — réessaie plus tard")
        return None
    if r.status_code == 404:
        return {"journeys": []}          # « aucun trajet » : Navitia répond 404 no_solution
    if r.status_code != 200:
        logging.error(f"[CHECKIN] HTTP {r.status_code} (dt={dt}) : {r.text[:200]}")
        return None
    return r.json()


def _liste_navitia(sid, ressource, cle, depuis, duree_s, max_pages=4):
    """Départs ou arrivées d'une gare (horaire de base, sans TGV), paginés. None si erreur API."""
    from bazo_quota import RESERVE_CHECKIN, peut_appeler
    out = []
    for page in range(max_pages):
        if not peut_appeler("checkin", reserve=RESERVE_CHECKIN):
            return None
        url = (
            f"https://api.sncf.com/v1/coverage/sncf/stop_areas/stop_area:SNCF:{sid}/{ressource}"
            f"?from_datetime={depuis.strftime('%Y%m%dT%H%M%S')}&duration={int(duree_s)}&count=200&start_page={page}"
            f"&data_freshness=base_schedule&forbidden_uris[]=physical_mode:LongDistanceTrain"
        )
        logging.info(f"[CHECKIN] GET {url}")
        try:
            r = HTTP.get(url, auth=(TOKEN_SNCF, ""), timeout=25)
        except Exception as e:
            logging.error(f"[CHECKIN] Exception {ressource} : {e}")
            return None
        try:
            from bazo_sources_rt import compter_appel_sncf
            compter_appel_sncf("checkin")
            log_quota_sncf(r, "checkin")
        except Exception:
            pass
        if r.status_code == 404:
            break
        if r.status_code != 200:
            logging.error(f"[CHECKIN] HTTP {r.status_code} sur {ressource} : {r.text[:150]}")
            return None
        lot = r.json().get(cle, [])
        out.extend(lot)
        if len(lot) < 200:
            break
    return out


def _trains_via_horaires_gares(sid_dep, sid_arr, gare_dep, gare_arr, jour, m_debut, m_fin):
    """Complète la recherche d'itinéraires : Navitia masque les trains « dominés » (plus lents qu'un autre
    train qui part après). On croise donc tous les départs de la gare et toutes les arrivées à destination
    (même numéro de train, arrivée après le départ)."""
    depuis = jour + timedelta(minutes=m_debut)
    duree = max(60, (m_fin - m_debut) * 60)
    deps = _liste_navitia(sid_dep, "departures", "departures", depuis, duree)
    if not deps:
        return []
    arrs = _liste_navitia(sid_arr, "arrivals", "arrivals", depuis, duree + 3 * 3600)
    if not arrs:
        return []
    arrivee = {}
    for a in arrs:
        info = a.get("display_informations", {})
        sdt = a.get("stop_date_time", {})
        h = sdt.get("base_arrival_date_time") or sdt.get("arrival_date_time")
        if info.get("headsign") and h:
            arrivee.setdefault(info["headsign"], []).append(h)
    out = []
    for d in deps:
        info = d.get("display_informations", {})
        if not _section_est_liaison_ferree({"type": "public_transport", "display_informations": info}):
            continue
        sdt = d.get("stop_date_time", {})
        h_dep = sdt.get("base_departure_date_time") or sdt.get("departure_date_time")
        numero = info.get("headsign")
        if not numero or not h_dep:
            continue
        h_arr = next((h for h in sorted(arrivee.get(numero, [])) if h > h_dep), None)
        h_theo = _heure_depuis_navitia(h_dep)
        if not h_arr or not h_theo or not h_dans_tranche(h_theo, f"{m_debut // 60}h-{m_fin // 60}h"):
            continue
        direction = sanitiser_chaine(info.get("direction", "") or gare_arr)
        out.append({
            "numero_train": sanitiser_chaine(numero),
            "heure_theo": h_theo,
            "heure_arrivee_theo": _heure_depuis_navitia(h_arr),
            "destination": direction,
            "direction_commerciale": direction,
            "gare_depart": nom_canonique_gare(gare_dep) or gare_dep,
            "gare_arrivee": nom_canonique_gare(gare_arr) or gare_arr,
            "supprime": False,
        })
    return out


def recuperer_trains_theoriques(gare_dep, gare_arr, tranche, sens="?", date_j=None):
    """Tous les trains THÉORIQUES (horaire de base, y compris ceux supprimés ce jour) qui partent de
    gare_dep vers gare_arr dans la tranche. On part du début de la tranche et on pagine jusqu'à sa fin :
    aucun train du créneau ne peut être oublié (ex. 08h33 pour 08h-10h)."""
    logging.info(f"[CHECKIN] === Recherche trains sens={sens} | {gare_dep} -> {gare_arr} | tranche={tranche} ===")
    sid_dep = resoudre_stop_area(gare_dep)
    sid_arr = resoudre_stop_area(gare_arr)
    if not sid_dep or not sid_arr:
        logging.error(f"[CHECKIN] ABANDON : UIC manquant (dep={sid_dep}, arr={sid_arr}) pour '{gare_dep}' -> '{gare_arr}'")
        return []
    date_j = date_j or datetime.now(tz_paris).strftime("%Y-%m-%d")
    cle_cache = f"v2_{sid_dep}_{sid_arr}_{tranche}_{date_j}"
    with _cache_trains_lock:
        entree = _cache_trains.get(cle_cache)
        if entree and (time.time() - entree["ts"]) < TTL_CACHE_TRAINS_SEC:
            logging.info(f"[CHECKIN] CACHE HIT {cle_cache} ({len(entree['trains'])} trains)")
            return entree["trains"]

    m_debut, m_fin = _bornes_tranche_min(tranche)
    jour = datetime.strptime(date_j, "%Y-%m-%d")
    curseur = jour + timedelta(minutes=m_debut)
    fin = jour + timedelta(minutes=m_fin)
    tous_trains, erreur_api = [], False
    direct = True
    for page in range(6):
        donnees = _journeys_navitia(sid_dep, sid_arr, curseur.strftime("%Y%m%dT%H%M%S"), "base_schedule", direct)
        if donnees is None:
            erreur_api = True
            break
        journeys = donnees.get("journeys", [])
        if not journeys and page == 0 and direct:
            direct = False               # pas de train direct : on propose le 1er tronçon des correspondances
            continue
        if not journeys:
            break
        tous_trains.extend(_parser_trains_de_journeys(donnees, tranche, gare_dep, gare_arr))
        deps = []
        for j in journeys:
            try:
                deps.append(datetime.strptime(j["departure_date_time"], "%Y%m%dT%H%M%S"))
            except Exception:
                pass
        if not deps or max(deps) >= fin - timedelta(minutes=1):
            break
        nouveau = max(deps) + timedelta(minutes=1)
        if nouveau <= curseur:
            break
        curseur = nouveau
    if not erreur_api and direct:
        # Trains directs « dominés » que l'API d'itinéraires ne renvoie pas (ex. TER omnibus 07h09 Metz→Nancy).
        try:
            connus = {t["numero_train"] for t in tous_trains}
            extra = [t for t in _trains_via_horaires_gares(sid_dep, sid_arr, gare_dep, gare_arr, jour, m_debut, m_fin)
                     if t["numero_train"] not in connus]
            if extra:
                logging.info(f"[CHECKIN] +{len(extra)} train(s) ajoutés par croisement départs/arrivées")
            tous_trains.extend(extra)
        except Exception as e:
            logging.error(f"[CHECKIN] croisement départs/arrivées en échec : {e}")
    tous_trains = _dedoublonner_trains_par_numero(tous_trains)
    if not tous_trains:
        logging.error(
            f"[CHECKIN] RÉSULTAT VIDE — '{gare_dep}'(SNCF:{sid_dep}) -> '{gare_arr}'(SNCF:{sid_arr}), "
            f"tranche {tranche}, erreur_api={erreur_api}."
        )
    else:
        logging.info(f"[CHECKIN] OK — {len(tous_trains)} train(s) unique(s) trouvé(s).")
        if not erreur_api:
            with _cache_trains_lock:
                _cache_trains[cle_cache] = {"ts": time.time(), "trains": tous_trains}
    return tous_trains

def enregistrer_checkin(id_telegram, train, tranche, sens, user, date_j=None):
    migrer_schema_phase1()
    conn = db_connect(timeout=10)
    date_j = date_j or datetime.now(tz_paris).strftime("%Y-%m-%d")
    g_dep, g_arr = gares_pour_sens(user, sens)
    g_dep = nom_canonique_gare(g_dep) or g_dep
    g_arr = nom_canonique_gare(g_arr) or g_arr
    direction = train.get("direction_commerciale") or train.get("destination") or g_arr
    conn.execute("""
        INSERT INTO trajets_valides (id_telegram, numero_train, date, heure_theo, heure_arrivee_theo,
            heure_reelle_arrivee, statut_validation, tranche_horaire, sens, gare_depart, gare_arrivee,
            destination, origine, direction_commerciale)
        VALUES (?, ?, ?, ?, ?, NULL, 'en_attente', ?, ?, ?, ?, ?, 'checkin', ?)
        ON CONFLICT(id_telegram, date, numero_train, heure_theo) DO UPDATE SET
            heure_arrivee_theo = excluded.heure_arrivee_theo,
            tranche_horaire = excluded.tranche_horaire,
            sens = excluded.sens, gare_depart = excluded.gare_depart,
            gare_arrivee = excluded.gare_arrivee, destination = excluded.destination,
            direction_commerciale = excluded.direction_commerciale,
            origine = CASE
                WHEN trajets_valides.origine = 'confirme_usager' THEN 'confirme_usager'
                ELSE 'checkin' END,
            statut_validation = CASE
                WHEN trajets_valides.statut_validation LIKE 'valide%' THEN trajets_valides.statut_validation
                ELSE 'en_attente' END
    """, (
        str(id_telegram), train["numero_train"], date_j, train["heure_theo"],
        train.get("heure_arrivee_theo"),
        tranche, sens, g_dep, g_arr, direction, direction,
    ))
    conn.commit()
    conn.close()
    activer_surveillance_checkin(
        date_j, train["numero_train"], g_dep, g_arr,
        train.get("destination", g_arr), train["heure_theo"],
    )

def menu_tranches_checkin(sens=None):
    modele = modele_grille_jour()
    lib = {"semaine": "Semaine (Lun-Ven)", "samedi": "Samedi", "dimanche": "Dimanche"}[modele]
    markup = types.InlineKeyboardMarkup(row_width=2)
    for tr in TRANCHES_PAR_MODELE[modele]:
        markup.add(types.InlineKeyboardButton(tr.replace("h", "h "), callback_data=f"ck_tr_{tr}"))
    sens_txt = f" · {sens}" if sens else ""
    markup.add(types.InlineKeyboardButton(f"📅 Grille : {lib}{sens_txt}", callback_data="ck_info"))
    return ajouter_bouton_retour(markup)

def lister_mois_attestation(id_telegram, user_data):
    """Mois distincts issus des trajets validés (check-in / check-out)."""
    mois_set = set()
    conn = db_connect(timeout=10)
    sync_trajets_valides_depuis_radar(id_telegram)
    rows_tv = conn.execute("""
        SELECT DISTINCT substr(date, 1, 7) FROM trajets_valides
        WHERE id_telegram = ? AND statut_validation != 'annule'
          AND (
            statut_validation LIKE 'valide%'
            OR COALESCE(retard_arrivee_min, 0) >= ?
          )
    """, (str(id_telegram), SEUIL_RETARD_MIN)).fetchall()
    conn.close()
    for (m,) in rows_tv:
        if m:
            mois_set.add(m)
    resultat = []
    for m in sorted(mois_set, reverse=True):
        try:
            y, mo = m.split("-")
            lib = f"{MOIS_FR[int(mo) - 1]} {y}"
        except Exception:
            lib = m
        resultat.append((m, lib))
    return resultat

def menu_mois_pdf(mois_liste):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for code, lib in mois_liste:
        markup.add(types.InlineKeyboardButton(lib, callback_data=f"pdf_mois_{code}"))
    return ajouter_bouton_retour(markup)

def menu_sens_signalement(user):
    g_dom = user.get("gare_depart", "?")
    g_trav = user.get("gare_retour", "?")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        f"➡️ Aller ({g_dom} → {g_trav})",
        callback_data="sig_sens_aller",
    ))
    markup.add(types.InlineKeyboardButton(
        f"⬅️ Retour ({g_trav} → {g_dom})",
        callback_data="sig_sens_retour",
    ))
    return ajouter_bouton_retour(markup)


def _tranche_profil_utilisateur(user, sens):
    """Créneau profil (étiquette) — la liste trains couvre toute la journée."""
    t = user.get("heures") if sens == "aller" else user.get("heures_retour")
    return t if t and t not in ("Non precise", "Toute la journee") else TRANCHE_JOURNEE_COMPLETE


def _charger_trains_apres_sens(user, sens, date_j=None):
    """Trains du jour dans le créneau 2h configuré par l'utilisateur."""
    g_dep, g_arr = gares_pour_sens(user, sens)
    tranche = _tranche_profil_utilisateur(user, sens)
    return recuperer_trains_theoriques(g_dep, g_arr, tranche, sens=sens, date_j=date_j)

def menu_trains_signalement(trains, page=0):
    markup = types.InlineKeyboardMarkup(row_width=1)
    debut = page * TRAINS_PAR_PAGE
    fin = debut + TRAINS_PAR_PAGE
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Précédent", callback_data=f"sig_page_{page - 1}"))
    if fin < len(trains):
        nav.append(types.InlineKeyboardButton(
            f"➡️ Suite ({len(trains) - fin} restants)", callback_data=f"sig_page_{page + 1}",
        ))
    if nav:
        markup.row(*nav)
    for i, t in enumerate(trains[debut:fin]):
        idx = debut + i
        arr = t.get("heure_arrivee_theo")
        trajet_h = f"{t['heure_theo']}→{arr}" if arr else t["heure_theo"]
        lib = f"{t['numero_train']} · {trajet_h} · {t['gare_depart']}→{t['destination']}"
        markup.add(types.InlineKeyboardButton(lib, callback_data=f"sig_pick_{idx}"))
    return ajouter_bouton_retour(markup)

_ICONES_SIGNALEMENT = {
    "retard_au_depart": "⏱", "train_supprime_annonce": "❌", "bus_substitution": "🚌",
    "arret_prolonge": "🛑", "voie_annoncee": "🛤", "forte_affluence": "👥",
    "rame_courte": "📏", "je_suis_bloque": "🆘",
}


def menu_types_signalement():
    """Types fermés (phase 2) : aucun texte libre. Un clic après le choix du train."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    boutons = [
        types.InlineKeyboardButton(
            f"{_ICONES_SIGNALEMENT.get(code, '•')} {info['libelle']}", callback_data=f"sig_type_{code}",
        )
        for code, info in TYPES_SIGNALEMENT.items()
    ]
    for i in range(0, len(boutons), 2):
        markup.row(*boutons[i:i + 2])
    return ajouter_bouton_retour(markup)


def menu_voies_signalement():
    """Valeur fermée pour « voie annoncée » : chiffres 1-9 puis A-C (saisie clavier aussi acceptée)."""
    markup = types.InlineKeyboardMarkup(row_width=5)
    valeurs = [str(i) for i in range(1, 11)] + ["A", "B", "C"]
    boutons = [types.InlineKeyboardButton(v, callback_data=f"sig_voie_{v}") for v in valeurs]
    for i in range(0, len(boutons), 5):
        markup.row(*boutons[i:i + 5])
    return ajouter_bouton_retour(markup)


def _cle_fil(date_j, numero, heure_theo):
    return f"fil_{date_j}_{numero}_{str(heure_theo).replace(':', 'h')}"


def menu_apres_signalement(sig, demander_pdf):
    """Boutons après création : photo facultative, fil du train, et question « est-ce ton trajet ? »."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    if demander_pdf:
        markup.row(
            types.InlineKeyboardButton("📂 Oui, c'est mon trajet", callback_data=f"sigpdf_oui_{sig['id']}"),
            types.InlineKeyboardButton("Non", callback_data=f"sigpdf_non_{sig['id']}"),
        )
    markup.row(
        types.InlineKeyboardButton("📸 Ajouter une photo", callback_data=f"sig_photo_{sig['id']}"),
        types.InlineKeyboardButton(
            "🗣 Fil du train", callback_data=_cle_fil(sig["date"], sig["numero_train"], sig["heure_theo"]),
        ),
    )
    return ajouter_bouton_retour(markup)


def _statut_signalement_txt(statut):
    return {
        "non_confirme": "à confirmer", "confirme": "confirmé par d'autres voyageurs",
        "conteste": "contesté", "expire": "expiré",
    }.get(statut, statut)


def texte_fil_train(date_j, numero, heure_theo, sigs):
    lignes = [f"🗣 *Fil du train {numero}* — {date_j} {heure_theo}", f"_{MENTION_COMMUNAUTAIRE}_", ""]
    if not sigs:
        lignes.append("Aucun signalement pour l'instant.")
    for s in sigs:
        lignes.append(
            f"• *{s['libelle']}* — {_statut_signalement_txt(s['statut'])} "
            f"(👍 {s['nb_confirmations']} · 👎 {s['nb_refutations']})"
        )
    return "\n".join(lignes)


def menu_fil_train(sigs, id_user):
    markup = types.InlineKeyboardMarkup(row_width=2)
    for s in sigs[:6]:
        if str(s["id_auteur"]) == str(id_user) or s["statut"] == "expire":
            continue
        lib = s["libelle"][:22]
        markup.row(
            types.InlineKeyboardButton(f"👍 {lib}", callback_data=f"vote_c_{s['id']}"),
            types.InlineKeyboardButton(f"👎 {lib}", callback_data=f"vote_r_{s['id']}"),
        )
    return ajouter_bouton_retour(markup)


def _stocker_photo_signalement(file_id, signalement_id):
    """Photo sur disque (jamais un file_id Telegram), métadonnées EXIF retirées. Visible admin seulement."""
    dossier = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "signalements_photos")
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, f"sig_{int(signalement_id)}.jpg")
    info = bot.get_file(file_id)
    contenu = bot.download_file(info.file_path)
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(contenu))
        img = img.convert("RGB")
        propre = Image.new("RGB", img.size)
        propre.putdata(list(img.getdata()))
        propre.save(chemin, "JPEG", quality=85)
    except Exception:
        with open(chemin, "wb") as f:
            f.write(contenu)
    conn = db_connect(timeout=5)
    conn.execute("UPDATE signalements SET photo_chemin=? WHERE id=?", (chemin, int(signalement_id)))
    conn.commit()
    conn.close()
    return chemin

def id_trajet_apres_checkin(id_telegram, numero_train, heure_theo):
    """ID du trajet du jour après enregistrement (lien signalement → PDF)."""
    try:
        conn = db_connect(timeout=5)
        row = conn.execute(
            """
            SELECT id FROM trajets_valides
            WHERE id_telegram = ? AND date = date('now')
              AND numero_train = ? AND heure_theo = ?
              AND statut_validation != 'annule'
            """,
            (str(id_telegram), str(numero_train), str(heure_theo)),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def enregistrer_rapport_ticket(
    id_telegram, prenom, message_utilisateur,
    type_signalement=None, photo_file_id=None, numero_train=None, gare=None, id_trajet=None,
):
    """Signalement voyageur : photo + type, modération admin, lien PDF si trajet déclaré."""
    now = datetime.now(tz_paris)
    date_j = now.strftime("%Y-%m-%d")
    heure_j = now.strftime("%H:%M:%S")
    msg_clean = sanitiser_chaine(message_utilisateur).replace(";", ",")
    rapport_id = None
    tid_lie = id_trajet
    if type_signalement and type_signalement not in ("autre", None):
        tid_lie = promouvoir_trajet_signalement(
            id_telegram, type_signalement, retard_min=SEUIL_RETARD_MIN,
            numero_train=numero_train, gare=gare, id_trajet=id_trajet,
        ) or id_trajet
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO rapports_incidents (
                date, heure, id_telegram, prenom, message, statut,
                type_signalement, photo_file_id, numero_train, gare, id_trajet
            ) VALUES (?, ?, ?, ?, ?, 'en_attente', ?, ?, ?, ?, ?)
            """,
            (
                date_j, heure_j, str(id_telegram), sanitiser_chaine(prenom), msg_clean,
                type_signalement, photo_file_id, numero_train, gare, tid_lie,
            ),
        )
        rapport_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"[TICKET] Insertion DB échouée : {e}")
        return False
    logging.info(f"[TICKET] Rapport #{rapport_id} enregistré pour {id_telegram}")
    try:
        markup = types.InlineKeyboardMarkup(row_width=2)
        if rapport_id:
            markup.add(
                types.InlineKeyboardButton("✅ Valider", callback_data=f"rap_ok_{rapport_id}"),
                types.InlineKeyboardButton("❌ Rejeter", callback_data=f"rap_no_{rapport_id}"),
            )
        entete = (
            f"🎫 *Signalement #{rapport_id}*\n"
            f"👤 {prenom} (`{id_telegram}`)\n"
            f"📅 {date_j} {heure_j}\n"
            f"🏷 Type : *{type_signalement or 'autre'}*\n"
        )
        if numero_train:
            entete += f"🚆 Train {numero_train}\n"
        if gare:
            entete += f"📍 {gare}\n"
        if tid_lie:
            entete += f"📂 Lié au trajet #{tid_lie} (PDF)\n"
        entete += f"\n{msg_clean[:3000]}\n\n_Valider = confirmé dans le dossier PDF._"
        if photo_file_id:
            bot.send_photo(ADMIN_CHAT_ID, photo_file_id, caption=entete[:1024], parse_mode="Markdown", reply_markup=markup)
        else:
            bot.send_message(ADMIN_CHAT_ID, entete, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        logging.error(f"[TICKET] Notification admin échouée : {e}")
    return True

def menu_trains_checkin(trains, page=0):
    markup = types.InlineKeyboardMarkup(row_width=1)
    debut = page * TRAINS_PAR_PAGE
    fin = debut + TRAINS_PAR_PAGE
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Précédent", callback_data=f"ck_page_{page - 1}"))
    if fin < len(trains):
        nav.append(types.InlineKeyboardButton(
            f"➡️ Suite ({len(trains) - fin} restants)", callback_data=f"ck_page_{page + 1}",
        ))
    if nav:
        markup.row(*nav)
    for i, t in enumerate(trains[debut:fin]):
        idx = debut + i
        arr = t.get("heure_arrivee_theo")
        trajet_h = f"{t['heure_theo']}→{arr}" if arr else t["heure_theo"]
        lib = f"{trajet_h} · Train {t['numero_train']}" + (" · ❌ supprimé" if t.get("supprime") else "")
        markup.add(types.InlineKeyboardButton(lib, callback_data=f"ck_pick_{idx}"))
    if len(trains) > TRAINS_PAR_PAGE and page == 0:
        markup.add(types.InlineKeyboardButton(
            f"📋 {len(trains)} trains au total — utilise Suite pour voir la liste",
            callback_data="ck_info_more",
        ))
    return ajouter_bouton_retour(markup)

def finaliser_validation_trajet(trajet_id, statut, heure_reelle=None, retard_min=None, motif=None):
    conn = db_connect(timeout=10)
    c = conn.cursor()
    c.execute("SELECT statut_validation FROM trajets_valides WHERE id = ?", (trajet_id,))
    _deja = c.fetchone()
    if _deja and _deja[0] == "valide_report" and statut != "valide_report":
        conn.close()      # le retard cumulé calculé par le moteur temps réel n'est jamais écrasé
        return True
    c.execute("""
        SELECT id_telegram, numero_train, date, heure_theo, gare_depart, gare_arrivee,
               destination, heure_arrivee_theo
        FROM trajets_valides WHERE id = ?
    """, (trajet_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    id_tg, num, date_j, h_theo, g_dep, g_arr, dest, h_arr_theo = row
    # Retard = arrivée réelle - arrivée THÉORIQUE à destination (jamais le départ).
    if retard_min is None:
        if heure_reelle and h_arr_theo:
            retard_min = calculer_retard_arrivee(h_arr_theo, heure_reelle)
        else:
            retard_min = 0
    c.execute("""
        UPDATE trajets_valides SET statut_validation = ?, heure_reelle_arrivee = ?,
            retard_arrivee_min = ?, motif_validation = ?
        WHERE id = ?
    """, (statut, heure_reelle, retard_min, motif, trajet_id))
    if statut == "valide_bus_panne" and retard_min >= SEUIL_RETARD_MIN:
        motif_f = sanitiser_chaine(motif or MOTIF_RUPTURE_CHARGE)
        c.execute("""
            SELECT id FROM historique WHERE date = ? AND numero_train = ? AND heure_theo = ?
        """, (date_j, num, h_theo))
        ex = c.fetchone()
        if ex:
            c.execute("""
                UPDATE historique SET retard_min = ?, retard_arrivee_min = ?,
                    motif = ?, heure_reel = ?, type = 'retard'
                WHERE id = ?
            """, (retard_min, retard_min, motif_f, heure_reelle, ex[0]))
        else:
            c.execute("""
                INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min,
                    retard_depart_min, retard_arrivee_min, motif, heure_theo, heure_reel)
                VALUES (?, ?, ?, ?, 'retard', ?, NULL, ?, ?, ?, ?)
            """, (date_j, num, dest, g_dep, retard_min, retard_min, motif_f, h_theo, heure_reelle))
    conn.commit()
    conn.close()
    return True

def parser_heure_saisie(texte):
    t = texte.strip().lower().replace("h", ":").replace(" ", "")
    m = re.match(r"^(\d{1,2}):?(\d{2})$", t)
    if not m:
        return None
    return f"{int(m.group(1)):02d}h{int(m.group(2)):02d}"

def _gares_similaires(a, b):
    if not a or not b:
        return False
    na = str(a).lower().replace("-", " ").strip()
    nb = str(b).lower().replace("-", " ").strip()
    return na in nb or nb in na

def retard_reel_scanner(date_j, numero_train, gare_arrivee=None, heure_theo=None):
    """Retard réel — scope par heure de départ si connue."""
    return retard_reel_scanner_db(date_j, numero_train, gare_arrivee, None, heure_theo=heure_theo)

def estimer_retard_train_suivant(date_j, numero_train, h_arr_theo, gare_arrivee=None):
    """Retard subi en prenant le train suivant : arrivée du suivant - arrivée prévue initiale.
    Estimé automatiquement depuis le snapshot théorique du jour + retard réel du suivant."""
    try:
        if not h_arr_theo:
            return 0
        conn = db_connect(timeout=10)
        rows = conn.execute(
            """SELECT numero_train, heure_arrivee_theo, gare_arrivee, destination
               FROM horaires_theoriques_jour
               WHERE date=? AND heure_arrivee_theo IS NOT NULL""",
            (date_j,),
        ).fetchall()
        conn.close()
        cible = _minutes_heure_label(h_arr_theo)
        candidats = []
        for num, arr, g_arr, dest in rows:
            if str(num) == str(numero_train):
                continue
            if gare_arrivee and not (_gares_similaires(g_arr, gare_arrivee) or _gares_similaires(dest, gare_arrivee)):
                continue
            m = _minutes_heure_label(arr)
            if m > cible:
                candidats.append((m, num))
        if not candidats:
            return 0
        m_next, num_next = min(candidats, key=lambda x: x[0])
        gap = m_next - cible
        r_next, _ = retard_reel_scanner(date_j, num_next, gare_arrivee)
        return max(0, gap + (r_next or 0))
    except Exception:
        return 0

def _verifier_proprietaire_trajet(trajet_id, id_user):
    """Sécurité : un utilisateur ne peut valider que SES propres trajets."""
    try:
        conn = db_connect(timeout=10)
        row = conn.execute(
            "SELECT id_telegram FROM trajets_valides WHERE id = ?", (trajet_id,)
        ).fetchone()
        conn.close()
        return row is not None and str(row[0]) == str(id_user)
    except Exception:
        return False

def executer_checkout(trajet_id, id_user, action):
    """Check-out API / mobile — action : ok | suppr | suiv | bus | rien."""
    if not _verifier_proprietaire_trajet(trajet_id, id_user):
        return {"ok": False, "error": "forbidden", "detail": "Ce trajet ne t'appartient pas."}
    infos = _infos_trajet(trajet_id)
    if not infos:
        return {"ok": False, "error": "not_found", "detail": "Trajet introuvable ou déjà validé."}
    action = (action or "").strip().lower()
    if action == "ok":
        retard, supprime = retard_reel_scanner(
            infos["date"], infos["numero_train"], infos["gare_arrivee"], infos.get("heure_theo"),
        )
        if supprime:
            finaliser_validation_trajet(
                trajet_id, "valide_supprime", retard_min=0, motif="Train supprimé (annulation SNCF)",
            )
            return {"ok": True, "statut": "valide_supprime", "retard_min": 0, "suppression": True}
        finaliser_validation_trajet(trajet_id, "valide_normal", retard_min=retard)
        return {"ok": True, "statut": "valide_normal", "retard_min": retard, "suppression": False}
    if action == "suppr":
        finaliser_validation_trajet(
            trajet_id, "valide_supprime", retard_min=0, motif="Train supprimé (annulation SNCF)",
        )
        return {"ok": True, "statut": "valide_supprime", "retard_min": 0, "suppression": True}
    if action == "suiv":
        retard = estimer_retard_train_suivant(
            infos["date"], infos["numero_train"], infos["heure_arrivee_theo"], infos["gare_arrivee"],
        ) if infos else 0
        finaliser_validation_trajet(
            trajet_id, "valide_normal", retard_min=retard,
            motif="Prise du train suivant (retard en cascade)",
        )
        return {"ok": True, "statut": "valide_normal", "retard_min": retard, "suppression": False}
    if action == "bus":
        retard, _ = retard_reel_scanner(
            infos["date"], infos["numero_train"], infos["gare_arrivee"], infos.get("heure_theo"),
        )
        retard = max(retard, 10)
        finaliser_validation_trajet(
            trajet_id, "valide_bus_panne", retard_min=retard, motif=MOTIF_RUPTURE_CHARGE,
        )
        return {"ok": True, "statut": "valide_bus_panne", "retard_min": retard, "suppression": False}
    if action == "rien":
        finaliser_validation_trajet(trajet_id, "annule", retard_min=0)
        return {"ok": True, "statut": "annule", "retard_min": 0, "suppression": False}
    return {"ok": False, "error": "invalid_action", "detail": "Action : ok, suppr, suiv, bus, rien."}


def _infos_trajet(tid):
    """Infos d'un check-in pour le check-out automatique."""
    try:
        conn = db_connect(timeout=10)
        row = conn.execute(
            """SELECT id_telegram, date, numero_train, gare_arrivee, heure_arrivee_theo, heure_theo
               FROM trajets_valides WHERE id = ?""",
            (tid,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "id_telegram": row[0], "date": row[1], "numero_train": row[2],
            "gare_arrivee": row[3], "heure_arrivee_theo": row[4], "heure_theo": row[5],
        }
    except Exception:
        return None

def charger_trajets_valides_pdf(id_telegram, mois_filtre=None):
    """Trajets déclarés par l'utilisateur (check-in) — base du PDF, enrichie par le radar."""
    migrer_schema_phase1()
    migrer_schema_phase2()
    conn = db_connect(timeout=10)
    # Règle R3 : seuls les trajets déclarés (check-in) ou confirmés par l'usager entrent
    # dans l'attestation. Les lignes fabriquées par le radar (radar_auto_legacy) sont exclues.
    sql = """
        SELECT id, date, numero_train, heure_theo, heure_reelle_arrivee, statut_validation,
            gare_depart, gare_arrivee, destination, retard_arrivee_min, motif_validation,
            heure_arrivee_theo, sens, direction_commerciale, COALESCE(origine, 'checkin'),
            COALESCE(provenance, 'officielle')
        FROM trajets_valides
        WHERE id_telegram = ?
          AND statut_validation != 'annule'
          AND COALESCE(origine, 'checkin') IN ('checkin', 'confirme_usager')
    """
    params = [str(id_telegram)]
    if mois_filtre:
        sql += " AND date LIKE ?"
        params.append(f"{mois_filtre}%")
    sql += " ORDER BY date, heure_theo"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def persister_figement_trajet(trajet_id, statut_cible, retard, motif):
    """Verrouille un trajet validé — appelé par checkout/radar, pas à la lecture PDF."""
    if not trajet_id:
        return
    try:
        conn = db_connect(timeout=8)
        conn.execute(
            """
            UPDATE trajets_valides
            SET statut_validation = ?,
                retard_arrivee_min = MAX(COALESCE(retard_arrivee_min, 0), ?),
                motif_validation = COALESCE(NULLIF(motif_validation, ''), ?)
            WHERE id = ? AND statut_validation = 'en_attente'
            """,
            (statut_cible, int(retard or 0), motif, trajet_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"[PDF] Figer incident trajet #{trajet_id} : {e}")

def incidents_pdf_depuis_trajets_valides(rows):
    """Construit le dict d'incidents PDF depuis trajets_valides + radar (retards persistés)."""
    incidents = {}
    for row in rows:
        tid = row[0]
        date_inc = str(row[1]).strip()
        num_train = str(row[2]).strip()
        h_theo = str(row[3]).strip()
        h_reelle = str(row[4]).strip() if row[4] else None
        statut = str(row[5]).strip()
        g_dep = str(row[6] or "").strip()
        g_arr = str(row[7] or "").strip()
        dest = str(row[8] or g_arr).strip()
        retard_db = row[9]
        motif_val = str(row[10] or "").strip()
        h_arr_theo = str(row[11]).strip() if len(row) > 11 and row[11] else None
        sens_db = str(row[12]).strip() if len(row) > 12 and row[12] else None
        direction = str(row[13]).strip() if len(row) > 13 and row[13] else dest
        origine = str(row[14]).strip() if len(row) > 14 and row[14] else "checkin"
        provenance = str(row[15]).strip() if len(row) > 15 and row[15] else "officielle"

        # Règle R1 : le trajet affiché est le segment usager, jamais la direction commerciale.
        try:
            trajet_final = libelle_trajet_pdf(g_dep, g_arr)
        except ValueError as e:
            logging.warning(f"[PDF] trajet #{tid} ignoré : {e}")
            continue

        retard = int(retard_db) if retard_db is not None else 0
        if h_reelle and h_arr_theo:
            retard = max(retard, calculer_retard_arrivee(h_arr_theo, h_reelle))

        # Règles R4 / R5 : un seul appel, scoppé sur le segment usager.
        r_scan, suppr_scan = retard_reel_scanner_db(
            date_inc, num_train, gare_arrivee=g_arr, gare_depart=g_dep, heure_theo=h_theo,
        )
        retard = max(retard, int(r_scan or 0))
        if statut in ("valide_normal", "valide_supprime") and int(retard_db or 0) >= SEUIL_RETARD_MIN:
            retard = max(retard, int(retard_db))

        if statut == "valide_supprime" or (statut == "en_attente" and suppr_scan):
            motif_pdf = motif_val or "Train supprimé"
            type_inc = "supprime"
            retard = 0
        elif statut == "valide_bus_panne":
            motif_pdf = MOTIF_RUPTURE_CHARGE
            type_inc = "retard"
            retard = max(retard, SEUIL_RETARD_MIN)
        elif statut == "valide_report":
            # Train arrêté / supprimé en cours de route : retard réel cumulé (arrivée réelle du 2e train
            # - arrivée prévue du 1er), calculé et stocké par le moteur temps réel.
            motif_pdf = motif_val or "Arrêt en cours de route : changement de train forcé"
            type_inc = "retard"
            retard = max(int(retard_db or 0), SEUIL_RETARD_MIN)
        else:
            if (
                statut == "valide_normal"
                and retard < SEUIL_RETARD_MIN
                and "train suivant" in motif_val.lower()
            ):
                retard = max(
                    retard,
                    estimer_retard_train_suivant(date_inc, num_train, h_arr_theo, g_arr),
                )
            if retard < SEUIL_RETARD_MIN:
                continue
            motif_pdf = motif_val or "Retard constaté sur la ligne"
            type_inc = "retard"

        # Règle R6 : motif en français usager, nettoyage unique partagé bot / API / mobile.
        motif_pdf = motif_pdf_officiel(motif_pdf, type_inc, retard)

        cle_unique = f"{date_inc}_{num_train}_{h_theo}"
        incidents[cle_unique] = {
            "date": date_inc,
            "train": num_train,
            "trajet": trajet_final,
            "heure": h_theo,
            "motif": sanitiser_chaine(motif_pdf)[:60],
            "retard_txt": "Supprimé" if type_inc == "supprime" else f"+{retard} min",
            "retard": retard,
            "type": type_inc,
            "sens": sens_db,
            "gare_depart": nom_canonique_gare(g_dep) or g_dep,
            "gare_arrivee": nom_canonique_gare(g_arr) or g_arr,
            "direction_commerciale": direction,
            "origine": origine,
            # Phase 2 : provenance par ligne (officielle = radar SNCF, voyageur = déclaration de l'usager).
            "provenance": provenance,
            "provenance_libelle": LIBELLE_PROVENANCE.get(provenance, LIBELLE_PROVENANCE["officielle"]),
        }
    return incidents

def incidents_manuels_valides(id_telegram, mois=None):
    """Signalements libres de l'usager validés par l'admin -> lignes d'incident PDF."""
    incidents = {}
    try:
        conn = db_connect(timeout=10)
        sql = "SELECT id, date, message, retard_min FROM rapports_incidents WHERE id_telegram = ? AND statut = 'valide'"
        params = [str(id_telegram)]
        if mois:
            sql += " AND date LIKE ?"
            params.append(f"{mois}%")
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except Exception as e:
        logging.error(f"[PDF] Chargement incidents manuels échoué : {e}")
        return incidents
    for rid, date_inc, message_rap, retard_min in rows:
        retard = int(retard_min or 0)
        incidents[f"manuel_{rid}"] = {
            "date": str(date_inc).strip(),
            "train": "-",
            "trajet": "Signalement usager",
            "heure": "-",
            "motif": sanitiser_chaine(message_rap or "Incident signalé")[:60],
            "retard_txt": f"+{retard} min" if retard > 0 else "Signalé",
            "retard": retard,
            "type": "retard",
        }
    return incidents

def bilan_mois_trajets_valides(id_telegram, mois=None):
    """Bilan mensuel aligné Mon Statut / PDF (radar + déclarations)."""
    mois = mois or datetime.now(tz_paris).strftime("%Y-%m")
    user = charger_utilisateur(id_telegram) or {}
    incidents, rows = assembler_incidents_pdf(str(id_telegram), mois, user)
    return bilan_depuis_incidents_pdf(incidents, len(rows))

def tache_checkout_soir():
    """Notification 20h — tous les trajets en_attente (y compris vacances / alertes OFF)."""
    date_j = datetime.now(tz_paris).strftime("%Y-%m-%d")
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute("""
            SELECT id, id_telegram, numero_train, heure_theo, tranche_horaire
            FROM trajets_valides
            WHERE date = ? AND statut_validation = 'en_attente'
        """, (date_j,)).fetchall()
        conn.close()
        inscrits = {str(u["id_telegram_propre"]): u for u in charger_inscriptions()}
        now = datetime.now(tz_paris)
        for tid, id_tg, num, h_theo, tranche in rows:
            profil = inscrits.get(str(id_tg), {}) or charger_utilisateur(id_tg)
            prenom = profil.get("prenom", "") if profil else ""
            salut = f"Bonsoir {prenom} ! " if prenom and prenom.lower() != "voyageur" else ""
            hint_radar = ""
            try:
                conn2 = db_connect(timeout=5)
                tv = conn2.execute(
                    "SELECT gare_arrivee, retard_arrivee_min, motif_validation FROM trajets_valides WHERE id=?",
                    (tid,),
                ).fetchone()
                conn2.close()
                if tv:
                    g_arr, r_db, motif = tv
                    r_scan, suppr = retard_reel_scanner(date_j, num, g_arr, h_theo)
                    if suppr:
                        hint_radar = "\n⚠️ _Le radar signale une suppression sur ce train._"
                    elif (r_db or 0) >= SEUIL_RETARD_MIN or r_scan >= SEUIL_RETARD_MIN:
                        r_fin = max(int(r_db or 0), r_scan)
                        hint_radar = f"\n⏱ _Retard déjà détecté : {format_duree_retard(r_fin)}._"
            except Exception:
                pass
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("✅ Oui, je l'ai pris", callback_data=f"ck_out_ok_{tid}"))
            markup.add(types.InlineKeyboardButton("⏭ J'ai pris le suivant", callback_data=f"ck_out_suiv_{tid}"))
            markup.add(types.InlineKeyboardButton("❌ Il était supprimé", callback_data=f"ck_out_suppr_{tid}"))
            markup.add(types.InlineKeyboardButton("🚌 Panne / Fini en Bus", callback_data=f"ck_out_bus_{tid}"))
            markup.add(types.InlineKeyboardButton("🙅 Je n'ai pas voyagé", callback_data=f"ck_out_rien_{tid}"))
            ajouter_bouton_retour(markup)
            bot.send_message(
                id_tg,
                f"🌆 *Check-out Bazo*\n\n{salut}As-tu pris le train *{num}* prévu à *{h_theo}* ?{hint_radar}\n\n"
                f"👉 *1 seul clic suffit* — Bazo calcule ton retard à l'arrivée pour ton dossier SNCF.\n"
                f"_Si tu n'as pas voyagé, choisis « Je n'ai pas voyagé »._",
                reply_markup=markup,
                parse_mode="Markdown",
            )
            time.sleep(0.05)
    except Exception as e:
        logging.warning(f"[CHECKOUT] Tâche 20h échouée : {e}")

_dernier_checkout_jour = None

def _boucle_planificateur_fallback():
    """Thread daemon si APScheduler absent (pip install APScheduler)."""
    global _dernier_checkout_jour
    while True:
        try:
            now = datetime.now(tz_paris)
            cle = now.strftime("%Y-%m-%d")
            if now.hour == 20 and now.minute == 0 and _dernier_checkout_jour != cle:
                _dernier_checkout_jour = cle
                tache_checkout_soir()
        except Exception as e:
            logging.warning(f"[PLANIFICATEUR] Boucle fallback : {e}")
        time.sleep(30)

def tache_entretien_signalements():
    """Expiration des signalements (heure théorique + 3 h) et purge des photos de plus de 30 jours."""
    try:
        n = expirer_signalements()
        from bazo_metier import purger_photos_signalements
        p = purger_photos_signalements(jours=30)
        if n or p:
            logging.info("[SIGNALEMENT] %s expiré(s), %s photo(s) purgée(s)", n, p)
    except Exception as e:
        logging.warning("[SIGNALEMENT] entretien : %s", e)


def demarrer_planificateur():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone=tz_paris)
        scheduler.add_job(tache_checkout_soir, "cron", hour=20, minute=0, id="checkout_soir")
        scheduler.add_job(tache_entretien_signalements, "interval", minutes=30, id="entretien_signalements")
        scheduler.start()
        logging.info("[PLANIFICATEUR] Checkout 20h00 actif (APScheduler).")
        return
    except ImportError:
        threading.Thread(target=_boucle_planificateur_fallback, daemon=True).start()
        logging.info("[PLANIFICATEUR] Checkout 20h00 actif (thread fallback).")
    except Exception as e:
        logging.warning(f"[PLANIFICATEUR] Non démarré : {e}")

JOURS_LIBELLES = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

def menu_preferences_alertes(user):
    jours_raw = str(user.get("jours_actifs") or "0,1,2,3,4")
    jours_set = {int(x.strip()) for x in jours_raw.split(",") if x.strip().isdigit()}
    markup = types.InlineKeyboardMarkup(row_width=2)
    for i, lib in enumerate(JOURS_LIBELLES):
        actif = "✅" if i in jours_set else "⬜"
        markup.add(types.InlineKeyboardButton(f"{actif} {lib}", callback_data=f"pref_j_{i}"))
    vac = user.get("vacances_jusquau")
    if vac:
        markup.add(types.InlineKeyboardButton(f"🏖️ Vacances jusqu'au {vac}", callback_data="pref_vac_off"))
    else:
        markup.add(types.InlineKeyboardButton("🏖️ Vacances 7 jours", callback_data="pref_vac_7"))
        markup.add(types.InlineKeyboardButton("🏖️ Vacances 14 jours", callback_data="pref_vac_14"))
    if user.get("alertes_actives", 1) == 0:
        markup.add(types.InlineKeyboardButton("🔔 Réactiver toutes les alertes", callback_data="pref_alert_on"))
    else:
        markup.add(types.InlineKeyboardButton("🔕 Couper les alertes", callback_data="pref_alert_off"))
    return ajouter_bouton_retour(markup)

def menu_choix_periode(prefixe):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🌅 Horaires du Matin", callback_data=f"nav_m_{prefixe}"), types.InlineKeyboardButton("🌆 Après-midi / Soir", callback_data=f"nav_s_{prefixe}"))
    return ajouter_bouton_retour(markup)

def menu_heures_matin(prefixe):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("04h - 06h", callback_data=f"set_{prefixe}_04h-06h"), types.InlineKeyboardButton("06h - 08h", callback_data=f"set_{prefixe}_06h-08h"))
    markup.add(types.InlineKeyboardButton("08h - 10h", callback_data=f"set_{prefixe}_08h-10h"), types.InlineKeyboardButton("10h - 12h", callback_data=f"set_{prefixe}_10h-12h"))
    return ajouter_bouton_retour(markup)

def menu_heures_soir(prefixe):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("12h - 14h", callback_data=f"set_{prefixe}_12h-14h"), types.InlineKeyboardButton("14h - 16h", callback_data=f"set_{prefixe}_14h-16h"))
    markup.add(types.InlineKeyboardButton("16h - 18h", callback_data=f"set_{prefixe}_16h-18h"), types.InlineKeyboardButton("18h - 20h", callback_data=f"set_{prefixe}_18h-20h"))
    markup.add(types.InlineKeyboardButton("20h - 22h", callback_data=f"set_{prefixe}_20h-22h"))
    return ajouter_bouton_retour(markup)

# ==========================================
# 3. COMMANDES ET LOGIQUE DE BASE
# ==========================================
@bot.message_handler(commands=['annuler', 'cancel'])
def commande_annuler(message):
    id_user = str(message.chat.id)
    nettoyer_etat_utilisateur(id_user)
    user = charger_utilisateur(id_user)
    bot.send_message(
        message.chat.id,
        f"❌ Action annulée.\n\n{message_accueil(user.get('prenom') if user else None)}",
        reply_markup=menu_principal(),
        parse_mode="Markdown",
    )


def est_admin(message_ou_id):
    cible = getattr(getattr(message_ou_id, "chat", None), "id", message_ou_id)
    return str(cible) == str(ADMIN_CHAT_ID)


def texte_panneau_admin():
    import html as html_mod
    from bazo_metier import stats_beta
    from bazo_quota import statut_quota

    s = stats_rapport_db() or {}
    beta = stats_beta()
    q = statut_quota()
    bot_ok = subprocess.run(["systemctl", "is-active", "--quiet", "bazo-bot"]).returncode == 0
    radar_ok = subprocess.run(["systemctl", "is-active", "--quiet", "bazo-radar"]).returncode == 0
    mois = datetime.now(tz_paris).strftime("%Y-%m")
    dash = html_mod.escape(f"{API_BASE_URL}/admin/")
    return (
        "🛠 <b>Panneau Admin Bazo</b>\n\n"
        f"🟢 Bot : {'actif' if bot_ok else 'ARRÊTÉ'} | Radar : {'actif' if radar_ok else 'ARRÊTÉ'}\n\n"
        f"🧪 <b>Beta Metz–Lux</b> : {beta['inscrits']}/{beta['objectif']} inscrits "
        f"({beta['progression_pct']} %) · <b>{beta['actifs_7j']}</b> actifs 7 j\n"
        f"👥 Inscrits : <b>{s.get('inscrits', 0)}</b> (alertes ON : {s.get('alertes_on', 0)})\n"
        f"🚆 Check-ins aujourd'hui : <b>{s.get('checkin_jour', 0)}</b>\n"
        f"📅 Ce mois : <b>{s.get('tv_valides_mois', 0)}</b> validés / <b>{s.get('tv_mois', 0)}</b> déclarés"
        f" ({s.get('tv_en_attente_mois', 0)} en attente)\n"
        f"❌ Suppressions ce mois : <b>{s.get('suppr_mois', 0)}</b>\n"
        f"💰 Éligibles GF (estim.) : <b>{s.get('eligible_gf', 0)}</b>\n"
        f"⚠️ Incidents radar aujourd'hui : <b>{s.get('incidents_jour', 0)}</b>\n"
        f"🧹 Lignes OCESN : <b>{s.get('ocesn_restants', 0)}</b>\n"
        f"🎫 Signalements en attente : <b>{s.get('pending_rapports', 0)}</b>\n"
        f"📡 Quota SNCF : <b>{q['utilise']}/{q['budget_jour']}</b> (reste {q['restant']})\n"
        f"\n<i>Mois courant : {html_mod.escape(libelle_mois(mois))}</i>\n"
        f"Dashboard web : {dash}"
    )


def _envoyer_panneau_admin_message(chat_id, txt, edit_message_id=None):
    markup = menu_admin()
    for mode in ("HTML", None):
        try:
            kwargs = {"reply_markup": markup}
            if mode:
                kwargs["parse_mode"] = mode
            if edit_message_id:
                bot.edit_message_text(txt, chat_id, edit_message_id, **kwargs)
            else:
                bot.send_message(chat_id, txt, **kwargs)
            return True
        except Exception as e:
            logging.warning("[ADMIN] Envoi panneau (%s) échoué : %s", mode or "plain", e)
    return False


def envoyer_panneau_admin(chat_id, edit_message_id=None):
    txt = texte_panneau_admin()
    if not _envoyer_panneau_admin_message(chat_id, txt, edit_message_id=edit_message_id):
        bot.send_message(chat_id, "⚠️ Panneau admin — erreur d'affichage. Réessaie /admin", reply_markup=menu_admin())


def menu_admin():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
        types.InlineKeyboardButton("🎫 Modérer", callback_data="adm_pending"),
    )
    markup.row(
        types.InlineKeyboardButton("📄 PDF global", callback_data="adm_pdfglobal"),
        types.InlineKeyboardButton("📂 Mon PDF", callback_data="adm_pdfperso"),
    )
    markup.row(
        types.InlineKeyboardButton("🔄 Radar", callback_data="adm_rel_radar"),
        types.InlineKeyboardButton("🔄 Bot", callback_data="adm_rel_bot"),
    )
    markup.row(
        types.InlineKeyboardButton("🩺 Healthcheck", callback_data="adm_health"),
        types.InlineKeyboardButton("🔧 Réparer mois", callback_data="adm_repair"),
    )
    markup.add(types.InlineKeyboardButton("🔄 Actualiser", callback_data="adm_refresh"))
    return markup


@bot.message_handler(commands=['beta'])
def cmd_beta(message):
    """Stats beta + lien de partage."""
    from bazo_metier import stats_beta, texte_invitation_beta

    b = stats_beta()
    barre = "█" * (b["progression_pct"] // 10) + "░" * (10 - b["progression_pct"] // 10)
    bot.reply_to(
        message,
        f"🧪 *Beta Metz–Lux*\n\n"
        f"👥 {b['inscrits']}/{b['objectif']} inscrits · {b['actifs_7j']} actifs (7j)\n"
        f"[{barre}] {b['progression_pct']}%\n\n"
        f"Partage Bazo à un·e collègue TER :\n"
        f"`{texte_invitation_beta()}`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=['gf', 'mongf'])
def cmd_stats_user(message):
    """Statut GF personnel (résumé rapide — /statut ouvre le menu complet)."""
    id_user = str(message.chat.id)
    user = charger_utilisateur(id_user)
    if not user:
        bot.reply_to(message, "Inscris-toi via /start")
        return
    from bazo_metier import barre_progression_gf, bilan_mois_aligne_pdf, eligibilite_gf_message, libelle_mois

    mois = datetime.now(tz_paris).strftime("%Y-%m")
    b = bilan_mois_aligne_pdf(id_user, mois, user)
    total_retard = int(b.get("retard_min", 0))
    total_suppr = int(b.get("suppressions", 0))
    bot.reply_to(
        message,
        f"📊 *{libelle_mois(mois)}*\n\n"
        f"{barre_progression_gf(total_retard, total_suppr)}\n\n"
        f"{eligibilite_gf_message(total_retard, total_suppr)}",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=['app', 'pwa'])
def cmd_app_pwa(message):
    """Lien app web + instructions token."""
    from bazo_config import API_BASE_URL

    bot.reply_to(
        message,
        "📱 *App web Bazo*\n\n"
        f"1. Ouvre {API_BASE_URL}/app/\n"
        "2. En MP : /token pour ton token API\n"
        "3. Saisis ID Telegram + token → Enregistrer\n\n"
        "_Alternative : tout se fait aussi ici dans Telegram._",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=['token', 'mon_token', 'apitoken'])
def cmd_api_token(message):
    """Token API personnel — confidentiel, pour l'app / PWA."""
    id_user = str(message.chat.id)
    if message.chat.type != "private":
        bot.reply_to(message, "🔒 Envoie /token en message privé pour recevoir ton token API.")
        return
    from bazo_metier import migrer_colonne_api_token, regenerer_api_token
    migrer_colonne_api_token()
    user = charger_utilisateur(id_user)
    if not user:
        bot.reply_to(message, "Inscris-toi d'abord via /start")
        return
    args = (message.text or "").split()
    regen = len(args) > 1 and args[1].lower() in ("regen", "regenerer", "rotate", "revoke", "nouveau")

    def _envoyer_token(texte):
        sent = bot.send_message(id_user, texte, parse_mode="Markdown")

        def _effacer():
            try:
                bot.delete_message(id_user, sent.message_id)
            except Exception:
                pass

        threading.Timer(120, _effacer).start()

    if regen:
        token = regenerer_api_token(id_user)
        if not token:
            bot.reply_to(message, "Erreur régénération token — réessaie.")
            return
        _envoyer_token(
            "🔄 *Nouveau token API* (l'ancien est révoqué)\n\n"
            f"`{token}`\n\n"
            "_Met à jour l'app web (/app) avec ce token._\n"
            "_Message auto-effacé dans 2 min._"
        )
        return
    conn = db_connect(timeout=5)
    row = conn.execute("SELECT api_token FROM inscriptions WHERE id_telegram = ?", (id_user,)).fetchone()
    conn.close()
    token = row[0] if row and row[0] else regenerer_api_token(id_user)
    if not token:
        bot.reply_to(message, "Erreur génération token — réessaie.")
        return
    _envoyer_token(
        "🔑 *Ton token API Bazo* (confidentiel — ne le partage jamais)\n\n"
        f"`{token}`\n\n"
        "_Utilise-le dans l'app web (/app) ou Authorization: Bearer …_\n"
        "_Pour le révoquer : /token regen · message effacé dans 2 min._"
    )


@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if not est_admin(message):
        bot.reply_to(message, "⛔ Commande réservée à l'administrateur.")
        return
    try:
        envoyer_panneau_admin(message.chat.id)
    except Exception as e:
        logging.exception("[ADMIN] /admin échoué")
        bot.reply_to(message, f"⚠️ Panneau admin indisponible ({type(e).__name__}). Réessaie dans quelques secondes.")


@bot.message_handler(commands=['pending'])
def cmd_pending(message):
    if not est_admin(message):
        return
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute(
            "SELECT id, date, heure, prenom, id_telegram, message FROM rapports_incidents "
            "WHERE statut='en_attente' ORDER BY id DESC LIMIT 15"
        ).fetchall()
        conn.close()
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ Erreur DB : {e}")
        return
    if not rows:
        bot.send_message(message.chat.id, "✅ Aucun signalement en attente.")
        return
    for rid, date_r, heure_r, prenom_r, id_r, msg_r in rows:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Valider", callback_data=f"rap_ok_{rid}"),
            types.InlineKeyboardButton("❌ Rejeter", callback_data=f"rap_no_{rid}"),
        )
        bot.send_message(
            message.chat.id,
            f"🎫 *Rapport #{rid}*\n👤 {prenom_r} (`{id_r}`)\n📅 {date_r} {heure_r}\n\n{(msg_r or '')[:3500]}",
            parse_mode="Markdown",
            reply_markup=markup,
        )


@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if not est_admin(message):
        return
    s = stats_rapport_db()
    if not s:
        bot.send_message(message.chat.id, "⚠️ Impossible de lire les statistiques.")
        return
    txt = (
        "📊 *Statistiques Bazo*\n\n"
        f"👥 Inscrits : *{s.get('inscrits', 0)}* (alertes actives : {s.get('alertes_on', 0)})\n"
        f"🚆 Trajets validés (total) : *{s.get('trajets_valides', 0)}*\n"
        f"📅 Déclarés ce mois : *{s.get('tv_mois', 0)}*"
        f" (validés : *{s.get('tv_valides_mois', 0)}*, en attente : *{s.get('tv_en_attente_mois', 0)}*)\n"
        f"❌ Suppressions ce mois : *{s.get('suppr_mois', 0)}*\n"
        f"💰 Éligibles GF (≥{SEUIL_GF_RETARD_MIN} min ou ≥{SEUIL_GF_SUPPR} suppr.) : *{s.get('eligible_gf', 0)}*\n"
        f"⚠️ Incidents radar aujourd'hui : *{s.get('incidents_jour', 0)}*\n"
        f"📚 Historique total : *{s.get('historique', 0)}* lignes\n"
        f"🧹 Parasites OCESN : *{s.get('ocesn_restants', 0)}*\n"
        f"🎫 Signalements validés : *{s.get('rap_valides', 0)}* | en attente : *{s.get('pending_rapports', 0)}*"
    )
    bot.send_message(message.chat.id, txt, parse_mode="Markdown")


def _relancer_service(service):
    """Relance un service géré par systemd (bazo-radar / bazo-bot), de façon détachée."""
    unit = {"radar": "bazo-radar", "bot": "bazo-bot"}[service]
    # Détaché + délai pour répondre avant que systemd ne redémarre (le bot peut se relancer lui-même).
    script = f"sleep 2; sudo systemctl restart {unit}"
    subprocess.Popen(["bash", "-c", script], cwd=os.path.dirname(os.path.abspath(__file__)),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@bot.message_handler(commands=['pdfglobal'])
def cmd_pdfglobal(message):
    """Admin : export PDF radar global (historique), distinct du PDF personnel."""
    if not est_admin(message):
        return
    bot.send_message(message.chat.id, "⏳ Génération PDF radar global...", reply_markup=menu_principal())
    generer_et_envoyer_pdf(
        message.chat.id, "Admin_Global",
        mode_admin=True,
        mois_filtre=datetime.now(tz_paris).strftime("%Y-%m"),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_"))
def gestion_admin_boutons(call):
    if not est_admin(call.message.chat.id):
        bot.answer_callback_query(call.id, "⛔ Admin uniquement.")
        return
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if call.data == "adm_refresh":
        bot.answer_callback_query(call.id, "Actualisation…")
        envoyer_panneau_admin(chat_id, edit_message_id=msg_id)
        return

    if call.data == "adm_stats":
        bot.answer_callback_query(call.id)
        cmd_stats(call.message)
        return

    if call.data == "adm_pending":
        bot.answer_callback_query(call.id, "Chargement…")
        cmd_pending(call.message)
        return

    if call.data == "adm_pdfglobal":
        bot.answer_callback_query(call.id, "Génération PDF global…")
        generer_et_envoyer_pdf(
            chat_id, "Admin_Global", mode_admin=True,
            mois_filtre=datetime.now(tz_paris).strftime("%Y-%m"),
        )
        return

    if call.data == "adm_pdfperso":
        bot.answer_callback_query(call.id, "Génération PDF perso…")
        user = charger_utilisateur(chat_id) or {}
        mois = datetime.now(tz_paris).strftime("%Y-%m")
        generer_et_envoyer_pdf(
            chat_id,
            f"{user.get('prenom', 'Admin')} {user.get('nom', '')}".strip(),
            user.get("gare_depart"), user.get("heures"),
            user.get("gare_retour"), user.get("heures_retour"),
            mois_filtre=mois,
        )
        return

    if call.data == "adm_rel_radar":
        _relancer_service("radar")
        bot.answer_callback_query(call.id, "Radar : redémarrage…")
        bot.send_message(chat_id, "🔄 *bazo-radar* redémarré.", parse_mode="Markdown")
        return

    if call.data == "adm_rel_bot":
        bot.answer_callback_query(call.id, "Bot : redémarrage dans 2s…")
        bot.send_message(chat_id, "🔄 *bazo-bot* va redémarrer…", parse_mode="Markdown")
        _relancer_service("bot")
        return

    if call.data == "adm_health":
        bot.answer_callback_query(call.id, "Healthcheck…")
        try:
            r = subprocess.run(
                ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "healthcheck_bazo.py")],
                capture_output=True, text=True, timeout=45, cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            sortie = (r.stdout or r.stderr or "Aucune sortie")[-3500:]
            bot.send_message(chat_id, f"🩺 *Healthcheck*\n```\n{sortie}\n```", parse_mode="Markdown")
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Healthcheck échoué : {e}")
        return

    if call.data == "adm_repair":
        bot.answer_callback_query(call.id, "Réparation…")
        mois = datetime.now(tz_paris).strftime("%Y-%m")
        n = reparer_tous_utilisateurs_mois(mois)
        bot.send_message(
            chat_id,
            f"🔧 Réparation *{libelle_mois(mois)}* terminée — *{n}* utilisateur(s) synchronisé(s).",
            parse_mode="Markdown",
        )
        envoyer_panneau_admin(chat_id)
        return

    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['relancer'])
def cmd_relancer(message):
    if not est_admin(message):
        return
    parts = (message.text or "").split()
    cible = parts[1].lower() if len(parts) > 1 else "radar"
    if cible not in ("radar", "bot", "all"):
        bot.send_message(message.chat.id, "Usage : /relancer radar | bot | all")
        return
    services = ["radar", "bot"] if cible == "all" else [cible]
    for s in services:
        _relancer_service(s)
    bot.send_message(
        message.chat.id,
        f"🔄 Redémarrage demandé : *{', '.join(services)}*.\n_Effectif dans ~2 secondes._",
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('rap_ok_') or call.data.startswith('rap_no_'))
def gestion_moderation_rapport(call):
    """Modération admin d'un signalement : valider (-> dossier PDF) ou rejeter."""
    if str(call.message.chat.id) != str(ADMIN_CHAT_ID):
        bot.answer_callback_query(call.id, "⛔ Action réservée à l'admin.")
        return
    valider = call.data.startswith('rap_ok_')
    try:
        rapport_id = int(call.data.rsplit('_', 1)[-1])
    except ValueError:
        bot.answer_callback_query(call.id, "ID invalide.")
        return
    now = datetime.now(tz_paris)
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT id_telegram, prenom, message, statut FROM rapports_incidents WHERE id = ?", (rapport_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            bot.answer_callback_query(call.id, "Rapport introuvable.")
            return
        id_user_rap, prenom_rap, message_rap, statut_actuel = row
        if statut_actuel != 'en_attente':
            conn.close()
            bot.answer_callback_query(call.id, f"Déjà traité ({statut_actuel}).")
            return
        nouveau_statut = 'valide' if valider else 'rejete'
        cur.execute(
            "UPDATE rapports_incidents SET statut = ?, traite_par = ?, date_traitement = ? WHERE id = ?",
            (nouveau_statut, str(ADMIN_CHAT_ID), now.strftime("%Y-%m-%d %H:%M:%S"), rapport_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"[MODERATION] Erreur DB rapport #{rapport_id} : {e}")
        bot.answer_callback_query(call.id, "Erreur base de données.")
        return

    # Met à jour le message admin (retire les boutons)
    icone = "✅ VALIDÉ" if valider else "❌ REJETÉ"
    try:
        bot.edit_message_text(
            f"{icone} — Rapport #{rapport_id}\n👤 {prenom_rap} (`{id_user_rap}`)\n\n{(message_rap or '')[:3500]}",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Rapport validé." if valider else "Rapport rejeté.")

    # Notifie l'usager
    try:
        if valider:
            bot.send_message(
                int(id_user_rap),
                "✅ *Ton signalement a été validé.*\nIl figurera désormais dans ton dossier PDF de réclamation.",
                parse_mode="Markdown",
            )
        else:
            bot.send_message(
                int(id_user_rap),
                "ℹ️ Ton signalement a été examiné mais n'a pas pu être retenu pour le dossier.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logging.error(f"[MODERATION] Notification usager échouée : {e}")


def menu_propositions_radar(propositions, max_items=5):
    """Boutons « j'étais dans ce train » / « non » pour les propositions radar (règle R3)."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    for p in propositions[:max_items]:
        lib_type = "supprimé" if p.get("type_incident") == "supprime" else f"+{p.get('retard_min', 0)} min"
        try:
            jour = datetime.strptime(p["date"], "%Y-%m-%d").strftime("%d/%m")
        except Exception:
            jour = p.get("date", "")
        titre = f"{jour} {p.get('heure_theo', '')} · {p.get('gare_depart', '')} → {p.get('gare_arrivee', '')} · {lib_type}"
        markup.add(types.InlineKeyboardButton(titre[:60], callback_data="prop_info"))
        markup.add(
            types.InlineKeyboardButton("✅ J'étais dans ce train", callback_data=f"prop_ok_{p['id']}"),
            types.InlineKeyboardButton("❌ Non", callback_data=f"prop_non_{p['id']}"),
        )
    return markup


@bot.callback_query_handler(func=lambda call: call.data.startswith("prop_"))
def gestion_propositions_radar(call):
    id_user = str(call.message.chat.id)
    if call.data == "prop_info":
        bot.answer_callback_query(call.id, "Train perturbé vu par le radar dans ton créneau.")
        return
    try:
        action, pid = call.data.split("_", 2)[1], int(call.data.rsplit("_", 1)[1])
    except Exception:
        bot.answer_callback_query(call.id, "Action invalide.")
        return
    if action == "ok":
        tid = accepter_proposition_radar(pid, id_user)
        texte = "✅ Train ajouté à ton attestation." if tid else "⚠️ Proposition introuvable ou déjà traitée."
    else:
        ok = refuser_proposition_radar(pid, id_user)
        texte = "👌 Noté, ce train n'apparaîtra pas." if ok else "⚠️ Proposition introuvable ou déjà traitée."
    bot.answer_callback_query(call.id, texte)
    restantes = lister_propositions_radar(id_user, datetime.now(tz_paris).strftime("%Y-%m"))
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_propositions_radar(restantes) if restantes else None,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "menu_principal")
def callback_menu_principal(call):
    id_user = str(call.message.chat.id)
    nettoyer_etat_utilisateur(id_user)
    bot.answer_callback_query(call.id)
    user = charger_utilisateur(id_user)
    envoyer_menu_accueil(call.message.chat.id, user, edit_message_id=call.message.message_id)


@bot.message_handler(commands=['aide', 'help'])
def cmd_aide(message):
    bot.send_message(message.chat.id, message_guide_client(), parse_mode="Markdown", reply_markup=menu_principal())


@bot.message_handler(commands=['statut', 'status'])
def cmd_statut(message):
    message.text = "📊 Mon Statut"
    gestion_boutons(message)


@bot.message_handler(commands=['trajet', 'checkin'])
def cmd_trajet(message):
    """Raccourci vers le check-in matinal."""
    message.text = "🚆 Déclarer mon trajet"
    gestion_boutons(message)


@bot.message_handler(commands=['start'])
def start(message):
    id_user = str(message.chat.id)
    nettoyer_etat_utilisateur(id_user)
    user_data = charger_utilisateur(id_user)

    disclaimer = (
        "ℹ️ *Mentions légales & RGPD :*\n"
        "L'Assistant Bazo est un service tiers totalement indépendant et n'est aucunement affilié, "
        "partenaire ou rattaché au groupe SNCF ou à ses filiales.\n\n"
        "Vos données personnelles sont stockées de manière sécurisée sur un serveur "
        "localisé en France et servent exclusivement à l'envoi de vos alertes personnalisées."
    )
    bot.send_message(message.chat.id, disclaimer, parse_mode="Markdown")

    if id_user == ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, message_accueil("Admin"), reply_markup=menu_principal())
        envoyer_panneau_admin(message.chat.id)
        return

    if user_data and utilisateur_a_prenom(user_data):
        accueil = message_accueil(user_data["prenom"])
        if profil_gares_configure(user_data):
            accueil += (
                "\n\n💡 *Routine :* alertes sur tes créneaux 2h (automatique). "
                f"PDF en fin de mois (📂). Bouton *{BOUTON_AIDE}* si besoin."
            )
        else:
            accueil += (
                "\n\n⚙️ *Ton profil n'est pas complet* — configure ton aller-retour "
                "via *Configuration Aller-Retour* pour recevoir les alertes et le PDF."
            )
        bot.send_message(message.chat.id, accueil, reply_markup=menu_principal(), parse_mode="Markdown")
        return

    debut_inscription(message.chat.id, message.from_user)

def chercher_gare_tunnel(message, prefixe):
    id_user = str(message.chat.id)
    if est_annulation(message.text):
        annuler_et_revenir_menu(message.chat.id)
        return
    recherche = message.text.lower().strip()
    resultats = [g for g in TOUTES_LES_GARES if recherche in g.lower()]
    
    if not resultats:
        msg = bot.send_message(message.chat.id, "❌ Gare introuvable. Écris de nouveau le nom (ex: 'Met' pour Metz) :")
        bot.register_next_step_handler(msg, chercher_gare_tunnel, prefixe)
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for res in resultats[:5]:
        markup.add(types.InlineKeyboardButton(f"✅ Confirmer : {res}", callback_data=f"{prefixe}_{res}"))
    ajouter_bouton_retour(markup)
    bot.send_message(message.chat.id, "Voici les gares correspondantes, clique sur la bonne :", reply_markup=markup)

@bot.message_handler(commands=['reponse'])
def repondre_utilisateur(message):
    id_user = str(message.chat.id)
    if id_user == ADMIN_CHAT_ID:
        try:
            parties = message.text.split(' ', 2)
            bot.send_message(parties[1], f"✉️ *Reponse de l'assistance :*\n\n{parties[2]}", parse_mode="Markdown")
            bot.reply_to(message, "✅ Envoyé.")
        except:
            bot.reply_to(message, "Usage : /reponse ID message")

@bot.message_handler(commands=['montrain'])
def commande_montrain(message):
    """Déclare le train habituel (ex. /montrain 08h33) pour les alertes personnalisées (grève, suppression)."""
    try:
        import bazo_greve
        rep = bazo_greve.traiter_commande_montrain(str(message.chat.id), message.text or "")
    except Exception:
        logger_checkin.exception("[MONTRAIN] échec")
        rep = "Désolé, je n'ai pas pu enregistrer ton train pour le moment. Réessaie dans quelques minutes."
    bot.send_message(message.chat.id, rep, parse_mode="Markdown")


@bot.message_handler(commands=['retard'])
def commande_retard(message):
    id_user = str(message.chat.id)
    user = charger_utilisateur(id_user)
    markup = types.InlineKeyboardMarkup(row_width=1)

    if user:
        if user.get('gare_depart') and user.get('gare_depart') != "Non precise":
            markup.add(types.InlineKeyboardButton(f"📍 Ma Gare Aller : {user['gare_depart']}", callback_data=f"live_{user['gare_depart']}"))
        if user.get('gare_retour') and user.get('gare_retour') != "Non precise":
            markup.add(types.InlineKeyboardButton(f"📍 Ma Gare Retour : {user['gare_retour']}", callback_data=f"live_{user['gare_retour']}"))
    
    markup.add(types.InlineKeyboardButton("🚉 Choisir une autre gare", callback_data="live_autre"))
    markup.add(types.InlineKeyboardButton("🌐 Tout le réseau", callback_data="live_all"))
    ajouter_bouton_retour(markup)

    bot.send_message(id_user, "🔄 Connexion au radar Bazo...", reply_markup=menu_principal())
    bot.send_message(
        id_user,
        "🔴 *Trafic radar Bazo*\n\nDonnées officielles SNCF (retards + suppressions).\nQuelle zone vérifier ?",
        reply_markup=markup, parse_mode="Markdown",
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('live_') or call.data.startswith('liveg_') or call.data.startswith('mod_'))
def gestion_live_trafic_et_profil(call):
    if call.data.startswith('mod_'):
        champ = call.data.replace('mod_', '')
        hint = f"\n\n{HINT_ANNULER}"
        if champ == "prenom":
            msg = bot.send_message(call.message.chat.id, f"✍️ Quel est ton nouveau **Prénom** ?{hint}", parse_mode="Markdown")
            bot.register_next_step_handler(msg, maj_profil, "prenom")
        elif champ == "nom":
            msg = bot.send_message(call.message.chat.id, f"✍️ Quel est ton nouveau **Nom** ?{hint}", parse_mode="Markdown")
            bot.register_next_step_handler(msg, maj_profil, "nom")
        elif champ == "age":
            msg = bot.send_message(call.message.chat.id, f"✍️ Précise ton âge ou abonnement (ex: '- de 26 ans', 'Abo Metz-Bettembourg') :{hint}", parse_mode="Markdown")
            bot.register_next_step_handler(msg, maj_profil, "age")
        return

    if call.data == "live_autre":
        msg = bot.send_message(call.message.chat.id, "🔍 Écris le nom de la gare à vérifier :")
        bot.register_next_step_handler(msg, chercher_gare_live)
        return
        
    cible = call.data.replace('live_', '').replace('liveg_', '')
    aujourd_hui = datetime.now().strftime("%Y-%m-%d")
    
    try:
        conn = db_connect(timeout=10)
        if cible == "all":
            rows = conn.execute("""
                SELECT gare, ligne, type, retard_min, heure_theo, heure_reel FROM historique
                WHERE date = ? AND COALESCE(numero_train,'') NOT LIKE 'OCESN%'
                  AND COALESCE(ligne,'') NOT LIKE '%econciliation GTFS%'
                ORDER BY heure_theo DESC LIMIT 15
            """, (aujourd_hui,)).fetchall()
            titre = "🌐 *ÉTAT DU TRAFIC (Tout le réseau) :*\n\n"
        else:
            rows = conn.execute("""
                SELECT gare, ligne, type, retard_min, heure_theo, heure_reel FROM historique
                WHERE date = ? AND gare LIKE ?
                  AND COALESCE(numero_train,'') NOT LIKE 'OCESN%'
                  AND COALESCE(ligne,'') NOT LIKE '%econciliation GTFS%'
                ORDER BY heure_theo DESC LIMIT 10
            """, (aujourd_hui, f"%{cible}%")).fetchall()
            titre = f"📍 *ÉTAT DU TRAFIC ({cible}) :*\n\n"
        conn.close()
        
        if not rows:
            msg = f"{titre}✅ Aucun incident ou retard enregistré aujourd'hui pour cette sélection."
        else:
            msg = titre
            for r in rows:
                is_supprime = "supprim" in str(r[2]).lower()
                if is_supprime:
                    msg += f"❌ *Supprimé* : {r[0]} ➔ {r[1]} (Prévu à {r[4]})\n"
                else:
                    msg += f"⏳ *+{r[3]} min* : {r[0]} ➔ {r[1]} (Prévu: {r[4]} | Départ: {r[5]})\n"
        markup_ret = types.InlineKeyboardMarkup()
        ajouter_bouton_retour(markup_ret)
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id,
                              parse_mode="Markdown", reply_markup=markup_ret)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"[LIVE] Affichage retards en direct échoué : {e}")
        bot.answer_callback_query(call.id, "⚠️ Erreur de récupération.")

def maj_profil(message, champ):
    id_user = str(message.chat.id)
    if est_annulation(message.text):
        nettoyer_etat_utilisateur(id_user)
        user = charger_utilisateur(id_user)
        bot.send_message(
            message.chat.id,
            f"❌ Modification annulée.\n\n{message_accueil(user.get('prenom') if user else None)}",
            reply_markup=menu_principal(),
            parse_mode="Markdown",
        )
        return
    col = normaliser_champ_profil(champ)
    if not col:
        bot.send_message(message.chat.id, "⚠️ Champ invalide.", reply_markup=menu_principal())
        return
    nouvelle_valeur = message.text.strip()
    if mettre_a_jour_inscription(id_user, **{col: nouvelle_valeur}):
        bot.send_message(message.chat.id, "✅ Ton profil a bien été mis à jour !", reply_markup=menu_principal())
    else:
        bot.send_message(message.chat.id, "⚠️ Erreur lors de la mise à jour. Réessaie plus tard.", reply_markup=menu_principal())

def chercher_gare_live(message):
    if est_annulation(message.text):
        nettoyer_etat_utilisateur(str(message.chat.id))
        bot.send_message(message.chat.id, "❌ Recherche annulée.", reply_markup=menu_principal())
        return
    recherche = message.text.lower().strip()
    resultats = [g for g in TOUTES_LES_GARES if recherche in g.lower()]
    if not resultats:
        msg = bot.send_message(message.chat.id, "❌ Introuvable. Réessaie avec un autre nom :")
        bot.register_next_step_handler(msg, chercher_gare_live)
        return
    markup = types.InlineKeyboardMarkup()
    for res in resultats[:5]:
        markup.add(types.InlineKeyboardButton(f"🔎 Vérifier : {res}", callback_data=f"live_{res}"))
    ajouter_bouton_retour(markup)
    bot.send_message(message.chat.id, "Sélectionne la gare :", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('remb_'))
def gestion_choix_remboursement(call):
    """Oriente vers la génération PDF : un seul message guide + texte SNCF envoyé avec le PDF."""
    id_user = str(call.message.chat.id)
    user = charger_utilisateur(id_user)
    if not user:
        bot.answer_callback_query(call.id, "Inscription requise.")
        debut_inscription(call.message.chat.id, call.from_user)
        return
    type_remb = "billet" if call.data == "remb_billet" else "abo"
    mois_dispo = lister_mois_pdf_eligibles(id_user, user)
    if not mois_dispo:
        bot.edit_message_text(
            "📭 Aucune perturbation sur ton axe pour une attestation.\n"
            "Configure tes gares (⚙️) — Bazo surveille tout ton trajet automatiquement.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=ajouter_bouton_retour(types.InlineKeyboardMarkup()),
        )
        bot.answer_callback_query(call.id)
        return
    set_etat_checkin(id_user, {"phase": "choix_mois_pdf", "type_reclamation": type_remb})
    lib = "Garantie G30 (billet)" if type_remb == "billet" else "Garantie Fiabilité (abonnement)"
    bot.edit_message_text(
        f"✅ Parcours *{lib}* enregistré.\n\n"
        "📂 Choisis le mois : le bot t'enverra **le PDF et le texte SNCF à copier-coller dans le même envoi**.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=menu_mois_pdf(mois_dispo),
        parse_mode="Markdown",
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: True)
def gestion_boutons(message):
    id_user = str(message.chat.id)
    user_data = charger_utilisateur(id_user)

    if message.text in (BOUTON_ANNULER, BOUTON_MENU):
        annuler_et_revenir_menu(message.chat.id)
        return

    if message.text == BOUTON_AIDE:
        bot.send_message(
            id_user,
            message_guide_client(),
            reply_markup=menu_principal(),
            parse_mode="Markdown",
        )
        return

    if message.text == '📊 Mon Statut':
        if not user_data and id_user != ADMIN_CHAT_ID:
            utilisateur_doit_sinscrire(id_user, message.chat.id, message.from_user)
            return
        u_statut = user_data or charger_utilisateur(id_user)
        if not u_statut:
            utilisateur_doit_sinscrire(id_user, message.chat.id, message.from_user)
            return
        msg_attente = bot.send_message(id_user, "🔄 *Analyse de ton mois en cours...*", parse_mode="Markdown")
        mois_actuel = datetime.now(tz_paris).strftime("%Y-%m")
        incidents_mois, rows_tv = assembler_incidents_pdf(id_user, mois_actuel, u_statut)
        total_retard, total_suppr, nb_incidents, nb_trajets = bilan_depuis_incidents_pdf(
            incidents_mois, len(rows_tv),
        )
        statut_remboursement, conseil = eligibilite_gf_message(total_retard, total_suppr)
        nb_attente = 0
        try:
            conn = db_connect(timeout=5)
            nb_attente = conn.execute(
                "SELECT COUNT(*) FROM trajets_valides WHERE id_telegram=? AND date LIKE ? AND statut_validation='en_attente'",
                (id_user, f"{mois_actuel}%"),
            ).fetchone()[0]
            conn.close()
        except Exception:
            pass
        ligne_attente = (
            f"\n🌆 *Check-outs en attente ce mois :* {nb_attente} _(validation 20h)_\n"
            if nb_attente else ""
        )
        barre = barre_progression_gf(total_retard, total_suppr)
        propositions = lister_propositions_radar(id_user, mois_actuel)
        ligne_props = (
            f"\n🔎 *Trains perturbés vus dans ton créneau, non déclarés :* {len(propositions)}\n"
            f"_Confirme ceux que tu as pris pour les ajouter à ton attestation._\n"
            if propositions else ""
        )
        texte_profil = (
            f"👤 *Profil :* {u_statut.get('prenom', 'Voyageur')} {u_statut.get('nom')}\n"
            f"🎂 *Abonnement :* {u_statut.get('age', 'Non précisé')}\n\n"
            f"🛫 *ALLER :* {u_statut.get('gare_depart')} ({u_statut.get('heures')})\n"
            f"🛬 *RETOUR :* {u_statut.get('gare_retour', 'Non définie')} ({u_statut.get('heures_retour', 'Non définie')})\n\n"
            f"📊 *TON BILAN — {libelle_mois(mois_actuel)}*\n"
            f"🚆 Trajets déclarés : *{nb_trajets}* | Incidents éligibles : *{nb_incidents}*\n"
            f"⏱️ Retards cumulés : *{format_duree_retard(total_retard)}* / {SEUIL_GF_RETARD_MIN} min\n"
            f"❌ Suppressions : *{total_suppr}* / {SEUIL_GF_SUPPR}\n"
            f"📈 Progression GF : {barre}\n"
            f"{ligne_attente}{ligne_props}\n"
            f"💡 {statut_remboursement}\n_{conseil}_"
        )
        if id_user == ADMIN_CHAT_ID:
            texte_profil += "\n\n_👑 Admin : tape /admin pour le panneau de gestion._"
        bot.edit_message_text(
            texte_profil, id_user, msg_attente.message_id, parse_mode="Markdown",
            reply_markup=menu_propositions_radar(propositions) if propositions else None,
        )

    elif message.text == '✏️ Modifier mon profil':
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("👤 Modifier mon Prénom", callback_data="mod_prenom"),
            types.InlineKeyboardButton("📝 Modifier mon Nom", callback_data="mod_nom"),
            types.InlineKeyboardButton("🎂 Modifier Âge / Abonnement", callback_data="mod_age"),
        )
        ajouter_bouton_retour(markup)
        bot.send_message(id_user, "Sélectionne l'information à mettre à jour :", reply_markup=markup)
    
    elif message.text == '📂 Mon PDF Instantané':
        if user_data or id_user == ADMIN_CHAT_ID:
            u = user_data or charger_utilisateur(id_user)
            if not u:
                if utilisateur_doit_sinscrire(id_user, message.chat.id, message.from_user):
                    return
                return
            mois_dispo = lister_mois_pdf_eligibles(id_user, u)
            if not mois_dispo:
                bot.send_message(
                    id_user,
                    "📭 *Aucun mois éligible pour l'instant.*\n\n"
                    "Bazo surveille *tout ton axe* (⚙️ Configuration). "
                    "Dès qu'une perturbation est détectée dans tes créneaux 2h, "
                    "le mois apparaîtra ici — *sans rien à compléter manuellement*.",
                    reply_markup=menu_principal(),
                    parse_mode="Markdown",
                )
                return
            bot.send_message(
                id_user,
                "📂 *Pour quel mois veux-tu ton attestation ?*\n\n"
                "Le dossier est rempli *automatiquement* par le radar "
                "(retards ≥ 10 min et suppressions dans tes créneaux 2h). "
                "Choisis le mois → PDF + kit réclamation envoyés tout de suite.",
                reply_markup=menu_mois_pdf(mois_dispo),
                parse_mode="Markdown",
            )

    elif message.text == '🚆 Déclarer mon trajet':
        if not user_data:
            if utilisateur_doit_sinscrire(id_user, message.chat.id, message.from_user):
                return
            return
        if user_data.get("gare_depart", "Non precise") == "Non precise" or user_data.get("gare_retour", "Non precise") == "Non precise":
            bot.send_message(id_user, "❌ Configure d'abord ton aller-retour via ⚙️ Configuration.", reply_markup=menu_principal())
            return
        bot.send_message(
            id_user,
            "🚆 *Déclarer mon trajet*\n\n"
            "1️⃣ Choisis *aller* ou *retour*\n"
            "2️⃣ Choisis ton *train* dans la liste\n\n"
            "_Tes alertes couvrent tes créneaux 2h configurés (⚙️)._",
            reply_markup=menu_sens_checkin(user_data),
            parse_mode="Markdown",
        )

    elif message.text == '⚠️ Signaler un incident':
        if not user_data:
            if utilisateur_doit_sinscrire(id_user, message.chat.id, message.from_user):
                return
            return
        if user_data.get("gare_depart", "Non precise") == "Non precise" or user_data.get("gare_retour", "Non precise") == "Non precise":
            bot.send_message(id_user, "❌ Configure d'abord ton aller-retour via ⚙️ Configuration.", reply_markup=menu_principal())
            return
        set_etat_checkin(id_user, {"phase": "signalement_sens"})
        bot.send_message(
            id_user,
            "⚠️ *Signaler un incident*\n\n"
            "1️⃣ Choisis *aller* ou *retour*\n"
            "2️⃣ Choisis ton *train* dans la liste (vaut déclaration de trajet)\n"
            "3️⃣ Choisis le type d'incident, photo facultative\n\n"
            f"_{MENTION_COMMUNAUTAIRE}_\n\n"
            "Quel sens ?",
            parse_mode="Markdown",
            reply_markup=menu_sens_signalement(user_data),
        )

    elif message.text == '⚙️ Configuration Aller-Retour':
        bot.send_message(id_user, "🔄 Ouverture des paramètres...", reply_markup=menu_principal())
        msg = bot.send_message(
            id_user,
            f"🛫 Configuration (ALLER).\nÉcris ta **Gare Aller** :\n\n{HINT_ANNULER}",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, chercher_gare_tunnel, "gal")

    elif message.text == '📅 Jours & Vacances':
        if not user_data:
            if utilisateur_doit_sinscrire(id_user, message.chat.id, message.from_user):
                return
            return
        texte = (
            "📅 *Alertes sur mesure*\n\n"
            "Active les jours où tu prends réellement le train (télétravail, 4/5, etc.).\n"
            "Le mode vacances coupe toutes les notifications jusqu'à la date choisie."
        )
        bot.send_message(id_user, texte, reply_markup=menu_preferences_alertes(user_data), parse_mode="Markdown")
        
    elif message.text in ('🔴 Trafic radar', '⚠️ Retards en direct'):
        commande_retard(message)
        
    elif message.text == '💸 Bazo - Ton Allié Réclamation':
        bot.send_message(id_user, "🔄 Ouverture de ton espace juridique...", reply_markup=menu_principal())
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("🎫 Billet TER à l'unité", callback_data="remb_billet"))
        markup.add(types.InlineKeyboardButton("💳 Abonnement (Mensuel / Hebdo / Jeune)", callback_data="remb_abo"))
        ajouter_bouton_retour(markup)
        texte = (
            "💸 *Bazo - Ton Allié Réclamation*\n\n"
            "Choisis ton type de titre : le bot génère ton **PDF d'attestation** et t'envoie "
            "**en une seule fois** la marche à suivre SNCF + le texte exact à copier-coller.\n\n"
            "Quel type de titre de transport possèdes-tu ?"
        )
        bot.send_message(message.chat.id, texte, reply_markup=markup, parse_mode="Markdown")
        
    elif message.text == '📢 Partager à un ami':
        from bazo_metier import texte_invitation_beta

        TEXTE_PARTAGE = texte_invitation_beta()
        bot.send_message(id_user, "🔄 Création de ton lien de partage...", reply_markup=menu_principal())
        bot.send_message(
            id_user, 
            "📲 *Pour partager via SMS ou WhatsApp :*\n"
            "Tapote une fois sur le texte ci-dessous pour le copier automatiquement, puis colle-le à ton ami !", 
            parse_mode="Markdown"
        )
        bot.send_message(
            id_user,
            f"`{TEXTE_PARTAGE}`",
            parse_mode="Markdown"
        )
        markup_partage = types.InlineKeyboardMarkup()
        bouton_tg = types.InlineKeyboardButton(text="✈️ Partager via Telegram", switch_inline_query=TEXTE_PARTAGE)
        markup_partage.add(bouton_tg)
        ajouter_bouton_retour(markup_partage)
        bot.send_message(id_user, "🔵 *Si ton ami a déjà Telegram*, utilise ce bouton :", reply_markup=markup_partage)

    elif message.text == '💬 Contact / Bug':
        bot.send_message(
            id_user,
            "💬 *Contact / Bug*\n\nDécris ton problème en un seul message.\n"
            f"{HINT_ANNULER}",
            reply_markup=menu_principal(),
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(message, transmettre_admin)

    else:
        etat = get_etat_checkin(id_user)
        if etat and etat.get("phase") in (
            "onboarding_prenom", "signalement_voie", "signalement_photo", "completer_mois",
            "completer_jour", "completer_train",
        ):
            return
        bot.send_message(
            id_user,
            "👆 *Tout se fait avec les boutons en bas de l'écran.*\n\n"
            f"• *{BOUTON_AIDE}* — mode d'emploi\n"
            f"• *{BOUTON_ANNULER}* — revenir au menu\n\n"
            "_Pas besoin de taper de commandes._",
            reply_markup=menu_principal(),
            parse_mode="Markdown",
        )

# ==========================================
# 4. TUNNEL D'INSCRIPTION INFRASTRUCTURE
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith('gal_') or call.data.startswith('gre_') or call.data.startswith('nav_') or call.data.startswith('set_'))
def tunnel_inscription(call):
    id_user = str(call.message.chat.id)
    nom_def = f"{call.from_user.first_name or ''} {call.from_user.last_name or ''}".strip() or "Voyageur"
    creer_inscription_si_absent(id_user, nom=nom_def)
    user = charger_utilisateur(id_user)

    if call.data.startswith('gal_'):
        mettre_a_jour_inscription(id_user, gare_depart=call.data.replace('gal_', ''))
        user = charger_utilisateur(id_user)
        bot.edit_message_text(f"🛫 Gare Aller : *{user['gare_depart']}*\nPériode :", call.message.chat.id, call.message.message_id, reply_markup=menu_choix_periode("all"), parse_mode="Markdown")
        
    elif call.data.startswith('gre_'):
        mettre_a_jour_inscription(id_user, gare_retour=call.data.replace('gre_', ''))
        user = charger_utilisateur(id_user)
        bot.edit_message_text(f"🛬 Gare Retour : *{user['gare_retour']}*\nPériode :", call.message.chat.id, call.message.message_id, reply_markup=menu_choix_periode("ret"), parse_mode="Markdown")
        
    elif call.data.startswith('nav_'):
        is_matin = "m_" in call.data
        periode_txt = "Matin" if is_matin else "Soir"
        if "all" in call.data:
            bot.edit_message_text(f"Créneau du {periode_txt} (Gare Aller) :", call.message.chat.id, call.message.message_id, reply_markup=menu_heures_matin("all") if is_matin else menu_heures_soir("all"))
        else:
            bot.edit_message_text(f"Créneau du {periode_txt} (Gare Retour) :", call.message.chat.id, call.message.message_id, reply_markup=menu_heures_matin("ret") if is_matin else menu_heures_soir("ret"))
            
    elif call.data.startswith('set_all_'):
        mettre_a_jour_inscription(id_user, heures=call.data.replace('set_all_', ''))
        user = charger_utilisateur(id_user)
        bot.edit_message_text(f"✅ Horaires Aller : *{user['heures']}*\n\n🛬 Configuration (RETOUR).", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        msg = bot.send_message(call.message.chat.id, "Écris ta **Gare Retour** (ex: Metz) :", parse_mode="Markdown")
        bot.register_next_step_handler(msg, chercher_gare_tunnel, "gre")
        
    elif call.data.startswith('set_ret_'):
        mettre_a_jour_inscription(id_user, heures_retour=call.data.replace('set_ret_', ''))
        user = charger_utilisateur(id_user)
        bot.edit_message_text(
            f"🚀 *C'est parti, {user.get('prenom', '')} !*\n\n"
            f"🛫 Aller : {user['gare_depart']} ({user['heures']})\n"
            f"🛬 Retour : {user['gare_retour']} ({user['heures_retour']})\n\n"
            f"_Bazo surveille tes axes et t'alerte si ton train est touché._",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        mettre_a_jour_inscription(id_user, jours_actifs=user.get('jours_actifs', '0,1,2,3,4'), alertes_actives=1)
        user = charger_utilisateur(id_user)
        envoyer_menu_accueil(call.message.chat.id, user)
        bot.send_message(call.message.chat.id, message_guide_client(), parse_mode="Markdown")

@bot.message_handler(func=lambda m: get_etat_checkin(m.chat.id) and get_etat_checkin(m.chat.id).get("phase") == "onboarding_prenom")
def saisie_prenom_onboarding(message):
    id_user = str(message.chat.id)
    if est_annulation(message.text):
        annuler_et_revenir_menu(id_user)
        return
    prenom = sanitiser_chaine(message.text).strip()
    if len(prenom) < 2:
        bot.send_message(
            id_user,
            f"❌ Prénom trop court. Réessaie (ex: Marie).\n\n{HINT_ANNULER}",
            parse_mode="Markdown",
        )
        return
    pop_etat_checkin(id_user)
    nom_def = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip() or "Voyageur"
    nouveau = not charger_utilisateur(id_user)
    creer_inscription_si_absent(id_user, nom=nom_def, prenom=prenom)
    mettre_a_jour_inscription(id_user, prenom=prenom, nom=nom_def)
    user = charger_utilisateur(id_user)
    if nouveau:
        try:
            bot.send_message(
                ADMIN_CHAT_ID,
                f"🆕 *Nouvel inscrit Bazo*\n👤 {prenom} (`{id_user}`)",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    if profil_gares_configure(user):
        bot.send_message(id_user, message_accueil(prenom), reply_markup=menu_principal(), parse_mode="Markdown")
    else:
        msg = bot.send_message(
            id_user,
            f"Enchanté *{prenom}* ! 🎉\n\nPour configurer tes alertes, écris le nom de ta **Gare Aller** (ex: Metz) :\n\n"
            f"{HINT_ANNULER}",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, chercher_gare_tunnel, "gal")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ck_'))
def tunnel_checkin_checkout(call):
    id_user = str(call.message.chat.id)
    user = charger_utilisateur(id_user)

    if call.data == "ck_info":
        bot.answer_callback_query(call.id, modele_grille_jour())
        return

    if call.data.startswith("ck_page_"):
        etat = get_etat_checkin(id_user)
        if not etat or etat.get("phase") != "choix_train":
            bot.answer_callback_query(call.id, "Session expirée.")
            return
        page = int(call.data.replace("ck_page_", ""))
        trains = etat.get("trains", [])
        tranche = etat.get("tranche", "")
        sens = etat.get("sens", "")
        set_etat_checkin(id_user, {"page": page}, merge=True)
        bot.edit_message_text(
            f"🕐 Créneau *{tranche}* ({sens}) — page {page + 1}\nSélectionne ton train exact :",
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_trains_checkin(trains, page=page),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    if call.data in ("ck_sens_aller", "ck_sens_retour"):
        if not user:
            bot.answer_callback_query(call.id, "Profil requis.")
            return
        sens = "aller" if call.data == "ck_sens_aller" else "retour"
        g_dep, g_arr = gares_pour_sens(user, sens)
        bot.answer_callback_query(call.id, "🔍 Chargement des trains…")
        bot.edit_message_text(
            f"⏳ Trains *{g_dep} → {g_arr}* ({sens})…",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        trains = _charger_trains_apres_sens(user, sens)
        tranche = _tranche_profil_utilisateur(user, sens)
        if not trains:
            bot.edit_message_text(
                f"❌ Aucun train trouvé pour *{g_dep} → {g_arr}*.\nRéessaie dans quelques minutes.",
                call.message.chat.id, call.message.message_id,
                reply_markup=menu_sens_checkin(user), parse_mode="Markdown",
            )
            return
        set_etat_checkin(id_user, {"phase": "choix_train", "tranche": tranche, "sens": sens, "trains": trains})
        bot.edit_message_text(
            f"🚆 *{g_dep} → {g_arr}* ({sens})\nChoisis ton train :",
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_trains_checkin(trains), parse_mode="Markdown",
        )
        return

    if call.data.startswith("ck_tr_"):
        if not user:
            bot.answer_callback_query(call.id, "Profil requis.")
            return
        etat = get_etat_checkin(id_user) or {}
        sens = etat.get("sens")
        if not sens:
            bot.answer_callback_query(call.id, "Choisis d'abord le sens du trajet.")
            return
        tranche = call.data.replace("ck_tr_", "")
        g_dep, g_arr = gares_pour_sens(user, sens)
        bot.answer_callback_query(call.id, "🔍 Recherche des trains...")
        bot.edit_message_text(
            f"⏳ Recherche des trains *{g_dep} → {g_arr}* ({tranche})…",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        trains = recuperer_trains_theoriques(g_dep, g_arr, tranche, sens=sens)
        if not trains:
            bot.answer_callback_query(call.id, "Aucun train trouvé.")
            bot.edit_message_text(
                f"❌ *Aucun train trouvé* pour *{g_dep} → {g_arr}* ({tranche}).\n\n"
                f"Causes possibles :\n"
                f"• Quota API SNCF temporairement saturé (réessaie dans 15 min)\n"
                f"• L'API SNCF est temporairement indisponible\n"
                f"• Aucun train à cette tranche aujourd'hui\n"
                f"• Profil à vérifier (⚙️ Configuration)\n\n"
                f"Réessaie dans quelques instants ou choisis une autre tranche.",
                call.message.chat.id, call.message.message_id,
                reply_markup=menu_tranches_checkin(sens),
                parse_mode="Markdown",
            )
            return
        set_etat_checkin(id_user, {"phase": "choix_train", "tranche": tranche, "sens": sens, "trains": trains})
        bot.edit_message_text(
            f"🕐 Créneau *{tranche}* ({sens})\nSélectionne ton train exact :",
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_trains_checkin(trains),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ck_pick_"):
        etat = get_etat_checkin(id_user)
        if not etat or etat.get("phase") != "choix_train":
            bot.answer_callback_query(call.id, "Session expirée, recommence.")
            return
        idx = int(call.data.replace("ck_pick_", ""))
        trains = etat.get("trains", [])
        if idx >= len(trains):
            bot.answer_callback_query(call.id, "Train invalide.")
            return
        train = trains[idx]
        enregistrer_checkin(id_user, train, etat["tranche"], etat["sens"], user)
        pop_etat_checkin(id_user)
        arr = train.get("heure_arrivee_theo") or "?"
        markup_fil = types.InlineKeyboardMarkup()
        markup_fil.add(types.InlineKeyboardButton(
            "🗣 Fil du train (signalements voyageurs)",
            callback_data=_cle_fil(datetime.now(tz_paris).strftime("%Y-%m-%d"), train["numero_train"], train["heure_theo"]),
        ))
        bot.edit_message_text(
            f"✅ *Check-in enregistré !*\n\n"
            f"🚆 Train *{train['numero_train']}* — départ *{train['heure_theo']}* → arrivée *{arr}*\n"
            f"📍 {train['gare_depart']} → {train.get('gare_arrivee') or train['destination']}\n\n"
            f"🔔 Si ce train est retardé ou supprimé, *tu seras alerté*.\n"
            f"🌆 Ce soir vers 20h : validation en 1 clic (retard auto).\n\n"
            f"_👇 Utilise le menu clavier pour continuer._",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown", reply_markup=markup_fil,
        )
        bot.answer_callback_query(call.id, "Check-in enregistré.")
        return

    if call.data == "ck_info_more":
        bot.answer_callback_query(call.id, "Affine ta tranche horaire dans Configuration pour voir plus de trains.")
        return

    if call.data.startswith("ck_out_ok_"):
        tid = int(call.data.replace("ck_out_ok_", ""))
        if not _verifier_proprietaire_trajet(tid, id_user):
            bot.answer_callback_query(call.id, "⛔ Ce trajet ne t'appartient pas.")
            return
        infos = _infos_trajet(tid)
        if not infos:
            bot.answer_callback_query(call.id, "Trajet introuvable — déjà validé ou expiré.")
            return
        retard, supprime = retard_reel_scanner(infos["date"], infos["numero_train"], infos["gare_arrivee"])
        if supprime:
            finaliser_validation_trajet(tid, "valide_supprime", retard_min=0, motif="Train supprimé (annulation SNCF)")
            bot.edit_message_text(
                "❌ Le radar indique que ton train était *supprimé*.\n"
                "_Comptabilisé pour ta Garantie Fiabilité._\n\n_👇 Menu clavier pour continuer._",
                call.message.chat.id, call.message.message_id, parse_mode="Markdown",
            )
            bot.answer_callback_query(call.id)
            return
        finaliser_validation_trajet(tid, "valide_normal", retard_min=retard)
        if retard >= SEUIL_RETARD_MIN:
            txt = (f"✅ Trajet validé.\n⏱ Retard réel à l'arrivée (détecté par Bazo) : *{retard} min*\n"
                   f"_Comptabilisé pour ta réclamation._")
        else:
            txt = f"✅ Trajet validé — aucun retard significatif détecté ({retard} min). Bonne soirée !"
        bot.edit_message_text(txt + "\n\n_👇 Menu clavier pour continuer._",
                              call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ck_out_suppr_"):
        tid = int(call.data.replace("ck_out_suppr_", ""))
        if not _verifier_proprietaire_trajet(tid, id_user):
            bot.answer_callback_query(call.id, "⛔ Ce trajet ne t'appartient pas.")
            return
        finaliser_validation_trajet(tid, "valide_supprime", retard_min=0, motif="Train supprimé (annulation SNCF)")
        bot.edit_message_text(
            "❌ Noté : ton train était *supprimé*.\n"
            "_Comptabilisé pour ta Garantie Fiabilité / réclamation TER._\n\n"
            "_👇 Menu clavier pour continuer._",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ck_out_suiv_"):
        tid = int(call.data.replace("ck_out_suiv_", ""))
        if not _verifier_proprietaire_trajet(tid, id_user):
            bot.answer_callback_query(call.id, "⛔ Ce trajet ne t'appartient pas.")
            return
        infos = _infos_trajet(tid)
        if not infos:
            bot.answer_callback_query(call.id, "Trajet introuvable.")
            return
        retard = estimer_retard_train_suivant(
            infos["date"], infos["numero_train"], infos["heure_arrivee_theo"], infos["gare_arrivee"]
        ) if infos else 0
        finaliser_validation_trajet(tid, "valide_normal", retard_min=retard,
                                    motif="Prise du train suivant (retard en cascade)")
        if retard >= SEUIL_RETARD_MIN:
            txt = (f"⏭ Noté : tu as pris le train suivant.\n⏱ Retard estimé à l'arrivée : *{retard} min*\n"
                   f"_Comptabilisé pour ta réclamation._")
        else:
            txt = f"⏭ Noté : tu as pris le train suivant ({retard} min de décalage estimé)."
        bot.edit_message_text(txt + "\n\n_👇 Menu clavier pour continuer._",
                              call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ck_out_bus_"):
        tid = int(call.data.replace("ck_out_bus_", ""))
        if not _verifier_proprietaire_trajet(tid, id_user):
            bot.answer_callback_query(call.id, "⛔ Ce trajet ne t'appartient pas.")
            return
        infos = _infos_trajet(tid)
        if not infos:
            bot.answer_callback_query(call.id, "Trajet introuvable.")
            return
        retard, _ = retard_reel_scanner(infos["date"], infos["numero_train"], infos["gare_arrivee"])
        finaliser_validation_trajet(tid, "valide_bus_panne", retard_min=max(retard, 10), motif=MOTIF_RUPTURE_CHARGE)
        bot.edit_message_text(
            f"🚌 Noté : panne / bus de substitution.\n⏱ Retard retenu : *{max(retard, 10)} min*\n"
            f"📝 Motif PDF : _{MOTIF_RUPTURE_CHARGE}_\n\n"
            f"_👇 Menu clavier pour continuer._",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ck_out_rien_"):
        tid = int(call.data.replace("ck_out_rien_", ""))
        if not _verifier_proprietaire_trajet(tid, id_user):
            bot.answer_callback_query(call.id, "⛔ Ce trajet ne t'appartient pas.")
            return
        finaliser_validation_trajet(tid, "annule", retard_min=0)
        bot.edit_message_text(
            "🙅 Noté : tu n'as pas voyagé. Ce trajet ne sera pas comptabilisé.\n\n"
            "_👇 Menu clavier pour continuer._",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return


def _lancer_pdf_mois(id_user, user, mois_code, type_reclamation, edit_message_id):
    preparer_dossier_pdf_mois(id_user, mois_code, user)
    nom_pdf = f"{user.get('prenom', '')} {user.get('nom', '')}".strip() or user["nom"]
    generer_et_envoyer_pdf(
        id_user, nom_pdf, user["gare_depart"], user["heures"],
        user.get("gare_retour"), user.get("heures_retour"),
        mois_filtre=mois_code,
        type_reclamation=type_reclamation,
        edit_message_id=edit_message_id,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('pdf_mois_'))
def choix_mois_pdf(call):
    id_user = str(call.message.chat.id)
    user = charger_utilisateur(id_user)
    if not user:
        bot.answer_callback_query(call.id, "Profil requis.")
        return
    etat = get_etat_checkin(id_user) or {}
    type_reclamation = etat.get("type_reclamation") or inferer_type_reclamation(user)
    mois_code = call.data.replace("pdf_mois_", "")
    pop_etat_checkin(id_user)
    bot.answer_callback_query(call.id, "Génération en cours...")
    bot.edit_message_text(
        f"⏳ Génération automatique du PDF + kit réclamation pour *{libelle_mois(mois_code)}*...",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
    )
    _lancer_pdf_mois(id_user, user, mois_code, type_reclamation, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("comp_"))
def tunnel_completer_dossier(call):
    """Complétion manuelle désactivée — tout callback comp_* lance le PDF automatique."""
    id_user = str(call.message.chat.id)
    user = charger_utilisateur(id_user)
    if not user:
        bot.answer_callback_query(call.id, "Profil requis.")
        return
    etat = get_etat_checkin(id_user) or {}
    type_reclamation = etat.get("type_reclamation") or inferer_type_reclamation(user)
    mois_code = etat.get("mois")
    if call.data.startswith("comp_skip_"):
        mois_code = call.data.replace("comp_skip_", "")
    if not mois_code:
        pop_etat_checkin(id_user)
        bot.answer_callback_query(call.id, "Relance 📂 Mon PDF Instantané.")
        bot.edit_message_text(
            "ℹ️ La complétion manuelle n'existe plus.\n\n"
            "Relance *📂 Mon PDF Instantané* : le radar remplit tout automatiquement.",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        return
    pop_etat_checkin(id_user)
    bot.answer_callback_query(call.id, "Génération automatique...")
    bot.edit_message_text(
        f"⏳ Génération automatique du PDF + kit réclamation pour *{libelle_mois(mois_code)}*...",
        call.message.chat.id, call.message.message_id, parse_mode="Markdown",
    )
    _lancer_pdf_mois(id_user, user, mois_code, type_reclamation, call.message.message_id)


def _charger_trajet_signalement(id_trajet, id_user):
    try:
        conn = db_connect(timeout=5)
        row = conn.execute(
            """
            SELECT id, date, numero_train, heure_theo, gare_depart, gare_arrivee
            FROM trajets_valides WHERE id = ? AND id_telegram = ?
            """,
            (int(id_trajet), str(id_user)),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "id": row[0], "date": row[1], "numero_train": row[2], "heure_theo": row[3],
            "gare_depart": row[4], "gare_arrivee": row[5],
        }
    except Exception:
        return None

def _finaliser_signalement(id_user, etat, code_type, valeur=None):
    """
    Crée le signalement communautaire (taxonomie fermée, rattaché au check-in).
    Aucun effet PDF ici : la question « est-ce ton trajet ? » est posée ensuite.
    """
    train = etat.get("train") or {}
    date_j = train.get("date") or datetime.now(tz_paris).strftime("%Y-%m-%d")
    res = creer_signalement(
        id_user, date_j, train.get("numero_train"), train.get("heure_theo"), code_type, valeur=valeur,
    )
    pop_etat_checkin(id_user)
    if not res.get("ok"):
        erreurs = {
            "checkin_requis": "Déclare d'abord ton trajet sur ce train (🚆 Déclarer mon trajet).",
            "fenetre_close": "Ce train est passé depuis plus de 3 h : le signalement n'est plus possible.",
            "type_inconnu": "Type de signalement inconnu.",
            "valeur_requise": "Indique la voie annoncée.",
            "valeur_invalide": "Voie invalide (ex. 2, 3B, A).",
        }
        bot.send_message(
            id_user, f"⚠️ {erreurs.get(res.get('erreur'), 'Signalement impossible pour le moment.')}",
            reply_markup=menu_principal(),
        )
        return None
    sig = res["signalement"]
    demander_pdf = sig.get("effet_pdf_possible", False)
    deja = " (déjà enregistré)" if res.get("existant") else ""
    texte = (
        f"✅ *Signalement enregistré*{deja} : *{sig['libelle']}*\n"
        f"🚆 Train {sig['numero_train']} · {sig['date']} · {sig['heure_theo']}\n"
        f"_{MENTION_COMMUNAUTAIRE}_\n\n"
        "Les autres voyageurs de ce train pourront le confirmer."
    )
    if demander_pdf:
        texte += (
            "\n\n📂 *Est-ce ton trajet du jour ?* Si oui, il est inscrit dans ton attestation avec la mention "
            f"« {LIBELLE_PROVENANCE[PROVENANCE_VOYAGEUR]} »."
        )
    bot.send_message(
        id_user, texte, parse_mode="Markdown", reply_markup=menu_apres_signalement(sig, demander_pdf),
    )
    try:
        bot.send_message(
            ADMIN_CHAT_ID,
            f"🗣 Signalement #{sig['id']} · {sig['libelle']} · train {sig['numero_train']} "
            f"{sig['date']} {sig['heure_theo']} · auteur `{id_user}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    return sig


@bot.callback_query_handler(func=lambda call: call.data.startswith("sig_"))
def tunnel_signalement(call):
    id_user = str(call.message.chat.id)
    user = charger_utilisateur(id_user)
    etat = get_etat_checkin(id_user) or {}

    if call.data in ("sig_sens_aller", "sig_sens_retour"):
        if not user:
            bot.answer_callback_query(call.id, "Profil requis.")
            return
        sens = "aller" if call.data == "sig_sens_aller" else "retour"
        g_dep, g_arr = gares_pour_sens(user, sens)
        bot.answer_callback_query(call.id, "🔍 Chargement des trains…")
        bot.edit_message_text(
            f"⏳ Trains *{g_dep} → {g_arr}* ({sens})…",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown",
        )
        trains = _charger_trains_apres_sens(user, sens)
        tranche = _tranche_profil_utilisateur(user, sens)
        if not trains:
            bot.edit_message_text(
                f"❌ Aucun train trouvé pour *{g_dep} → {g_arr}*.\nRéessaie dans quelques minutes.",
                call.message.chat.id, call.message.message_id,
                reply_markup=menu_sens_signalement(user), parse_mode="Markdown",
            )
            return
        set_etat_checkin(id_user, {
            "phase": "signalement_train", "tranche": tranche, "sens": sens, "trains": trains,
        })
        bot.edit_message_text(
            f"🚆 *{g_dep} → {g_arr}* ({sens})\nQuel train est concerné ?",
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_trains_signalement(trains), parse_mode="Markdown",
        )
        return

    if call.data.startswith("sig_page_"):
        if etat.get("phase") != "signalement_train":
            bot.answer_callback_query(call.id, "Session expirée, recommence.")
            return
        page = int(call.data.replace("sig_page_", ""))
        trains = etat.get("trains", [])
        set_etat_checkin(id_user, {**etat, "page": page})
        bot.edit_message_text(
            f"🕐 Créneau *{etat.get('tranche', '?')}* — page {page + 1}\nSélectionne le train concerné :",
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_trains_signalement(trains, page=page),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("sig_pick_"):
        if not user:
            bot.answer_callback_query(call.id, "Profil requis.")
            return
        if etat.get("phase") != "signalement_train":
            bot.answer_callback_query(call.id, "Session expirée, recommence.")
            return
        idx = int(call.data.replace("sig_pick_", ""))
        trains = etat.get("trains", [])
        if idx >= len(trains):
            bot.answer_callback_query(call.id, "Train invalide.")
            return
        train = dict(trains[idx])
        # Le signalement exige un check-in : choisir le train vaut déclaration du trajet.
        enregistrer_checkin(id_user, train, etat["tranche"], etat["sens"], user)
        train["date"] = datetime.now(tz_paris).strftime("%Y-%m-%d")
        tid = id_trajet_apres_checkin(id_user, train["numero_train"], train["heure_theo"])
        set_etat_checkin(id_user, {
            "phase": "signalement_type", "id_trajet": tid, "train": train,
            "sens": etat["sens"], "tranche": etat["tranche"],
        })
        arr = train.get("heure_arrivee_theo") or "?"
        bot.edit_message_text(
            f"🚆 *Train {train['numero_train']}* — départ *{train['heure_theo']}* → *{arr}*\n"
            f"📍 {train['gare_depart']} → {train.get('gare_arrivee') or train.get('destination')}\n\n"
            "Que se passe-t-il ?",
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_types_signalement(), parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("sig_type_"):
        code = call.data.replace("sig_type_", "")
        if code not in TYPES_SIGNALEMENT:
            bot.answer_callback_query(call.id, "Type inconnu.")
            return
        if etat.get("phase") != "signalement_type" or not etat.get("train"):
            bot.answer_callback_query(call.id, "Choisis d'abord ton train.")
            return
        if TYPES_SIGNALEMENT[code]["valeur"]:
            set_etat_checkin(id_user, {**etat, "phase": "signalement_voie", "code_type": code})
            bot.edit_message_text(
                f"🛤 *Voie annoncée* pour le train {etat['train'].get('numero_train')}\n"
                "Choisis la voie (ou écris-la : 2, 3B, A…).",
                call.message.chat.id, call.message.message_id,
                reply_markup=menu_voies_signalement(), parse_mode="Markdown",
            )
            bot.answer_callback_query(call.id)
            return
        bot.answer_callback_query(call.id, "Enregistrement…")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        _finaliser_signalement(id_user, etat, code)
        return

    if call.data.startswith("sig_voie_"):
        if etat.get("phase") != "signalement_voie":
            bot.answer_callback_query(call.id, "Session expirée, recommence.")
            return
        valeur = call.data.replace("sig_voie_", "")
        bot.answer_callback_query(call.id, "Enregistrement…")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        _finaliser_signalement(id_user, etat, etat.get("code_type"), valeur=valeur)
        return

    if call.data.startswith("sig_photo_"):
        sid = int(call.data.replace("sig_photo_", ""))
        sig = charger_signalement(sid)
        if not sig or str(sig["id_auteur"]) != str(id_user):
            bot.answer_callback_query(call.id, "⛔ Ce signalement ne t'appartient pas.")
            return
        set_etat_checkin(id_user, {"phase": "signalement_photo", "signalement_id": sid})
        bot.send_message(
            id_user,
            "📸 Envoie la photo (panneau, affichage…). Elle est conservée 30 jours, sans métadonnées, "
            f"et n'est visible que des modérateurs.\n\n_{HINT_ANNULER}_",
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("sigpdf_"))
def decision_pdf_signalement(call):
    """Question « est-ce ton trajet ? » : seul le propriétaire déclenche l'effet PDF."""
    id_user = str(call.message.chat.id)
    oui = call.data.startswith("sigpdf_oui_")
    try:
        sid = int(call.data.rsplit("_", 1)[-1])
    except ValueError:
        bot.answer_callback_query(call.id, "Identifiant invalide.")
        return
    sig = charger_signalement(sid)
    if not sig or str(sig["id_auteur"]) != str(id_user):
        bot.answer_callback_query(call.id, "⛔ Ce signalement ne t'appartient pas.")
        return
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=menu_apres_signalement(sig, demander_pdf=False),
        )
    except Exception:
        pass
    if not oui:
        bot.answer_callback_query(call.id, "Compris : rien n'est ajouté à ton attestation.")
        return
    tid = promouvoir_trajet_signalement(
        id_user, sig["code_type"], id_trajet=sig["id_trajet"], signalement_id=sid,
    )
    if tid:
        bot.answer_callback_query(call.id, "Inscrit dans ton attestation.")
        bot.send_message(
            id_user,
            f"📂 Ton trajet du {sig['date']} (train {sig['numero_train']}) est inscrit dans ton attestation "
            f"avec la mention « {LIBELLE_PROVENANCE[PROVENANCE_VOYAGEUR]} ».",
            reply_markup=menu_principal(),
        )
    else:
        bot.answer_callback_query(call.id, "Impossible d'inscrire ce trajet.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("fil_"))
def afficher_fil_train(call):
    id_user = str(call.message.chat.id)
    try:
        _, date_j, numero, heure = call.data.split("_", 3)
    except ValueError:
        bot.answer_callback_query(call.id, "Fil introuvable.")
        return
    expirer_signalements()
    sigs = signalements_du_train(date_j, numero, heure)
    bot.send_message(
        id_user, texte_fil_train(date_j, numero, heure, sigs),
        parse_mode="Markdown", reply_markup=menu_fil_train(sigs, id_user),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("vote_"))
def voter_fil_train(call):
    id_user = str(call.message.chat.id)
    try:
        _, sens_vote, sid = call.data.split("_", 2)
        sid = int(sid)
    except ValueError:
        bot.answer_callback_query(call.id, "Vote invalide.")
        return
    res = voter_signalement(sid, id_user, "confirme" if sens_vote == "c" else "refute")
    if not res.get("ok"):
        msgs = {
            "auteur": "Tu es l'auteur de ce signalement.",
            "checkin_requis": "Seuls les voyageurs ayant déclaré ce train peuvent voter.",
            "deja_vote": "Tu as déjà voté.",
            "expire": "Ce signalement a expiré.",
            "introuvable": "Signalement introuvable.",
        }
        bot.answer_callback_query(call.id, msgs.get(res.get("erreur"), "Vote impossible."), show_alert=True)
        return
    bot.answer_callback_query(call.id, f"Merci ! Statut : {_statut_signalement_txt(res['statut'])}.")
    sig = charger_signalement(sid)
    if sig:
        sigs = signalements_du_train(sig["date"], sig["numero_train"], sig["heure_theo"])
        try:
            bot.edit_message_text(
                texte_fil_train(sig["date"], sig["numero_train"], sig["heure_theo"], sigs),
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=menu_fil_train(sigs, id_user),
            )
        except Exception:
            pass


@bot.message_handler(
    func=lambda m: get_etat_checkin(m.chat.id)
    and get_etat_checkin(m.chat.id).get("phase") in ("signalement_voie", "signalement_photo"),
    content_types=["text", "photo"],
)
def saisie_signalement(message):
    """Seules saisies acceptées : une voie (valeur fermée) ou une photo. Jamais de texte libre public."""
    id_user = str(message.chat.id)
    etat = get_etat_checkin(id_user) or {}
    if message.content_type == "text" and est_annulation(message.text):
        annuler_et_revenir_menu(id_user)
        return
    if etat.get("phase") == "signalement_voie":
        if message.content_type != "text":
            bot.send_message(id_user, "Indique la voie annoncée (ex. 2, 3B, A).")
            return
        _finaliser_signalement(id_user, etat, etat.get("code_type"), valeur=(message.text or "").strip())
        return
    # phase signalement_photo
    if message.content_type != "photo" or not message.photo:
        bot.send_message(id_user, f"Envoie une photo, ou {BOUTON_ANNULER} pour revenir au menu.")
        return
    sid = int(etat.get("signalement_id") or 0)
    sig = charger_signalement(sid)
    pop_etat_checkin(id_user)
    if not sig or str(sig["id_auteur"]) != str(id_user):
        bot.send_message(id_user, "⛔ Signalement introuvable.", reply_markup=menu_principal())
        return
    try:
        _stocker_photo_signalement(message.photo[-1].file_id, sid)
        bot.send_message(id_user, "📸 Photo ajoutée au signalement. Merci !", reply_markup=menu_principal())
    except Exception as e:
        logging.warning("[SIGNALEMENT] photo #%s : %s", sid, e)
        bot.send_message(id_user, "⚠️ Photo non enregistrée, réessaie plus tard.", reply_markup=menu_principal())


@bot.callback_query_handler(func=lambda call: call.data.startswith('pref_'))
def preferences_alertes(call):
    id_user = str(call.message.chat.id)
    user = charger_utilisateur(id_user)
    if not user:
        bot.answer_callback_query(call.id, "Inscription requise.")
        return

    if call.data.startswith("pref_j_"):
        jour = int(call.data.replace("pref_j_", ""))
        jours_raw = str(user.get("jours_actifs") or "0,1,2,3,4")
        jours_set = {int(x.strip()) for x in jours_raw.split(",") if x.strip().isdigit()}
        if jour in jours_set:
            jours_set.discard(jour)
        else:
            jours_set.add(jour)
        if not jours_set:
            bot.answer_callback_query(call.id, "Garde au moins un jour actif.")
            return
        mettre_a_jour_inscription(id_user, jours_actifs=",".join(str(j) for j in sorted(jours_set)))
        user = charger_utilisateur(id_user)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=menu_preferences_alertes(user))
        bot.answer_callback_query(call.id, "Jours mis à jour.")
        return

    if call.data == "pref_vac_7":
        mettre_a_jour_inscription(id_user, vacances_jusquau=(datetime.now(tz_paris) + timedelta(days=7)).strftime("%Y-%m-%d"), alertes_actives=1)
    elif call.data == "pref_vac_14":
        mettre_a_jour_inscription(id_user, vacances_jusquau=(datetime.now(tz_paris) + timedelta(days=14)).strftime("%Y-%m-%d"), alertes_actives=1)
    elif call.data == "pref_vac_off":
        mettre_a_jour_inscription(id_user, vacances_jusquau=None)
    elif call.data == "pref_alert_off":
        mettre_a_jour_inscription(id_user, alertes_actives=0)
    elif call.data == "pref_alert_on":
        mettre_a_jour_inscription(id_user, alertes_actives=1, vacances_jusquau=None)

    user = charger_utilisateur(id_user)
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=menu_preferences_alertes(user))
    bot.answer_callback_query(call.id, "Préférences enregistrées.")

# ==========================================
# 5. GENERATION TECHNIQUE DU PDF
# ==========================================
def construire_message_reclamation(type_reclamation, nom_client, mois_filtre=None):
    """Message unique : marche à suivre SNCF + texte exact à copier-coller (envoyé avec le PDF)."""
    mois_libelle = "le mois concerné"
    periode = ""
    if mois_filtre:
        try:
            y, mo = mois_filtre.split("-")
            mois_libelle = f"{MOIS_FR[int(mo) - 1]} {y}"
            periode = f" — période *{mois_libelle}*"
        except Exception:
            mois_libelle = mois_filtre
            periode = f" — période *{mois_filtre}*"
    lien = LIEN_SNCF_RECLAMATION
    if type_reclamation == "billet":
        entete = f"🎫 *Réclamation SNCF — Billet TER à l'unité*{periode}\n\n"
        etapes = (
            "🛤️ *Marche à suivre :*\n"
            "1️⃣ Ouvre le portail SNCF (lien ci-dessous).\n"
            "2️⃣ Connecte-toi avec ton compte client.\n"
            "3️⃣ Choisis *Train retardé* ou *Train supprimé*.\n"
            "4️⃣ Joins le PDF Bazo ci-dessus et copie-colle le texte ci-dessous.\n\n"
            "⚠️ Respecte les délais de réclamation prévus par les CGV TER.\n\n"
        )
        texte_cc = texte_formulaire_billet()
    else:
        entete = f"💳 *Réclamation SNCF — Abonnement TER (Garantie Fiabilité)*{periode}\n\n"
        etapes = (
            "🛤️ *Marche à suivre :*\n"
            "1️⃣ Ouvre le portail SNCF (lien ci-dessous).\n"
            "2️⃣ Connecte-toi sur le site officiel.\n"
            "3️⃣ Clique sur *Carte, pass, abonnement, tarif réduit*.\n"
            "4️⃣ Sélectionne ton abonnement TER Grand Est, joins le PDF Bazo "
            "et copie-colle le texte ci-dessous.\n\n"
            "⚠️ La Garantie Fiabilité s'applique aux incidents sur tes axes et créneaux abonnés "
            "(≥ 120 min de retard cumulé ou ≥ 3 suppressions sur le mois).\n\n"
        )
        texte_cc = texte_formulaire_abo(mois_libelle)
    return (
        entete
        + "📄 *Ton attestation PDF est le document ci-dessus.*\n\n"
        + f"👉 [Ouvrir le portail réclamation SNCF]({lien})\n\n"
        + etapes
        + "📝 *Texte à copier-coller dans le formulaire SNCF :*\n\n"
        + f"```\n{texte_cc}\n```"
    )

def generer_et_envoyer_pdf(id_to_send, nom_client, g_all=None, t_all=None, g_ret=None, t_ret=None, mode_admin=False, mois_filtre=None, type_reclamation="abo", edit_message_id=None, mode_api=False):
    nom_temp = f"Attestation_{id_to_send}_{uuid.uuid4().hex[:6]}.pdf"
    incidents_valides = {}
    rows_tv = []
    path_retenu = None

    try:
        if not mode_admin:
            user_pdf = charger_utilisateur(id_to_send) or {}
            g_all = g_all or user_pdf.get("gare_depart", "Non précisé")
            t_all = t_all or user_pdf.get("heures", "Non précisé")
            g_ret = g_ret or user_pdf.get("gare_retour", "Non précisé")
            t_ret = t_ret or user_pdf.get("heures_retour", "Non précisé")
            incidents_valides, rows_tv = assembler_incidents_pdf(id_to_send, mois_filtre, user_pdf)
            if not incidents_valides:
                if mode_api:
                    return None
                msg_vide = (
                    "📭 *Aucun incident éligible* pour ce mois dans tes créneaux 2h.\n\n"
                    "Le radar n'a détecté aucun retard ≥ 10 min ni suppression sur ton axe "
                    "pendant tes horaires configurés.\n\n"
                    f"_Retards < {SEUIL_RETARD_MIN} min non éligibles._"
                )
                if edit_message_id:
                    try:
                        bot.edit_message_text(
                            msg_vide, id_to_send, edit_message_id,
                            parse_mode="Markdown",
                            reply_markup=ajouter_bouton_retour(types.InlineKeyboardMarkup()),
                        )
                    except Exception:
                        bot.send_message(id_to_send, msg_vide, parse_mode="Markdown")
                else:
                    bot.send_message(id_to_send, msg_vide, parse_mode="Markdown")
                return

        pdf = PDF()
        pdf.add_page()
        pdf.set_font("Arial", size=11)
        couleur_titres = (200, 220, 255)
        
        pdf.cell(0, 8, txt=f"Beneficiaire : {clean_text(nom_client)}", ln=True)
        if mois_filtre and not mode_admin:
            try:
                y, mo = mois_filtre.split("-")
                pdf.cell(0, 8, txt=f"Periode couverte : {clean_text(MOIS_FR[int(mo) - 1])} {y}", ln=True)
            except Exception:
                pdf.cell(0, 8, txt=f"Periode couverte : {clean_text(mois_filtre)}", ln=True)
        if not mode_admin:
            pdf.cell(0, 8, txt=f"Surveillance Aller : {clean_text(g_all)} ({t_all})", ln=True)
            pdf.cell(0, 8, txt=f"Surveillance Retour : {clean_text(g_ret)} ({t_ret})", ln=True)
        else:
            pdf.cell(0, 8, txt="Profil : Administrateur (Extraction Globale)", ln=True)
        pdf.ln(5)
        
        pdf.set_fill_color(*couleur_titres)
        pdf.set_font("Arial", 'B', 8)
            
        # LARGEURS DE COLONNES (Total = 190)
        pdf.cell(18, 10, "Date", 1, 0, 'C', 1)
        pdf.cell(14, 10, "Train", 1, 0, 'C', 1)
        pdf.cell(40, 10, "Trajet", 1, 0, 'C', 1)
        pdf.cell(12, 10, "Heure", 1, 0, 'C', 1)
        pdf.cell(68, 10, "Motif de l'incident", 1, 0, 'C', 1)
        pdf.cell(18, 10, "Source", 1, 0, 'C', 1)
        pdf.cell(20, 10, "Retard", 1, 1, 'C', 1)
        
        pdf.set_font("Arial", size=8)
        
        if mode_admin:
            mois_admin = mois_filtre or datetime.now(tz_paris).strftime("%Y-%m")
            conn = db_connect(timeout=10)
            rows = conn.execute("""
                SELECT date, numero_train, ligne, gare, type, retard_min, motif, heure_theo
                FROM historique
                WHERE date LIKE ?
                  AND COALESCE(ligne,'') NOT LIKE '%econciliation GTFS%'
                  AND NOT (heure_theo = '00h00' AND heure_reel IN ('Annule', 'Annulé'))
                  AND COALESCE(numero_train,'') NOT LIKE 'OCESN%'
                ORDER BY date DESC, heure_theo DESC
                LIMIT 200
            """, (f"{mois_admin}%",)).fetchall()
            conn.close()
            incidents_valides = {}
            for row in rows:
                date_inc = str(row[0]).strip()
                num_train = str(row[1]).strip()
                dest_raw = str(row[2]).replace("\n", " ").strip()
                gare_raw = str(row[3]).replace("\n", " ").strip()
                type_inc = str(row[4]).lower().strip()
                retard = row[5] or 0
                motif_raw = str(row[6]).replace("\n", " ").strip()
                h_theo_raw = str(row[7]).replace("\n", " ").strip()
                is_supprime = "supprim" in type_inc
                cle_unique = f"{date_inc}_{num_train}"
                incidents_valides[cle_unique] = {
                    "date": date_inc,
                    "train": num_train,
                    "trajet": f"{gare_raw} -> {dest_raw}",
                    "heure": h_theo_raw,
                    "motif": motif_raw[:60],
                    "retard_txt": f"+{retard} min" if not is_supprime else "Supprimé",
                    "retard": retard,
                    "type": "supprime" if is_supprime else "retard",
                }
            nb_trajets_total = len(incidents_valides)
        else:
            nb_trajets_total = len(rows_tv)

        if not incidents_valides:
            pdf.cell(
                190, 10,
                txt="Aucun trajet validé éligible pour ce mois (check-in + check-out ou bus de substitution).",
                border=1, ln=True, align="C",
            )
        else:
            total_retard_min = 0
            total_suppr = 0
            
            for inc in sorted(incidents_valides.values(), key=lambda x: (x["date"], x.get("heure", ""))):
                source = "Voyageur" if inc.get("provenance") == PROVENANCE_VOYAGEUR else "SNCF"
                pdf.cell(18, 10, clean_text(inc["date"]), 1, 0, 'C')
                pdf.cell(14, 10, clean_text(inc["train"]), 1, 0, 'C')
                pdf.cell(40, 10, clean_text(inc["trajet"][:26]), 1, 0, 'C')
                pdf.cell(12, 10, clean_text(inc["heure"]), 1, 0, 'C')
                pdf.cell(68, 10, clean_text(inc["motif"][:48]), 1, 0, 'L')
                pdf.cell(18, 10, source, 1, 0, 'C')
                pdf.cell(20, 10, clean_text(inc["retard_txt"]), 1, 1, 'C')
                
                if inc["type"] == "supprime":
                    total_suppr += 1
                else:
                    total_retard_min += inc["retard"]
                    
            pdf.ln(8)
            pdf.set_font("Arial", 'B', 10)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(0, 8, clean_text("BILAN GLOBAL SUR VOS CRÉNEAUX :"), ln=True)
            
            pdf.set_font("Arial", '', 10)
            heures = total_retard_min // 60
            minutes = total_retard_min % 60
            
            if heures > 0:
                txt_temps = f"{heures} heure(s) et {minutes} minute(s)"
            else:
                txt_temps = f"{minutes} minute(s)"
                
            pdf.cell(0, 6, clean_text(f"- Nombre de trajets effectues ce mois : {nb_trajets_total}"), ln=True)
            pdf.cell(0, 6, clean_text(f"- Nombre de trains en retard (>= 10 min) : {sum(1 for i in incidents_valides.values() if i['type'] != 'supprime' and i['retard'] >= 10)}"), ln=True)
            pdf.cell(0, 6, clean_text(f"- Temps total de retard cumulé : {txt_temps}"), ln=True)
            pdf.cell(0, 6, clean_text(f"- Nombre de trains supprimés : {total_suppr}"), ln=True)

        if not mode_admin:
            # Règle R9 : provenance explicite.
            pdf.ln(6)
            pdf.set_font("Arial", 'I', 8)
            pdf.set_text_color(80, 80, 80)
            pdf.multi_cell(0, 4, clean_text(
                "Trajets déclarés par le voyageur. Source SNCF : retards et suppressions issus des données "
                "temps réel publiques SNCF, relevées par Bazo. Source Voyageur : déclaration du voyageur, "
                "non vérifiée par SNCF. Document établi par Bazo, non émis par SNCF."
            ))
            pdf.set_text_color(0, 0, 0)

        pdf.output(nom_temp)
        
        nb_ret = sum(1 for i in incidents_valides.values() if i["type"] != "supprime")
        nb_sup = sum(1 for i in incidents_valides.values() if i["type"] == "supprime")
        lib_mois = libelle_mois(mois_filtre) if mois_filtre else "période sélectionnée"
        if mode_api:
            path_retenu = nom_temp
            return {
                "path": nom_temp,
                "mois": mois_filtre,
                "mois_libelle": lib_mois,
                "nb_incidents": len(incidents_valides),
                "nb_retards": nb_ret,
                "nb_suppressions": nb_sup,
            }
        with open(nom_temp, 'rb') as f:
            bot.send_document(
                id_to_send,
                f,
                caption=(
                    f"📄 *Attestation Bazo — {lib_mois}*\n"
                    f"✅ {len(incidents_valides)} incident(s) : {nb_ret} retard(s), {nb_sup} suppression(s)\n"
                    f"_Document prêt pour la SNCF (service tiers, non affilié)._"
                ),
                parse_mode="Markdown",
            )
        if not mode_admin:
            bot.send_message(
                id_to_send,
                construire_message_reclamation(type_reclamation, nom_client, mois_filtre),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

    except Exception as e:
        if mode_api:
            raise
        bot.send_message(id_to_send, f"⚠️ Erreur technique PDF : {e}")
    finally:
        if os.path.exists(nom_temp) and nom_temp != path_retenu:
            os.remove(nom_temp)

def transmettre_admin(message):
    if est_annulation(message.text):
        nettoyer_etat_utilisateur(str(message.chat.id))
        bot.send_message(message.chat.id, "❌ Message annulé.", reply_markup=menu_principal())
        return
    if message.text in TOUS_BOUTONS_MENU:
        gestion_boutons(message)
        return

    bot.send_message(ADMIN_CHAT_ID, f"💬 *MESSAGE REÇU*\nDe : {message.from_user.first_name}\nID : `{message.chat.id}`\nTexte : {message.text}", parse_mode="Markdown")
    bot.send_message(message.chat.id, "✅ Transmis avec succès.", reply_markup=menu_principal())

if __name__ == "__main__":
    logging.info("[BOT] Prêt — recherche active.")
    demarrer_planificateur()
    bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)