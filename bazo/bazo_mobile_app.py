#!/usr/bin/env python3
"""
Bazo Backend API — Production
API REST pure pour les applications mobiles iOS et Android (React Native / Expo).
Conformité légale : Signalements à choix fermés uniquement (Art. L2241-10 Code des transports).
"""
import hashlib
import hmac
import os
import secrets
import tempfile
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from fpdf import FPDF

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bazo_config import ADMIN_CHAT_ID, db_connect
from bazo_metier import (
    SEUIL_GF_RETARD_MIN,
    SEUIL_GF_SUPPR,
    TOUTES_LES_GARES,
    charger_utilisateur,
    creer_inscription_si_absent,
    eligibilite_gf_message,
    format_duree_retard,
    libelle_mois,
    mettre_a_jour_inscription,
    motif_pdf_officiel,
    nom_canonique_gare,
    MENTION_COMMUNAUTAIRE,
    TYPES_SIGNALEMENT,
    creer_signalement,
    expirer_signalements,
    migrer_schema_phase2,
    types_signalement_publics,
)
from bot_interactif import (
    LIEN_SNCF_RECLAMATION,
    TRANCHES_PAR_MODELE,
    assembler_incidents_pdf,
    clean_text,
    enregistrer_checkin,
    executer_checkout,
    gares_pour_sens,
    lister_mois_pdf_eligibles,
    recuperer_trains_theoriques,
    sanitiser_chaine,
    texte_formulaire_abo,
    texte_formulaire_billet,
)

