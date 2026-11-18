import json
import logging
import os
import time
import requests
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bazo_config import (
    ADMIN_CHAT_ID, DB_PATH, RADAR_REGIONS, TOKEN_SNCF, TOKEN_TELEGRAM, db_connect, log_quota_sncf, make_session,
)
from bazo_metier import (
    MOTIF_ALERTE_RADAR,
    SEUIL_GF_RETARD_MIN,
    SEUIL_GF_SUPPR,
    SEUIL_RETARD_MIN,
    AXES,
    barre_progression_gf,
    bilan_mois_trajets_valides_db,
    bilan_mois_aligne_pdf,
    calculer_sens,
    eligibilite_gf_message,
    get_index_gare,
    h_dans_tranche,
    h_dans_tranche_alerte,
    incident_pertinent_profil,
    traiter_suppression_radar_utilisateur,
    lier_checkins_retards_radar,
    promouvoir_trajet_alerte_radar,
    train_sur_axes_ter,
    trajets_radar_multi,
    utilisateur_peut_recevoir_alertes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bazo.radar")

# L'intelligence temporelle : Gestion automatique Hiver/Été
tz_paris = ZoneInfo("Europe/Paris")

# ==========================================
# 1. CONFIGURATION
# ==========================================
HTTP = make_session(pool=10)
FREQUENCE_SCAN = int(os.getenv("BAZO_RADAR_SCAN_SEC", "720"))
RADAR_RETRO_MIN = int(os.getenv("BAZO_RADAR_RETRO_MIN", "75"))
FICHIER_MEMOIRE = 'alertes_envoyees.json'  # migration legacy → memoire_radar (SQLite)
URL_GTFS_RT = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
trajets = trajets_radar_multi(RADAR_REGIONS)

# ==========================================
# 2. GESTION BASE DE DONNÉES
# ==========================================
def init_db():
    conn = db_connect(timeout=10)
    # ACTIVATION DU MODE WAL : Anti-crash lecture/écriture simultanée
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inscriptions (
            id_telegram TEXT PRIMARY KEY,
            nom TEXT,
            prenom TEXT,
            gare_depart TEXT,
            heures TEXT,
            gare_retour TEXT,
            heures_retour TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historique (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            numero_train TEXT,
            ligne TEXT,
            gare TEXT,
            type TEXT,
            retard_min INTEGER,
            motif TEXT,
            heure_theo TEXT,
            heure_reel TEXT
        )
    """)
    conn.commit()
    conn.close()
    migrer_schema()

def migrer_schema():
    conn = db_connect(timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(inscriptions)")
    cols = [c[1] for c in cursor.fetchall()]
    if "prenom" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN prenom TEXT")
    if "nom" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN nom TEXT")
    if "age" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN age TEXT DEFAULT 'Non précisé'")
    if "jours_actifs" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN jours_actifs TEXT DEFAULT '0,1,2,3,4'")
    if "alertes_actives" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN alertes_actives INTEGER DEFAULT 1")
    if "vacances_jusquau" not in cols:
        cursor.execute("ALTER TABLE inscriptions ADD COLUMN vacances_jusquau TEXT")
    cursor.execute("PRAGMA table_info(historique)")
    hcols = [c[1] for c in cursor.fetchall()]
    if "retard_depart_min" not in hcols:
        cursor.execute("ALTER TABLE historique ADD COLUMN retard_depart_min INTEGER")
    if "retard_arrivee_min" not in hcols:
        cursor.execute("ALTER TABLE historique ADD COLUMN retard_arrivee_min INTEGER")
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
        )
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
        )
    """)
    cursor.execute("PRAGMA table_info(trajets_valides)")
    tvcols = [c[1] for c in cursor.fetchall()]
    if "heure_arrivee_theo" not in tvcols:
        cursor.execute("ALTER TABLE trajets_valides ADD COLUMN heure_arrivee_theo TEXT")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS horaires_theoriques_jour (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            axe TEXT,
            numero_train TEXT NOT NULL,
            gare_depart TEXT,
            gare_arrivee TEXT,
            destination TEXT,
            heure_theo TEXT,
            heure_arrivee_theo TEXT,
            vu_temps_reel INTEGER DEFAULT 0,
            statut TEXT DEFAULT 'theorique',
            UNIQUE(date, numero_train, gare_depart)
        )
    """)
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
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_user_date ON trajets_valides(id_telegram, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hist_date ON historique(date)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_hist_train_heure "
        "ON historique(date, numero_train, gare, heure_theo)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_surv_date ON surveillance_trains(date, statut)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rap_user_statut ON rapports_incidents(id_telegram, statut)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memoire_radar (
            cle TEXT PRIMARY KEY,
            valeur TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def charger_memoire():
    """Charge la mémoire anti-spam depuis SQLite (migration auto depuis JSON legacy)."""
    try:
        conn = db_connect(timeout=10)
        rows = conn.execute("SELECT cle, valeur FROM memoire_radar").fetchall()
        conn.close()
        if rows:
            mem = {}
            for cle, val in rows:
                try:
                    mem[cle] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    mem[cle] = val
            return mem
    except Exception as e:
        logger.warning("[MEMOIRE] Lecture SQLite échouée (%s) — fallback JSON.", e)
    if os.path.exists(FICHIER_MEMOIRE):
        try:
            with open(FICHIER_MEMOIRE, 'r', encoding='utf-8') as f:
                legacy = json.load(f)
            if legacy:
                sauvegarder_memoire(legacy)
            return legacy
        except Exception as e:
            logger.warning("[MEMOIRE] %s illisible (%s) — réinitialisation.", FICHIER_MEMOIRE, e)
    return {}


def sauvegarder_memoire(memoire):
    """Persiste la mémoire anti-spam en SQLite (atomique, résistant aux redémarrages)."""
    try:
        conn = db_connect(timeout=10)
        cur = conn.cursor()
        for cle, val in (memoire or {}).items():
            cur.execute(
                """
                INSERT INTO memoire_radar (cle, valeur, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur, updated_at = excluded.updated_at
                """,
                (str(cle), json.dumps(val, ensure_ascii=False)),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[MEMOIRE] Écriture SQLite échouée (%s) — fallback JSON.", e)
        tmp = f"{FICHIER_MEMOIRE}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(memoire, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FICHIER_MEMOIRE)

def enregistrer_incident(incident):
    conn = db_connect(timeout=10)
    cursor = conn.cursor()
    
    nouvel_id_train = str(incident['numero_train'])
    nouvelle_date = str(incident['date'])
    nouvelle_gare = str(incident['gare'])
    nouveau_type = str(incident['type'])
    retard_depart = incident.get('retard_depart_min')
    retard_arrivee = incident.get('retard_arrivee_min')
    if retard_arrivee is not None:
        nouveau_retard = int(retard_arrivee)
    else:
        nouveau_retard = int(incident['info'])
    if retard_depart is None and nouveau_type != "supprime":
        retard_depart = int(incident.get('info', 0))
    h_theo = str(incident.get('heure_theo') or '')
    
    cursor.execute("""
        SELECT id, type, retard_min FROM historique 
        WHERE date = ? AND numero_train = ? AND gare = ? AND heure_theo = ?
    """, (nouvelle_date, nouvel_id_train, nouvelle_gare, h_theo))
    row = cursor.fetchone()
    if not row and not h_theo:
        cursor.execute("""
            SELECT id, type, retard_min FROM historique 
            WHERE date = ? AND numero_train = ? AND gare = ?
        """, (nouvelle_date, nouvel_id_train, nouvelle_gare))
        row = cursor.fetchone()
    
    if row:
        db_id, db_type, db_retard = row
        if nouveau_type == "supprime":
            cursor.execute("""
                UPDATE historique SET type = 'supprime', retard_min = 0,
                    retard_depart_min = 0, retard_arrivee_min = 0,
                    motif = ?, heure_reel = 'Annulé' 
                WHERE id = ?
            """, (incident['motif'], db_id))
        elif db_type != "supprime" and nouveau_retard > db_retard:
            cursor.execute("""
                UPDATE historique SET retard_min = ?, retard_depart_min = ?,
                    retard_arrivee_min = ?, heure_reel = ?, motif = ? 
                WHERE id = ?
            """, (nouveau_retard, retard_depart, retard_arrivee, incident['heure_reel'], incident['motif'], db_id))
    else:
        impact_type = nouveau_type
        impact_retard = nouveau_retard
        h_reel_impact = incident['heure_reel']
        motif_connu = incident['motif']
        cursor.execute("""
            INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min,
                retard_depart_min, retard_arrivee_min, motif, heure_theo, heure_reel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (incident['date'], incident['numero_train'], incident['destination'], incident['gare'],
              impact_type, impact_retard, retard_depart, retard_arrivee, motif_connu, incident['heure_theo'], h_reel_impact))
        
    conn.commit()
    conn.close()

def _incident_perime(incident, now_min, delai_min=25):
    """True si le départ réel du retard est antérieur de plus de `delai_min` minutes (même journée)."""
    try:
        hh, mm = str(incident.get('heure_reel') or '').split('h')
        ecart = now_min - (int(hh) * 60 + int(mm))
        return ecart > delai_min
    except Exception:
        return False

def envoyer_message(chat_id, message, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = HTTP.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        payload.pop("parse_mode", None)
        r2 = HTTP.post(url, json=payload, timeout=10)
        if r2.status_code != 200:
            logger.warning("[TELEGRAM] Envoi échoué vers %s : HTTP %s", chat_id, r2.status_code)
            return False
        return True
    except Exception as e:
        logger.warning("[TELEGRAM] Envoi impossible vers %s : %s", chat_id, e)
        return False

def _normaliser_ville(nom):
    nom = str(nom).replace('\n', ' ').replace('\r', '').strip()
    if "Luxemb" in nom:
        nom = "Luxembourg"
    return nom

def deduire_destination_finale(gare_raw, dest_raw, sens_aller):
    """Aligné sur la génération PDF (bot_interactif)."""
    dest_finale = dest_raw.split('(')[0].strip()
    if dest_finale == gare_raw or len(dest_finale) < 3:
        for nom_axe, gares in AXES.items():
            if gare_raw.lower() in [g.lower() for g in gares]:
                dest_finale = gares[-1].title() if sens_aller == 1 else gares[0].title()
                break
    if dest_finale == gare_raw:
        dest_finale = "Fin de ligne"
    return dest_finale

def incident_correspond_profil_pdf(gare_raw, dest_raw, h_theo_raw, g_all, g_ret, t_all, t_ret, pour_alerte=False, type_incident="retard"):
    """Perturbation sur l'axe du profil + créneau 2h (filtre unifié bazo_metier)."""
    sens, _ = incident_pertinent_profil(
        gare_raw, dest_raw, h_theo_raw, g_all, g_ret, t_all, t_ret,
        type_incident=type_incident,
        pour_alerte=pour_alerte,
    )
    return sens is not None

def _gares_correspondent(nom_a, nom_b):
    return get_index_gare(nom_a) == get_index_gare(nom_b) and get_index_gare(nom_a) != -1

def activer_surveillance_train(incident):
    if incident.get("type") != "retard":
        return
    try:
        conn = db_connect(timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO surveillance_trains
                (date, numero_train, gare_depart, gare_arrivee, destination, heure_theo,
                 retard_depart_min, statut, motif)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'surveillance', ?)
            ON CONFLICT(date, numero_train, gare_depart) DO UPDATE SET
                retard_depart_min = CASE
                    WHEN excluded.retard_depart_min > surveillance_trains.retard_depart_min
                    THEN excluded.retard_depart_min ELSE surveillance_trains.retard_depart_min END,
                motif = excluded.motif
        """, (
            incident["date"], incident["numero_train"], incident["gare"],
            incident.get("gare_arrivee_suivi", incident["destination"]),
            incident["destination"], incident["heure_theo"],
            incident.get("retard_depart_min", incident["info"]), incident.get("motif", "Non précisé"),
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass

def synchroniser_checkin_retard(date_jour, numero_train, retard=None, heure_reel=None, supprime=False,
                                heure_theo=None, mode="arrivee"):
    """Lie le radar au check-in par numéro + date (+ heure si retard)."""
    try:
        conn = db_connect(timeout=10)
        cur = conn.cursor()
        params = [date_jour, str(numero_train)]
        sql_extra = ""
        if not supprime and heure_theo:
            sql_extra = " AND heure_theo = ?"
            params.append(str(heure_theo))
        if supprime:
            cur.execute(
                f"""
                UPDATE trajets_valides
                SET statut_validation = 'valide_supprime',
                    motif_validation = COALESCE(NULLIF(motif_validation, ''), 'Train supprimé (annulation SNCF)'),
                    heure_reelle_arrivee = 'Annulé',
                    retard_arrivee_min = 0
                WHERE date = ? AND numero_train = ? AND statut_validation NOT IN ('annule', 'valide_report')
                {sql_extra}
                """,
                tuple(params),
            )
        elif mode == "depart" and retard is not None and int(retard) >= SEUIL_RETARD_MIN:
            cur.execute(
                f"""
                UPDATE trajets_valides
                SET retard_arrivee_min = MAX(COALESCE(retard_arrivee_min, 0), ?),
                    statut_validation = CASE
                        WHEN statut_validation = 'en_attente' THEN 'valide_normal'
                        ELSE statut_validation END,
                    motif_validation = COALESCE(NULLIF(motif_validation, ''), ?)
                WHERE date = ? AND numero_train = ? AND statut_validation != 'annule'
                {sql_extra}
                """,
                (
                    int(retard),
                    f"{MOTIF_ALERTE_RADAR} (retard départ +{int(retard)} min)",
                    *params,
                ),
            )
        elif mode == "arrivee" and retard is not None and int(retard) >= SEUIL_RETARD_MIN:
            cur.execute(
                f"""
                UPDATE trajets_valides
                SET statut_validation = CASE
                        WHEN statut_validation = 'en_attente' THEN 'valide_normal'
                        ELSE statut_validation END,
                    retard_arrivee_min = MAX(COALESCE(retard_arrivee_min, 0), ?),
                    heure_reelle_arrivee = COALESCE(heure_reelle_arrivee, ?),
                    motif_validation = COALESCE(NULLIF(motif_validation, ''), ?)
                WHERE date = ? AND numero_train = ? AND statut_validation != 'annule'
                {sql_extra}
                """,
                (int(retard), heure_reel, MOTIF_ALERTE_RADAR, *params),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[SYNC] synchroniser_checkin_retard échoué train %s : %s", numero_train, e)

def appliquer_retard_arrivee(date_jour, numero_train, gare_arrivee, retard_arrivee, heure_reel_arr, motif="Non précisé", heure_theo=None):
    if retard_arrivee < SEUIL_RETARD_MIN:
        return
    synchroniser_checkin_retard(date_jour, numero_train, retard=retard_arrivee, heure_reel=heure_reel_arr, heure_theo=heure_theo)
    try:
        conn = db_connect(timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, gare_depart, gare_arrivee, destination, heure_theo, motif, retard_depart_min
            FROM surveillance_trains
            WHERE date = ? AND numero_train = ? AND statut = 'surveillance'
        """, (date_jour, numero_train))
        for sid, g_dep, g_arr, dest, h_theo, motif_s, retard_dep in cursor.fetchall():
            if not _gares_correspondent(gare_arrivee, g_arr):
                continue
            cursor.execute("""
                UPDATE surveillance_trains SET retard_arrivee_min = ?, statut = 'finalise'
                WHERE id = ? AND (retard_arrivee_min IS NULL OR retard_arrivee_min < ?)
            """, (retard_arrivee, sid, retard_arrivee))
            cursor.execute("""
                SELECT id, retard_min FROM historique
                WHERE date = ? AND numero_train = ? AND gare = ?
                  AND (heure_theo = ? OR ? = '' OR heure_theo IS NULL OR heure_theo = '')
            """, (date_jour, numero_train, g_dep, h_theo or heure_theo or "", heure_theo or h_theo or ""))
            row = cursor.fetchone()
            if row:
                db_id, ancien = row
                if retard_arrivee > ancien:
                    cursor.execute("""
                        UPDATE historique SET retard_min = ?, retard_arrivee_min = ?,
                            retard_depart_min = COALESCE(retard_depart_min, ?),
                            heure_reel = ?, motif = ?
                        WHERE id = ?
                    """, (retard_arrivee, retard_arrivee, retard_dep, heure_reel_arr, motif_s or motif, db_id))
            else:
                cursor.execute("""
                    INSERT INTO historique (date, numero_train, ligne, gare, type, retard_min,
                        retard_depart_min, retard_arrivee_min, motif, heure_theo, heure_reel)
                    VALUES (?, ?, ?, ?, 'retard', ?, ?, ?, ?, ?, ?)
                """, (date_jour, numero_train, dest, g_dep, retard_arrivee, retard_dep, retard_arrivee,
                      motif_s or motif, h_theo, heure_reel_arr))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[ARRIVEE] appliquer_retard_arrivee train %s : %s", numero_train, e)

# ==========================================
# 3. NOTIFICATION CIBLÉE (FILTRE STRICT)
# ==========================================
def _ligne_voie(incident):
    """Ligne « Voie N » suivie d'un retour à la ligne, ou chaîne vide si la voie est inconnue.
    L'API publique Navitia ne fournit pas la voie : on n'affiche jamais « Non spécifiée »."""
    voie = str(incident.get("voie") or "").strip()
    if not voie or voie.lower() in ("non spécifiée", "non specifiee", "none"):
        return ""
    return f"📐 Voie : *{voie}*\n"


def alerter_checkin_concernes(incident, phrase_maj=""):
    """ALERTE CIBLÉE : prévient les utilisateurs ayant fait un check-in sur CE train précis."""
    alertes_ids = set()
    try:
        date_i = str(incident.get('date'))
        num = str(incident.get('numero_train'))
        if not num or num == 'Inconnu':
            return alertes_ids
        now_paris = datetime.now(tz_paris)
        conn = db_connect(timeout=10)
        cursor = conn.cursor()
        rows = cursor.execute("""
            SELECT t.id_telegram, t.heure_theo, t.gare_depart, t.gare_arrivee, t.destination,
                   i.alertes_actives, i.vacances_jusquau, i.jours_actifs
            FROM trajets_valides t
            LEFT JOIN inscriptions i ON i.id_telegram = t.id_telegram
            WHERE t.date = ? AND t.numero_train = ?
              AND t.statut_validation = 'en_attente'
        """, (date_i, num)).fetchall()
        conn.close()

        ligne_voie = _ligne_voie(incident)
        for chat_id, h_theo_u, g_dep_u, g_arr_u, dest_u, alertes, vac, jours in rows:
            if not chat_id:
                continue
            h_inc = str(incident.get('heure_theo') or '').strip()
            if h_inc and h_theo_u and h_inc != str(h_theo_u).strip():
                if incident.get('type') != "supprime":
                    continue
            u_filtre = {
                "alertes_actives": alertes if alertes is not None else 1,
                "vacances_jusquau": vac, "jours_actifs": jours,
            }
            if not utilisateur_peut_recevoir_alertes(u_filtre, now_paris):
                continue
            dest_aff = dest_u or incident.get('destination', '')
            promouvoir_trajet_alerte_radar(chat_id, date_i, num, h_theo_u, incident)
            if incident.get('type') == "supprime":
                msg = (f"{phrase_maj}❌ *TON train est supprimé*\n\n"
                       f"🚆 Train *{num}* (ton check-in de *{h_theo_u}*) → *{dest_aff}*\n"
                       f"{ligne_voie}"
                       f"📝 {incident.get('motif', 'Non précisé')}\n"
                       f"💡 Prochain conseillé : *{incident.get('prochain_train', 'Non disponible')}*\n\n"
                       f"✅ _Incident enregistré dans ton dossier PDF._ Check-out 20h optionnel.")
            else:
                retard_aff = incident.get('retard_depart_min', incident.get('info', 0))
                msg = (f"{phrase_maj}⚠️ *TON train a du retard*\n\n"
                       f"🚆 Train *{num}* (ton check-in de *{h_theo_u}*) → *{dest_aff}*\n"
                       f"📍 Retard signalé à *{incident.get('gare')}*\n"
                       f"🔴 Départ réel : *{incident.get('heure_reel')}* (+{retard_aff} min)\n"
                       f"{ligne_voie}"
                       f"📝 {incident.get('motif', 'Non précisé')}\n\n"
                       f"✅ _Retard enregistré pour ton PDF SNCF._")
            envoyer_message(chat_id, msg)
            alertes_ids.add(str(chat_id))
    except Exception as e:
        logger.warning("[ALERTE] alerter_checkin_concernes échoué train %s : %s", num, e)
    return alertes_ids

def alerter_abonnes(incident, phrase_maj="", exclure_ids=None):
    exclure_ids = exclure_ids or set()
    try:
        now_paris = datetime.now(tz_paris)
        conn = db_connect(timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_telegram, nom, gare_depart, heures, gare_retour, heures_retour,
                   jours_actifs, alertes_actives, vacances_jusquau
            FROM inscriptions
        """)
        utilisateurs = [{
            "id_telegram_propre": r[0], "nom": r[1], "gare_depart": r[2], "heures": r[3],
            "gare_retour": r[4], "heures_retour": r[5], "jours_actifs": r[6],
            "alertes_actives": r[7], "vacances_jusquau": r[8],
        } for r in cursor.fetchall()]
        conn.close()

        gare_incident = str(incident['gare'])
        dest_train = str(incident['destination'])
        h_theo = str(incident['heure_theo'])

        for user in utilisateurs:
            chat_id = user.get("id_telegram_propre")
            if not chat_id:
                continue

            # Déjà prévenu via l'alerte ciblée check-in : pas de doublon.
            if str(chat_id) in exclure_ids:
                continue

            if not utilisateur_peut_recevoir_alertes(user, now_paris):
                continue

            if not incident_correspond_profil_pdf(
                gare_incident, dest_train, h_theo,
                user.get("gare_depart"), user.get("gare_retour"),
                user.get("heures"), user.get("heures_retour"),
                pour_alerte=True,
                type_incident=str(incident.get("type") or "retard"),
            ):
                continue

            sens, _tr = incident_pertinent_profil(
                gare_incident, dest_train, h_theo,
                user.get("gare_depart"), user.get("gare_retour"),
                user.get("heures"), user.get("heures_retour"),
                type_incident=str(incident.get("type") or "retard"),
                pour_alerte=True,
            )
            gare_usager = user.get("gare_depart") if sens == "aller" else user.get("gare_retour")
            # La voie n'est pas fournie par l'API publique : on n'affiche la ligne que si elle existe.
            ligne_voie = _ligne_voie(incident)
            reply_markup = None

            if incident['type'] == "supprime":
                action, ref = traiter_suppression_radar_utilisateur(chat_id, user, incident)
                if action == "maj":
                    suite = "✅ _Ton train déclaré est enregistré comme supprimé dans ton dossier PDF._"
                elif action == "proposition":
                    suite = "📂 _Si tu étais dans ce train, confirme-le pour l'ajouter à ton attestation._"
                    reply_markup = {
                        "inline_keyboard": [[
                            {"text": "✅ J'étais dans ce train", "callback_data": f"prop_ok_{ref}"},
                            {"text": "❌ Non", "callback_data": f"prop_non_{ref}"},
                        ]]
                    }
                else:
                    suite = ""
                msg = (f"❌ *Train Supprimé*\n\n"
                       f"🚆 Train n° *{incident['numero_train']}* — ton trajet depuis *{gare_usager}*\n"
                       f"🕒 Prévu à : {incident['heure_theo']}\n"
                       f"📍 Signalé à : {incident['gare']}\n"
                       f"{ligne_voie}"
                       f"📝 Motif : {incident['motif']}\n\n"
                       f"{suite}\n"
                       f"💡 Prochain train conseillé : *{incident.get('prochain_train', 'Non disponible')}*")
            else:
                suivi = ""
                if incident.get("retard_depart_min") and not incident.get("retard_arrivee_min"):
                    suivi = "\n🔍 _Suivi actif : le retard à l'arrivée sera enregistré pour ton dossier SNCF._\n"
                msg = (f"{phrase_maj}⚠️ *Retard de train*\n\n"
                       f"📍 Retard à *{incident['gare']}* à destination de *{incident['destination']}*\n"
                       f"🕒 Normalement prévu à : {incident['heure_theo']}\n"
                       f"🔴 Partira à : *{incident['heure_reel']}*\n"
                       f"{ligne_voie}"
                       f"⏳ Retard au départ : *{incident.get('retard_depart_min', incident['info'])} minutes*{suivi}\n"
                       f"🚆 Train n° : {incident['numero_train']}\n"
                       f"📝 Motif : {incident['motif']}")
            envoyer_message(chat_id, msg, reply_markup=reply_markup)
    except Exception as e:
        logger.warning("[ALERTE] Construction/envoi alerte échoué pour %s : %s", chat_id, e)

# ==========================================
# 🛡️ BOÎTE NOIRE (SÉCURITÉ GTFS-RT)
# ==========================================
def enregistrer_boite_noire(force=False):
    """Archive GTFS-RT — par défaut 1×/heure (pas à chaque cycle de 10 min)."""
    try:
        if not force:
            cle_heure = datetime.now(tz_paris).strftime("%Y-%m-%d_%H")
            memoire_gtfs = charger_memoire()
            if memoire_gtfs.get("gtfs_archive_heure") == cle_heure:
                return
        maintenant = datetime.now(tz_paris)
        nom_dossier = "archives_gtfs"
        if not os.path.exists(nom_dossier):
            os.makedirs(nom_dossier)

        limite = time.time() - (7 * 24 * 3600)
        for fichier in os.listdir(nom_dossier):
            if fichier.endswith('.pb'):
                chemin = os.path.join(nom_dossier, fichier)
                if os.path.getmtime(chemin) < limite:
                    os.remove(chemin)
                    logger.info("[GTFS] Archive supprimée : %s", chemin)
            
        nom_fichier = f"{nom_dossier}/sncf_{maintenant.strftime('%Y-%m-%d_%Hh%M')}.pb"
        
        reponse = HTTP.get(URL_GTFS_RT, timeout=15)
        if reponse.status_code == 200:
            with open(nom_fichier, 'wb') as f:
                f.write(reponse.content)
            logger.info("[GTFS] Boîte noire enregistrée : %s", nom_fichier)
            memoire_gtfs = charger_memoire()
            memoire_gtfs["gtfs_archive_heure"] = datetime.now(tz_paris).strftime("%Y-%m-%d_%H")
            sauvegarder_memoire(memoire_gtfs)
    except Exception as e:
        logger.warning("[GTFS] Erreur boîte noire : %s", e)

# ==========================================
# 3 bis. PHASE 3 : TRAINS FANTÔMES & GRÈVE
# ==========================================
def _h_label_navitia(s):
    if not s or len(s) < 15:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S").strftime("%Hh%M")
    except Exception:
        return None

def _section_ferree_scanner(info_affichage):
    """Garde le TER, exclut TGV/OUIGO/grandes lignes/bus.
    Attention : ne pas exclure 'FLUO TER / Intercités' (TER régional)."""
    mode = str(info_affichage.get('commercial_mode', '')).upper()
    reseau = str(info_affichage.get('network', '')).upper()
    phys = str(info_affichage.get('physical_mode', '')).upper()
    nom = str(info_affichage.get('name', '') or info_affichage.get('label', '')).upper()
    blob = " ".join([mode, reseau, phys, nom])
    if any(x in blob for x in ["TGV", "OUIGO", "EUROSTAR", "LYRIA", "ICE"]):
        return False
    if "INTERCIT" in blob and "TER" not in blob and "FLUO" not in blob:
        return False
    if any(x in blob for x in ["AUTOCAR", "BUS", "CAR"]):
        return False
    return True

def construire_snapshot_theorique(aujourd_hui, now_paris):
    """Enregistre une fois par jour les trains THÉORIQUES attendus (base_schedule)."""
    from bazo_quota import RESERVE_CHECKIN, enregistrer_appel, peut_appeler

    conn = db_connect(timeout=10)
    cur = conn.cursor()
    debut_jour = now_paris.strftime("%Y%m%dT000000")
    for trajet in trajets:
        if not peut_appeler("snapshot", reserve=RESERVE_CHECKIN):
            logger.warning("[SNAPSHOT] Quota SNCF — snapshot théorique interrompu")
            break
        url = trajet['url'].replace("data_freshness=realtime", "data_freshness=base_schedule")
        url += f"&datetime={debut_jour}&datetime_represents=departure"
        url = url.replace("count=30", "count=40")
        try:
            r = HTTP.get(url, auth=(TOKEN_SNCF, ''), timeout=20)
            enregistrer_appel("snapshot")
            log_quota_sncf(r, "snapshot")
            if r.status_code != 200:
                continue
            donnees = r.json()
            for journey in donnees.get('journeys', []):
                for section in journey.get('sections', []):
                    if section.get('type') != 'public_transport':
                        continue
                    info = section.get('display_informations', {})
                    if not _section_ferree_scanner(info):
                        continue
                    numero = info.get('headsign', 'Inconnu')
                    dest = (info.get('direction', '') or trajet['nom'].split(' ➔ ')[-1]).split('(')[0].strip()
                    arrets = section.get('stop_date_times', [])
                    if not arrets:
                        continue
                    g_dep = arrets[0].get('stop_point', {}).get('name', 'Inconnu')
                    g_arr = arrets[-1].get('stop_point', {}).get('name', dest)
                    h_dep = _h_label_navitia(arrets[0].get('base_departure_date_time') or arrets[0].get('departure_date_time'))
                    h_arr = _h_label_navitia(arrets[-1].get('base_arrival_date_time') or arrets[-1].get('arrival_date_time'))
                    if not h_dep:
                        continue
                    # Trains déjà passés au moment de la construction : neutralisés (pas fantômes).
                    deja_passe = 0
                    try:
                        hh, mm = int(h_dep.split('h')[0]), int(h_dep.split('h')[1])
                        if (hh * 60 + mm) <= (now_paris.hour * 60 + now_paris.minute):
                            deja_passe = 1
                    except Exception:
                        pass
                    cur.execute("""
                        INSERT OR IGNORE INTO horaires_theoriques_jour
                            (date, axe, numero_train, gare_depart, gare_arrivee, destination,
                             heure_theo, heure_arrivee_theo, vu_temps_reel, statut)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'theorique')
                    """, (aujourd_hui, trajet['nom'], numero, g_dep, g_arr, dest, h_dep, h_arr, deja_passe))
            # Validation après CHAQUE axe : sinon la transaction d'écriture ouverte bloque le compteur de
            # quota (autre connexion) et tous les axes suivants étaient silencieusement ignorés.
            conn.commit()
        except Exception as e:
            logger.warning("[SNAPSHOT] axe %s ignoré : %s", trajet.get('nom'), e)
            try:
                conn.rollback()
            except Exception:
                pass
            continue
    conn.commit()
    conn.close()

def marquer_trains_vus(aujourd_hui, numeros_vus_par_axe):
    """Marque les trains vus, limité à l'axe (gare de départ) pour éviter les faux positifs."""
    if not numeros_vus_par_axe:
        return
    try:
        conn = db_connect(timeout=10)
        cur = conn.cursor()
        for gare_axe, nums in numeros_vus_par_axe.items():
            prefix = str(gare_axe)[:8]
            for n in nums:
                cur.execute(
                    """UPDATE horaires_theoriques_jour SET vu_temps_reel = 1
                       WHERE date = ? AND numero_train = ? AND gare_depart LIKE ?""",
                    (aujourd_hui, str(n), f"%{prefix}%"),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[FANTOMES] marquer_trains_vus échoué : %s", e)

def detecter_trains_fantomes(aujourd_hui, now_paris, memoire):
    """Train théorique jamais vu en temps réel + horaire dépassé > 25 min = Supprimé."""
    fantomes = []
    try:
        conn = db_connect(timeout=10)
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT id, numero_train, gare_depart, gare_arrivee, destination, heure_theo
            FROM horaires_theoriques_jour
            WHERE date = ? AND vu_temps_reel = 0 AND statut = 'theorique'
        """, (aujourd_hui,)).fetchall()
        now_min = now_paris.hour * 60 + now_paris.minute
        for hid, numero, g_dep, g_arr, dest, h_theo in rows:
            try:
                hh, mm = int(h_theo.split('h')[0]), int(h_theo.split('h')[1])
            except Exception:
                continue
            delta = now_min - (hh * 60 + mm)
            # Fenêtre : entre 25 min et 6 h après l'horaire théorique
            if not (25 <= delta <= 360):
                continue
            cur.execute("UPDATE horaires_theoriques_jour SET statut = 'fantome' WHERE id = ?", (hid,))
            fantomes.append({
                "date": aujourd_hui, "numero_train": numero, "sens": "",
                "destination": dest, "gare": g_dep, "gare_arrivee_suivi": g_arr,
                "info": 0, "type": "supprime", "id_unique": f"{numero}_{g_dep}_{h_theo}_fantome",
                "motif": "Train absent des données temps réel (probable suppression)",
                "heure_theo": h_theo, "heure_reel": "Annulé",
                "voie": "Non spécifiée", "prochain_train": "Non disponible",
            })
        conn.commit()
        conn.close()
    except Exception:
        pass
    return fantomes

def texte_indique_greve(texte):
    if not texte:
        return False
    t = str(texte).lower()
    return any(x in t for x in [
        "grève", "greve", "mouvement social",
        "mouvement national", "conflit social", "grève nationale",
    ])

def declencher_alerte_greve(memoire, aujourd_hui, motif):
    """Alerte globale Grève / Mouvement social, une seule fois par jour."""
    cle = f"greve_alerte_{aujourd_hui}"
    if memoire.get(cle):
        return
    motif_clean = (motif or "").strip()
    if len(motif_clean) > 280:
        motif_clean = motif_clean[:277] + "..."
    detail = f"\n\nℹ️ _{motif_clean}_" if motif_clean else ""
    msg = (
        "⚠️ *Mouvement social / Grève*\n\n"
        "Un mouvement social impactant fortement la circulation des trains est en cours "
        "sur ta ligne. De nombreuses suppressions et retards sont à prévoir.{detail}\n\n"
        "💡 Vérifie bien ton train avant de partir. Bazo continue d'enregistrer "
        "tes retards et suppressions pour ta réclamation."
    ).format(detail=detail)
    try:
        conn = db_connect(timeout=10)
        cur = conn.cursor()
        cur.execute("""
            SELECT id_telegram, alertes_actives, vacances_jusquau, jours_actifs FROM inscriptions
        """)
        users = [{
            "id_telegram_propre": r[0], "alertes_actives": r[1],
            "vacances_jusquau": r[2], "jours_actifs": r[3],
        } for r in cur.fetchall()]
        conn.close()
        now_paris = datetime.now(tz_paris)
        admin_ok = envoyer_message(ADMIN_CHAT_ID, f"👑 *ALERTE GRÈVE DIFFUSÉE*\n\n{motif_clean or 'Perturbations majeures détectées.'}")
        envoyes = 0
        for u in users:
            cid = u.get("id_telegram_propre")
            if not cid:
                continue
            if not utilisateur_peut_recevoir_alertes(u, now_paris):
                continue
            if envoyer_message(cid, msg):
                envoyes += 1
        if admin_ok or envoyes > 0:
            memoire[cle] = True
        else:
            logger.warning("[GREVE] Diffusion échouée — nouvel essai au prochain cycle")
    except Exception as e:
        logger.warning("[GREVE] Alerte grève échouée : %s", e)

# ==========================================
# 4. MOTEUR DE SCAN
# ==========================================
if __name__ == "__main__":
    logger.info("DÉMARRAGE RADAR Bazo — production (scan %ss, axes %s)", FREQUENCE_SCAN, len(trajets))

while __name__ == "__main__":
    try:
        now_paris = datetime.now(tz_paris)
        heure_actuelle_int = int(now_paris.strftime("%H"))
        maintenant = now_paris.strftime("%H:%M:%S")
        aujourd_hui = now_paris.strftime("%Y-%m-%d")
        
        nouveaux_incidents = []
        memoire = charger_memoire()
        
        # --- NETTOYAGE INTELLIGENT DE LA MÉMOIRE (Anti-Bug de Minuit) ---
        if memoire.get("date_jour") != aujourd_hui:
            nouvelle_memoire = {"date_jour": aujourd_hui}
            for cle, valeur in memoire.items():
                if isinstance(valeur, (int, str)) and "flash_" not in cle and "date_jour" not in cle:
                    try:
                        heure_str = cle.split('_')[-1] 
                        if int(heure_str) >= 2200: # Conserve les retards de fin de soirée
                            nouvelle_memoire[cle] = valeur
                    except:
                        pass
            memoire = nouvelle_memoire

        # --- PHASE 3 : snapshot théorique du jour (une fois) ---
        if memoire.get("snapshot_theo_date") != aujourd_hui:
            construire_snapshot_theorique(aujourd_hui, now_paris)
            memoire["snapshot_theo_date"] = aujourd_hui

        numeros_vus_par_axe = {}
        trains_supprimes_vus = set()
        nb_suppressions_scan = 0
        motif_greve = None
        trajets_ok = 0
        trajets_ko = 0
        axes_ko = []

        from bazo_quota import RESERVE_CHECKIN, budget_restant, enregistrer_appel, peut_appeler, statut_quota

        def _fetch_trajet_api(traj):
            try:
                if not peut_appeler("radar", reserve=RESERVE_CHECKIN):
                    return traj, 429, None
                # Fenêtre rétroactive (75 min) : un train déjà parti de l'origine de l'axe reste visible
                # (incident Bettembourg 04/11 : le 08h33 n'était plus scanné après son départ de Metz).
                debut_fenetre = (datetime.now(tz_paris) - timedelta(minutes=RADAR_RETRO_MIN)).strftime("%Y%m%dT%H%M%S")
                url_scan = traj['url'].replace("count=30", "count=36") + f"&datetime={debut_fenetre}&datetime_represents=departure"
                r = HTTP.get(url_scan, auth=(TOKEN_SNCF, ''), timeout=15)
                enregistrer_appel("radar")
                log_quota_sncf(r, f"scan:{traj.get('nom', '?')}")
                if r.status_code == 200:
                    return traj, 200, r.json()
                return traj, r.status_code, None
            except Exception as e:
                logger.warning("[SCAN] Fetch %s : %s", traj.get('nom'), e)
                return traj, 0, None

        max_calls = max(0, budget_restant() - RESERVE_CHECKIN)
        if max_calls <= 0:
            q = statut_quota()
            logger.warning(
                "[SCAN] Quota SNCF jour atteint (%s/%s) — cycle ignoré",
                q["utilise"], q["budget_jour"],
            )
            time.sleep(FREQUENCE_SCAN)
            continue

        offset = int(memoire.get("radar_scan_offset", 0) or 0)
        nb_axes = min(len(trajets), max_calls)
        batch = [trajets[(offset + i) % len(trajets)] for i in range(nb_axes)]
        memoire["radar_scan_offset"] = (offset + nb_axes) % len(trajets)

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultats_scan = list(pool.map(_fetch_trajet_api, batch))

        for trajet, http_code, donnees in resultats_scan:
            try:
                if http_code != 200 or not donnees:
                    trajets_ko += 1
                    axes_ko.append(f"{trajet.get('nom')} (HTTP {http_code})")
                    continue
                trajets_ok += 1
                destination_finale_fallback = trajet['nom'].split(' ➔ ')[1]
                gare_axe = trajet['nom'].split(' ➔ ')[0]
                
                motif_global = "Non précisé"
                
                if 'disruptions' in donnees:
                    for dis in donnees['disruptions']:
                        texte_dis = ""
                        if 'messages' in dis and len(dis['messages']) > 0:
                            texte_dis = dis['messages'][0].get('text', '')
                            if texte_dis and "Non précisé" not in texte_dis and motif_global == "Non précisé":
                                motif_global = texte_dis
                        if texte_indique_greve(texte_dis) and not motif_greve:
                            motif_greve = texte_dis

                if 'journeys' in donnees:
                    for journey in donnees['journeys']:
                        # --- PROTECTION ET VOCABULAIRE DES TRAINS SUPPRIMÉS ---
                        etat_trajet = journey.get('status', '').lower()
                        trajet_supprime = etat_trajet in ['cancelled', 'deleted', 'no_service']
                        
                        for section in journey.get('sections', []):
                            if section['type'] == 'public_transport':
                                info_affichage = section.get('display_informations', {})
                                
                                # --- LE BOUCLIER ANTI-TGV (Méthode Blacklist) ---
                                mode_commercial = str(info_affichage.get('commercial_mode', '')).upper()
                                reseau = str(info_affichage.get('network', '')).upper()
                                info_globale = mode_commercial + " " + reseau
                                
                                if not _section_ferree_scanner(info_affichage):
                                    continue
                                
                                numero_train = info_affichage.get('headsign', 'Inconnu')
                                numeros_vus_par_axe.setdefault(gare_axe, set()).add(str(numero_train))
                                
                                # CORRECTION BUG DESTINATION VIDE
                                vraie_destination = info_affichage.get('direction', '')
                                if not vraie_destination:
                                    vraie_destination = destination_finale_fallback
                                vraie_destination = vraie_destination.split('(')[0].strip()
                                
                                arrets = section.get('stop_date_times', [])
                                gare_arrivee_suivi = arrets[-1].get('stop_point', {}).get('name', vraie_destination) if arrets else vraie_destination
                                h_depart_train = None

                                for index, arret in enumerate(arrets):
                                    if index == len(arrets) - 1:
                                        h_theo_arr_str = arret.get('base_arrival_date_time') or arret.get('arrival_date_time')
                                        if h_theo_arr_str:
                                            h_theo_arr = datetime.strptime(h_theo_arr_str, "%Y%m%dT%H%M%S")
                                            h_reel_arr_str = arret.get('arrival_date_time') or h_theo_arr_str
                                            h_reel_arr = datetime.strptime(h_reel_arr_str, "%Y%m%dT%H%M%S")
                                            retard_arr = int((h_reel_arr - h_theo_arr).total_seconds() / 60)
                                            gare_arr_nom = arret.get('stop_point', {}).get('name', 'Inconnu')
                                            appliquer_retard_arrivee(
                                                aujourd_hui, numero_train, gare_arr_nom, retard_arr,
                                                h_reel_arr.strftime("%Hh%M"), motif_global,
                                                heure_theo=h_depart_train,
                                            )
                                        continue

                                    statut = arret.get('departure_status', '').lower()
                                    if trajet_supprime:
                                        statut = 'cancelled'
                                    
                                    # CORRECTION BUG SUPPRESSION (KeyError sécurisé)
                                    h_theo_str = arret.get('base_departure_date_time') or arret.get('departure_date_time')
                                    if not h_theo_str:
                                        continue 
                                        
                                    h_theo = datetime.strptime(h_theo_str, "%Y%m%dT%H%M%S")
                                    h_depart_train = h_theo.strftime("%Hh%M")
                                    gare_nom = arret.get('stop_point', {}).get('name', 'Inconnu')
                                    id_unique = f"{numero_train}_{gare_nom}_{h_theo.strftime('%H%M')}"
                                    
                                    # --- EXTRACTION SÉCURISÉE DE LA VOIE (PLATFORM) ---
                                    extraction_voie = arret.get('platform', '') or arret.get('stop_point', {}).get('platform_code', '')
                                    voie_finale = str(extraction_voie).strip() if extraction_voie else "Non spécifiée"
                                    
                                    if statut in ['cancelled', 'deleted']:
                                        # --- ALGORITHME DE RECHERCHE DU PROCHAIN TRAIN VALIDE ---
                                        heure_prochain = "Non disponible"
                                        trouve_suivant = False
                                        
                                        for nj in donnees['journeys']:
                                            if nj.get('status', '').lower() in ['cancelled', 'deleted', 'no_service']:
                                                continue
                                            for nsec in nj.get('sections', []):
                                                if nsec['type'] == 'public_transport':
                                                    for narret in nsec.get('stop_date_times', []):
                                                        nom_g_secours = narret.get('stop_point', {}).get('name', 'Inconnu')
                                                        if nom_g_secours == gare_nom:
                                                            h_theo_secours_str = narret.get('base_departure_date_time') or narret.get('departure_date_time')
                                                            if h_theo_secours_str:
                                                                h_theo_secours = datetime.strptime(h_theo_secours_str, "%Y%m%dT%H%M%S")
                                                                if h_theo_secours > h_theo:
                                                                    heure_prochain = h_theo_secours.strftime("%Hh%M")
                                                                    trouve_suivant = True
                                                                    break
                                                    if trouve_suivant: break
                                            if trouve_suivant: break
                                        # --------------------------------------------------------

                                        nouveaux_incidents.append({
                                            "date": aujourd_hui, 
                                            "numero_train": numero_train, 
                                            "sens": trajet['nom'], 
                                            "destination": vraie_destination, 
                                            "gare": gare_nom,
                                            "info": 0, 
                                            "type": "supprime", 
                                            "id_unique": id_unique, 
                                            "motif": motif_global,
                                            "heure_theo": h_theo.strftime("%Hh%M"), 
                                            "heure_reel": "Annulé",
                                            "voie": voie_finale,
                                            "prochain_train": heure_prochain
                                        })
                                        if numero_train not in trains_supprimes_vus:
                                            trains_supprimes_vus.add(numero_train)
                                            nb_suppressions_scan += 1
                                    else:
                                        h_reel_str = arret.get('departure_date_time') or h_theo_str
                                        h_reel = datetime.strptime(h_reel_str, "%Y%m%dT%H%M%S")
                                        retard = int((h_reel - h_theo).total_seconds() / 60)
                                        if retard >= 10:
                                            incident_retard = {
                                                "date": aujourd_hui, 
                                                "numero_train": numero_train, 
                                                "sens": trajet['nom'], 
                                                "destination": vraie_destination, 
                                                "gare": gare_nom,
                                                "info": retard, 
                                                "type": "retard", 
                                                "id_unique": id_unique, 
                                                "motif": motif_global,
                                                "heure_theo": h_theo.strftime("%Hh%M"), 
                                                "heure_reel": h_reel.strftime("%Hh%M"),
                                                "voie": voie_finale,
                                                "retard_depart_min": retard,
                                                "gare_arrivee_suivi": gare_arrivee_suivi,
                                            }
                                            activer_surveillance_train(incident_retard)
                                            nouveaux_incidents.append(incident_retard)
            except Exception as e:
                trajets_ko += 1
                logger.warning("[SCAN] Erreur axe %s : %s", trajet.get('nom'), e)

        if trajets_ok == 0 and trajets_ko > 0:
            envoyer_message(
                ADMIN_CHAT_ID,
                f"🚨 *Radar Bazo* : aucun axe SNCF répondu ({trajets_ko}/{len(trajets)} échecs). "
                f"Vérifie l'API ou le quota.",
            )

        # --- PHASE 3 : trains fantômes (disparus du temps réel) ---
        marquer_trains_vus(aujourd_hui, numeros_vus_par_axe)
        fantomes = detecter_trains_fantomes(aujourd_hui, now_paris, memoire)
        if fantomes:
            logger.info("[FANTOMES] %s train(s) non vus en RT — pas d'alerte auto (évite faux positifs)", len(fantomes))

        # --- PHASE 3 : alerte globale Grève / Mouvement social ---
        # Déclenchée par un motif explicite de grève, OU un nombre anormal d'annulations
        # confirmées en temps réel (les trains fantômes inférés ne comptent pas, anti faux positif).
        if (motif_greve and nb_suppressions_scan >= 3) or nb_suppressions_scan >= 15:
            declencher_alerte_greve(memoire, aujourd_hui, motif_greve)

        # --- Flash Trafic ---
        try:
            conn = db_connect(timeout=10)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id_telegram, nom, gare_depart, heures, gare_retour, heures_retour,
                       jours_actifs, alertes_actives, vacances_jusquau
                FROM inscriptions
            """)
            utilisateurs = [{
                "id_telegram_propre": r[0], "nom": r[1], "gare_depart": r[2], "heures": r[3],
                "gare_retour": r[4], "heures_retour": r[5], "jours_actifs": r[6],
                "alertes_actives": r[7], "vacances_jusquau": r[8],
            } for r in cursor.fetchall()]

            for user in utilisateurs:
                chat_id = user.get("id_telegram_propre")
                if not chat_id:
                    continue
                if not utilisateur_peut_recevoir_alertes(user, now_paris):
                    continue
                
                nom = user.get("nom", "Voyageur")
                gare_dep = str(user.get("gare_depart", ""))
                gare_ret = str(user.get("gare_retour", ""))
                
                tranche_aller = user.get('heures', 'Non precise')
                tranche_retour = user.get('heures_retour', 'Non precise')
                
                def doit_flasher(tranche):
                    if '-' in tranche:
                        try:
                            h_debut = int(tranche.split('-')[0].replace('h', '').strip())
                            return heure_actuelle_int == (h_debut - 1)
                        except:
                            pass
                    elif tranche in ["Toute la journee", "Non precise"] and heure_actuelle_int == 4: 
                        return True
                    return False

                if doit_flasher(tranche_aller) and memoire.get(f"flash_aller_{chat_id}") != aujourd_hui:
                    sens_user = calculer_sens(gare_dep, gare_ret)
                    probleme_creneau = False
                    for inc in nouveaux_incidents:
                        if get_index_gare(gare_dep) == get_index_gare(inc['gare']) and h_dans_tranche(inc['heure_theo'], tranche_aller):
                            if calculer_sens(inc['gare'], inc['destination']) == sens_user and sens_user != 0:
                                probleme_creneau = True
                                break
                    if not probleme_creneau:
                        hist_rows = cursor.execute(
                            """SELECT gare, ligne, type, heure_theo FROM historique
                               WHERE date = ? AND gare LIKE ? AND type IN ('retard', 'supprime')""",
                            (aujourd_hui, f"%{gare_dep[:8]}%"),
                        ).fetchall()
                        for g_raw, dest_raw, type_inc, h_theo in hist_rows:
                            if h_dans_tranche(h_theo, tranche_aller) and calculer_sens(g_raw, dest_raw) == sens_user and sens_user != 0:
                                probleme_creneau = True
                                break
                    
                    if probleme_creneau:
                        envoyer_message(chat_id, f"🌅 *Flash Trafic Aller*\n\nBonjour {nom} ! Ton train approche.\n⚠️ *Attention*, des perturbations sont détectées à ta gare ({gare_dep}).")
                    else:
                        envoyer_message(chat_id, f"🌅 *Flash Trafic Aller*\n\nBonjour {nom} ! Ton train approche.\n✅ Tout est au vert pour l'instant depuis {gare_dep} ! Bonne route !")
                    memoire[f"flash_aller_{chat_id}"] = aujourd_hui

                if doit_flasher(tranche_retour) and memoire.get(f"flash_retour_{chat_id}") != aujourd_hui:
                    sens_user = calculer_sens(gare_ret, gare_dep)
                    probleme_creneau = False
                    for inc in nouveaux_incidents:
                        if get_index_gare(gare_ret) == get_index_gare(inc['gare']) and h_dans_tranche(inc['heure_theo'], tranche_retour):
                            if calculer_sens(inc['gare'], inc['destination']) == sens_user and sens_user != 0:
                                probleme_creneau = True
                                break
                    if not probleme_creneau:
                        hist_rows = cursor.execute(
                            """SELECT gare, ligne, type, heure_theo FROM historique
                               WHERE date = ? AND gare LIKE ? AND type IN ('retard', 'supprime')""",
                            (aujourd_hui, f"%{gare_ret[:8]}%"),
                        ).fetchall()
                        for g_raw, dest_raw, type_inc, h_theo in hist_rows:
                            if h_dans_tranche(h_theo, tranche_retour) and calculer_sens(g_raw, dest_raw) == sens_user and sens_user != 0:
                                probleme_creneau = True
                                break
                        
                    if probleme_creneau:
                        envoyer_message(chat_id, f"🌇 *Flash Trafic Retour*\n\nBonsoir {nom} ! C'est bientôt l'heure de rentrer.\n⚠️ *Attention*, des perturbations sont détectées pour ton départ depuis {gare_ret}.")
                    else:
                        envoyer_message(chat_id, f"🌇 *Flash Trafic Retour*\n\nBonsoir {nom} ! C'est bientôt l'heure de rentrer.\n✅ Tout est au vert pour ton retour depuis {gare_ret} !")
                    memoire[f"flash_retour_{chat_id}"] = aujourd_hui
            conn.close()
        except Exception as e:
            logger.warning("[FLASH] Flash trafic échoué : %s", e)

        # --- Notification des retards (dédoublonnage par train+heure pour check-in) ---
        if nouveaux_incidents:
            incidents_dedup = {}
            for inc in nouveaux_incidents:
                cle = (inc['date'], inc['numero_train'], inc.get('heure_theo'))
                if cle not in incidents_dedup:
                    incidents_dedup[cle] = inc
                elif inc['type'] == 'supprime':
                    incidents_dedup[cle] = inc
                elif isinstance(incidents_dedup[cle].get('info'), int) and inc.get('info', 0) > incidents_dedup[cle].get('info', 0):
                    incidents_dedup[cle] = inc
            for incident in incidents_dedup.values():
                if not train_sur_axes_ter(incident.get('numero_train')):
                    continue
                if str(incident.get('numero_train', '')).strip() in ('Inconnu', ''):
                    continue
                cle_broadcast = f"{incident['date']}_{incident['numero_train']}_{incident.get('heure_theo')}"
                id_train = cle_broadcast
                retard_actuel = incident['info']
                type_inc = incident['type']
                
                deja_envoye = memoire.get(id_train)
                a_notifier = False
                phrase_maj = ""

                now_min_cur = now_paris.hour * 60 + now_paris.minute
                h_theo_str_inc = str(incident.get('heure_theo') or '00h00')
                try:
                    hh_i, mm_i = int(h_theo_str_inc.split('h')[0]), int(h_theo_str_inc.split('h')[1])
                    dep_min_inc = hh_i * 60 + mm_i
                except Exception:
                    dep_min_inc = now_min_cur
                est_dans_le_train = now_min_cur >= dep_min_inc

                etat_mem = memoire.get(id_train)
                if isinstance(etat_mem, int):
                    etat_mem = {"retard": etat_mem, "avant": True, "pendant": False, "statut": "retard"}
                elif etat_mem == "supprime":
                    etat_mem = {"retard": 999, "avant": True, "pendant": True, "statut": "supprime"}
                elif not isinstance(etat_mem, dict):
                    etat_mem = None

                if type_inc == 'supprime':
                    if not etat_mem or etat_mem.get("statut") != "supprime":
                        a_notifier = True
                        memoire[id_train] = {"retard": 999, "avant": True, "pendant": True, "statut": "supprime"}
                else:
                    if not etat_mem:
                        a_notifier = True
                        memoire[id_train] = {
                            "retard": retard_actuel,
                            "avant": not est_dans_le_train,
                            "pendant": est_dans_le_train,
                            "statut": "retard",
                        }
                    else:
                        ancien_retard = int(etat_mem.get("retard") or 0)
                        if est_dans_le_train and not etat_mem.get("pendant") and (retard_actuel - ancien_retard) >= 15:
                            a_notifier = True
                            phrase_maj = "🚆 *Point trafic à bord :* "
                            etat_mem["pendant"] = True
                            etat_mem["retard"] = retard_actuel
                            memoire[id_train] = etat_mem
                        elif retard_actuel > ancien_retard:
                            etat_mem["retard"] = retard_actuel
                            memoire[id_train] = etat_mem
                            enregistrer_incident(incident)

                if a_notifier and type_inc == 'retard' and _incident_perime(incident, now_min_cur):
                    a_notifier = False
                    enregistrer_incident(incident)      # consigné pour l'historique, pas de notification tardive

                if a_notifier:
                    if type_inc == 'retard':
                        activer_surveillance_train(incident)
                    enregistrer_incident(incident)
                    h_sync = incident.get('heure_theo')
                    if type_inc == 'supprime':
                        synchroniser_checkin_retard(incident['date'], incident['numero_train'],
                                                    supprime=True)
                    else:
                        synchroniser_checkin_retard(incident['date'], incident['numero_train'],
                                                    retard=incident.get('retard_depart_min', incident.get('info')),
                                                    heure_theo=h_sync, mode="depart")
                    if type_inc == 'supprime':
                        msg_admin = f"👑 *SUPERVISION : TRAIN SUPPRIMÉ*\n\n📍 Gare : *{incident['gare']}* à destination de *{incident['destination']}*\n🕒 Prévu à : {incident['heure_theo']}\n{_ligne_voie(incident)}🚆 Train n° : {incident['numero_train']}"
                    else:
                        msg_admin = f"👑 *SUPERVISION*\n\n📍 Retard à *{incident['gare']}* à destination de *{incident['destination']}*\n🕒 Prévu à : {incident['heure_theo']}\n🔴 Partira à : *{incident['heure_reel']}*\n{_ligne_voie(incident)}⏳ Retard : *{incident['info']} min*"
                    
                    envoyer_message(ADMIN_CHAT_ID, msg_admin)
                    ids_checkin = alerter_checkin_concernes(incident, phrase_maj=phrase_maj)
                    alerter_abonnes(incident, phrase_maj, exclure_ids=ids_checkin)

        lier_checkins_retards_radar(aujourd_hui)

        # ==========================================
        # 👑 LE COMPTABLE AUTOMATIQUE (Bilan du 1er du mois à 18h)
        # ==========================================
        jour_actuel = now_paris.strftime("%d")
        
        if jour_actuel == "01" and heure_actuelle_int == 18 and now_paris.minute < 15:
            premier_jour_ce_mois = now_paris.replace(day=1)
            dernier_jour_mois_prec = premier_jour_ce_mois - timedelta(days=1)
            mois_prec_str = dernier_jour_mois_prec.strftime("%Y-%m")
            
            mois_francais = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
            nom_mois_prec = mois_francais[dernier_jour_mois_prec.month - 1]

            if not memoire.get(f"bilan_mensuel_envoye_{mois_prec_str}"):
                conn = None
                try:
                    conn = db_connect(timeout=10)
                    cursor = conn.cursor()
                    cursor.execute("SELECT id_telegram, nom, prenom FROM inscriptions")
                    utilisateurs_bilan = [{"id": r[0], "nom": r[1], "prenom": r[2]} for r in cursor.fetchall()]

                    for u in utilisateurs_bilan:
                        chat_id_b = u["id"]
                        if not chat_id_b:
                            continue

                        total_retard, total_suppr, _, _ = bilan_mois_aligne_pdf(chat_id_b, mois_prec_str)

                        if total_retard >= SEUIL_GF_RETARD_MIN or total_suppr >= SEUIL_GF_SUPPR:
                            h_ret = total_retard // 60
                            m_ret = total_retard % 60
                            msg_bilan = (
                                f"🎉 *BILAN DE {nom_mois_prec.upper()}*\n\n"
                                f"Salut {u.get('prenom', 'Voyageur')} !\n"
                                f"Le mois est terminé. Bazo a fait les comptes pour toi et j'ai une excellente nouvelle.\n\n"
                                f"Tu as subi *{h_ret}h{m_ret:02d}* de retards cumulés et *{total_suppr}* trains supprimés.\n"
                                f"📈 Progression : {barre_progression_gf(total_retard, total_suppr)}\n"
                                f"✅ *TU AS FRANCHI LE SEUIL D'ÉLIGIBILITÉ POUR LE REMBOURSEMENT !*\n\n"
                                f"👉 *Ce que tu dois faire maintenant :*\n"
                                f"1️⃣ Clique sur *📂 Mon PDF Instantané* pour télécharger tes preuves du mois passé.\n"
                                f"2️⃣ Clique sur *💸 Bazo - Ton Allié Réclamation* pour copier le message type.\n"
                                f"3️⃣ Connecte-toi sur ton espace SNCF et récupère ton argent !\n\n"
                                f"Ne laisse pas cet argent dormir à la SNCF, c'est ton droit ! 💪"
                            )
                            envoyer_message(chat_id_b, msg_bilan)
                            time.sleep(1) 
                            
                except Exception as e:
                    logger.warning("[BILAN] Bilan mensuel échoué : %s", e)
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass

                memoire[f"bilan_mensuel_envoye_{mois_prec_str}"] = True
        
        sauvegarder_memoire(memoire)
        
        # Enregistrement en continu de la boîte noire
        enregistrer_boite_noire()
        
        if axes_ko:
            logger.warning("[SCAN] AXES EN ÉCHEC (%s) : %s", len(axes_ko), " ; ".join(axes_ko))
            cle_ko = f"axes_ko_alerte_{aujourd_hui}"
            if not memoire.get(cle_ko) and not str(axes_ko[0]).endswith("HTTP 429)"):
                memoire[cle_ko] = True
                envoyer_message(
                    ADMIN_CHAT_ID,
                    "⚠️ *Radar Bazo* : axes en échec ce cycle :\n" + "\n".join(f"• {a}" for a in axes_ko[:12])
                    + "\n_Alerte unique par jour._",
                )
        memoire["radar_axes_ko"] = axes_ko
        logger.info("[%s Paris] Scan %s/%s axes OK — repos %ss", maintenant, trajets_ok, len(trajets), FREQUENCE_SCAN)
        time.sleep(FREQUENCE_SCAN)
        
    except Exception as e:
        logger.exception("CRASH GLOBAL DU SCANNER EVITÉ — redémarrage dans 60s : %s", e)
        time.sleep(60)