tz_paris = ZoneInfo("Europe/Paris")
app = FastAPI(title="Bazo API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# Catégories fermées strictes : aucun texte libre n'est accepté (Conformité L2241-10).
# Phase 2 : la taxonomie de référence est TYPES_SIGNALEMENT (bazo_metier). Les anciens
# codes du client mobile sont acceptés et traduits, en attendant le retrait de ce service (phase 4).
ALIAS_TYPES_MOBILE = {
    "retard_depart": "retard_au_depart",
    "arret_voie": "arret_prolonge",
    "train_supprime": "train_supprime_annonce",
    "forte_affluence": "forte_affluence",
    "changement_quai": "voie_annoncee",
    "bus_remplacement": "bus_substitution",
}
TYPES_INCIDENTS_AUTORISES = {code: info["libelle"] for code, info in TYPES_SIGNALEMENT.items()}

def init_tables():
    """Les tables communautaires sont créées par bazo_metier.migrer_schema_phase2 (table unique `signalements`)."""
    migrer_schema_phase2()

init_tables()

# ============================================================================
# SÉCURITÉ DES MOTS DE PASSE (PBKDF2-HMAC-SHA256 AVEC SEL UNIQUE)
# ============================================================================
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2${salt}${dk.hex()}"

def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    if stored_hash.startswith("pbkdf2$"):
        try:
            _, salt, hex_dk = stored_hash.split("$", 2)
            new_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
            return hmac.compare_digest(new_dk.hex(), hex_dk)
        except Exception:
            return False
    # Rétrocompatibilité anciens comptes
    legacy = hashlib.sha256(f"bazo_salt_2026_{password}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored_hash)

def get_current_uid(
    authorization: str | None = Header(default=None),
    token_q: str | None = Query(default=None, alias="token"),
) -> str:
    tok = None
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization.split(" ", 1)[1].strip()
    elif token_q:
        tok = token_q.strip()
    if not tok:
        raise HTTPException(status_code=401, detail="Authentification requise.")
    conn = db_connect(timeout=5)
    row = conn.execute("SELECT id_telegram FROM inscriptions WHERE api_token = ?", (tok,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée.")
    return str(row[0])

# ============================================================================
# AUTHENTIFICATION
# ============================================================================
class RegisterReq(BaseModel):
    identifiant: str
    password: str
    prenom: str
    nom: str = ""
    gare_depart: str = "Metz Ville"
    heures: str = "06h-08h"
    gare_retour: str = "Luxembourg"
    heures_retour: str = "16h-18h"

class LoginReq(BaseModel):
    identifiant: str
    secret: str

@app.post("/api/v1/auth/register")
def register(payload: RegisterReq):
    ident = payload.identifiant.strip().lower()
    if len(ident) < 4 or len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Identifiant valide et mot de passe de 6 caractères minimum requis.")
    is_email = "@" in ident
    conn = db_connect(timeout=10)
    cur = conn.cursor()
    if cur.execute("SELECT id_telegram FROM inscriptions WHERE LOWER(email) = ? OR telephone = ?", (ident, ident)).fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Un compte existe déjà avec cet identifiant.")

    uid = f"usr_{uuid.uuid4().hex[:12]}"
    api_tok = secrets.token_urlsafe(32)
    pwd_h = hash_password(payload.password)

    creer_inscription_si_absent(uid, nom=payload.nom.strip() or "Abonné", prenom=payload.prenom.strip())
    cur.execute(
        """UPDATE inscriptions
           SET prenom = ?, nom = ?, gare_depart = ?, heures = ?, gare_retour = ?, heures_retour = ?,
               email = ?, telephone = ?, password_hash = ?, api_token = ?, auth_provider = 'mobile'
           WHERE id_telegram = ?""",
        (payload.prenom.strip(), payload.nom.strip(), payload.gare_depart, payload.heures,
         payload.gare_retour, payload.heures_retour, ident if is_email else None,
         None if is_email else ident, pwd_h, api_tok, uid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "uid": uid, "token": api_tok, "prenom": payload.prenom.strip()}

@app.post("/api/v1/auth/login")
def login(payload: LoginReq):
    ident = payload.identifiant.strip().lower()
    sec = payload.secret.strip()
    conn = db_connect(timeout=10)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id_telegram, prenom, api_token, password_hash FROM inscriptions WHERE LOWER(email) = ? OR telephone = ? OR id_telegram = ?",
        (ident, ident, ident),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=401, detail="Identifiants incorrects.")

    uid, prenom, db_tok, db_pwd = str(row[0]), row[1] or "Abonné", row[2], row[3]
    auth_ok = verify_password(sec, db_pwd) or (db_tok and hmac.compare_digest(db_tok, sec))
    if not auth_ok:
        conn.close()
        raise HTTPException(status_code=401, detail="Identifiants incorrects.")

    if not db_tok:
        db_tok = secrets.token_urlsafe(32)
        cur.execute("UPDATE inscriptions SET api_token = ? WHERE id_telegram = ?", (db_tok, uid))
        conn.commit()
    conn.close()
    return {"ok": True, "uid": uid, "token": db_tok, "prenom": prenom}

# ============================================================================
# SIGNALEMENTS TRAFIC (CHOIX FERMÉS UNIQUEMENT - CONFORME L2241-10)
# ============================================================================
class SignalementReq(BaseModel):
    numero_train: str
    sens: str = "aller"
    code_type: str
    valeur: str | None = None
    heure_theo: str | None = None

@app.post("/api/v1/trafic/signaler")
def signaler_incident(payload: SignalementReq, uid: str = Depends(get_current_uid)):
    """Écrit dans la table unique `signalements` (phase 2). Exige un check-in du jour sur ce train."""
    code = ALIAS_TYPES_MOBILE.get(payload.code_type, payload.code_type)
    if code not in TYPES_SIGNALEMENT:
        raise HTTPException(status_code=400, detail="Type de signalement non autorisé.")
    date_j = datetime.now(tz_paris).strftime("%Y-%m-%d")
    res = creer_signalement(
        uid, date_j, sanitiser_chaine(payload.numero_train)[:12], payload.heure_theo, code, valeur=payload.valeur,
    )
    if not res.get("ok"):
        err = res.get("erreur")
        if err == "checkin_requis":
            raise HTTPException(status_code=409, detail="Déclare d'abord ton trajet sur ce train.")
        if err in ("valeur_requise", "valeur_invalide"):
            raise HTTPException(status_code=400, detail="Voie requise ou invalide (ex. 2, 3B, A).")
        raise HTTPException(status_code=400, detail="Signalement impossible.")
    sig = res["signalement"]
    return {"ok": True, "libelle": sig["libelle"], "id": sig["id"], "statut": sig["statut"], "mention": MENTION_COMMUNAUTAIRE}

# ============================================================================
# GÉNÉRATION DU RELEVÉ DE PONCTUALITÉ PDF PROPRE
# ============================================================================
def nettoyer_motif_officiel(motif_brut: str, type_inc: str, retard_min: int) -> str:
    """Alias rétrocompatible : le nettoyage est fait une seule fois dans bazo_metier."""
    return motif_pdf_officiel(motif_brut, type_inc, retard_min)

def obtenir_incidents_propres(uid: str, mois: str, user: dict):
    """
    Les libellés (trajet = segment usager, motif usager) sont produits par
    `assembler_incidents_pdf` : aucun client ne corrige après coup (phase 1, règle R1/R6).
    """
    return assembler_incidents_pdf(uid, mois, user)

@app.get("/api/v1/dashboard")
def get_dashboard(uid: str = Depends(get_current_uid)):
    user = charger_utilisateur(uid) or {}
    mois_actuel = datetime.now(tz_paris).strftime("%Y-%m")
    aujourdhui = datetime.now(tz_paris).strftime("%Y-%m-%d")
    incidents_dict, _ = obtenir_incidents_propres(uid, mois_actuel, user)

    total_retard = sum(int(i.get("retard") or 0) for i in incidents_dict.values() if i.get("type") != "supprime")
    total_suppr = sum(1 for i in incidents_dict.values() if i.get("type") == "supprime")
    statut_gf, conseil_gf = eligibilite_gf_message(total_retard, total_suppr)

    expirer_signalements()
    from bazo_geo import nom_pour_uic
    from bazo_metier import libelle_type_signalement
    conn = db_connect(timeout=10)
    trafic_rows = conn.execute(
        """SELECT id, heure_theo, numero_train, gare_uic, code_type, valeur, nb_confirmations, statut
           FROM signalements WHERE date = ? AND statut IN ('non_confirme', 'confirme')
           ORDER BY id DESC LIMIT 25""",
        (aujourdhui,),
    ).fetchall()
    conn.close()

    return {
        "profil": {
            "prenom": user.get("prenom") or "Abonné",
            "nom": user.get("nom") or "",
            "gare_depart": nom_canonique_gare(user.get("gare_depart") or "Metz"),
            "heures": user.get("heures") or "06h-08h",
            "gare_retour": user.get("gare_retour") or "Luxembourg",
            "heures_retour": user.get("heures_retour") or "16h-18h",
        },
        "bilan_mois": {
            "mois_code": mois_actuel,
            "mois_libelle": libelle_mois(mois_actuel),
            "retard_cumule_min": total_retard,
            "suppressions_cumulees": total_suppr,
            "seuil_retard_min": SEUIL_GF_RETARD_MIN,
            "seuil_suppressions": SEUIL_GF_SUPPR,
            "eligible_garantie": total_retard >= SEUIL_GF_RETARD_MIN or total_suppr >= SEUIL_GF_SUPPR,
            "resume": statut_gf,
            "detail": conseil_gf,
        },
        "signalements_jour": [
            {
                "id": r[0], "heure": r[1], "train": r[2], "depart": nom_pour_uic(r[3]) or r[3], "destination": "",
                "libelle": libelle_type_signalement(r[4], r[5]), "confirmations": r[6], "statut": r[7],
                "mention": MENTION_COMMUNAUTAIRE,
            }
            for r in trafic_rows
        ],
        "mention_communautaire": MENTION_COMMUNAUTAIRE,
        "incidents_retenus": sorted(list(incidents_dict.values()), key=lambda x: (x["date"], x["heure"]), reverse=True),
        "types_signalements": TYPES_INCIDENTS_AUTORISES,
        "types_signalements_detail": types_signalement_publics(),
    }

class ReleveMensuelPDF(FPDF):
    def header(self):
        self.set_font("Arial", "B", 13)
        self.set_text_color(15, 23, 42)
        self.cell(0, 10, clean_text("RELEVE MENSUEL DE PONCTUALITE FERROVIAIRE"), 0, 1, "C")
        self.ln(3)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "I", 8)
        self.set_text_color(100, 116, 139)
        self.cell(0, 10, clean_text(f"Releve etabli le {datetime.now(tz_paris).strftime('%d/%m/%Y')}"), 0, 0, "C")

@app.get("/api/v1/releve-pdf")
def telecharger_pdf(background_tasks: BackgroundTasks, mois: str = Query(default=""), uid: str = Depends(get_current_uid)):
    user = charger_utilisateur(uid) or {}
    mois_cible = mois or datetime.now(tz_paris).strftime("%Y-%m")
    incidents, _ = obtenir_incidents_propres(uid, mois_cible, user)
    if not incidents:
        raise HTTPException(status_code=404, detail="Aucun incident enregistré sur cette période.")

    nom_complet = f"{user.get('prenom', '')} {user.get('nom', '')}".strip() or "Titulaire Abonnement"
    pdf = ReleveMensuelPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=10)
    pdf.cell(0, 7, txt=clean_text(f"Voyageur : {nom_complet}"), ln=True)
    pdf.cell(0, 7, txt=clean_text(
        f"Trajet habituel : {nom_canonique_gare(user.get('gare_depart') or 'Metz')} - "
        f"{nom_canonique_gare(user.get('gare_retour') or 'Luxembourg')}"
    ), ln=True)
    pdf.cell(0, 7, txt=clean_text(f"Periode : {libelle_mois(mois_cible)}"), ln=True)
    pdf.ln(4)

    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Arial", "B", 8)
    pdf.cell(22, 9, "Date", 1, 0, "C", 1)
    pdf.cell(18, 9, "N. Train", 1, 0, "C", 1)
    pdf.cell(52, 9, "liaison", 1, 0, "C", 1)
    pdf.cell(16, 9, "Horaire", 1, 0, "C", 1)
    pdf.cell(62, 9, "Constat", 1, 0, "C", 1)
    pdf.cell(20, 9, "Ecart", 1, 1, "C", 1)

    pdf.set_font("Arial", size=8)
    for inc in sorted(incidents.values(), key=lambda x: (x["date"], x.get("heure", ""))):
        pdf.cell(22, 8, clean_text(inc["date"]), 1, 0, "C")
        pdf.cell(18, 8, clean_text(inc["train"]), 1, 0, "C")
        pdf.cell(52, 8, clean_text(inc["trajet"][:32]), 1, 0, "C")
        pdf.cell(16, 8, clean_text(inc["heure"]), 1, 0, "C")
        pdf.cell(62, 8, clean_text(inc["motif"][:45]), 1, 0, "L")
        pdf.cell(20, 8, clean_text(inc["retard_txt"]), 1, 1, "C")

    nom_fichier = os.path.join(tempfile.gettempdir(), f"Releve_Ponctualite_{uid}_{uuid.uuid4().hex[:6]}.pdf")
    pdf.output(nom_fichier)
    background_tasks.add_task(lambda p: os.path.exists(p) and os.remove(p), nom_fichier)
    return FileResponse(nom_fichier, media_type="application/pdf", filename=f"Releve_Ponctualite_{mois_cible}.pdf")