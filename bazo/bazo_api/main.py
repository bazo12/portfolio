#!/usr/bin/env python3
"""API REST Bazo — profil, statut, incidents, check-in/out, PDF (M1–M2)."""
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bazo_api.pdf_sign import cache_key_pdf, creer_token_pdf, verifier_token_pdf
from bazo_api.security import enforce_rate_limit, require_admin, require_user
from bazo_config import API_BASE_URL, API_DOCS_ENABLED, DB_PATH, PDF_TOKEN_TTL_SEC, RADAR_REGIONS, db_connect
from bazo_metier import (
    mettre_a_jour_inscription,
    BETA_OBJECTIF,
    SEUIL_GF_RETARD_MIN,
    SEUIL_GF_SUPPR,
    barre_progression_gf,
    bilan_mois_aligne_pdf,
    charger_utilisateur,
    eligibilite_gf_message,
    format_duree_retard,
    libelle_mois,
    lister_checkout_pending,
    stats_beta,
    stats_rapport_db,
    texte_invitation_beta,
)

tz_paris = ZoneInfo("Europe/Paris")
_bearer = HTTPBearer(auto_error=False)

app = FastAPI(
    title="Bazo API",
    description="API REST pour apps mobile Bazo (TER, alertes, PDF)",
    version="0.5.0",
    docs_url="/docs" if API_DOCS_ENABLED else None,
    redoc_url="/redoc" if API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if API_DOCS_ENABLED else None,
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    return response


class HealthResponse(BaseModel):
    status: str
    db: str
    date: str
    version: str = "0.5.0"
    backend: str = ""
    regions: int = 0
    radar_axes: int = 0


class ProfilResponse(BaseModel):
    id_telegram: str
    prenom: str | None
    nom: str | None
    gare_depart: str | None
    heures: str | None
    gare_retour: str | None
    heures_retour: str | None
    alertes_actives: bool


class StatutResponse(BaseModel):
    mois: str
    mois_libelle: str
    retard_min: int
    suppressions: int
    nb_incidents: int
    nb_trajets: int
    barre_gf: str
    statut_gf: str
    conseil_gf: str


class IncidentsResponse(BaseModel):
    mois: str
    nb_incidents: int
    incidents: list[dict]


class TrainItem(BaseModel):
    numero_train: str
    heure_theo: str
    heure_arrivee_theo: str | None = None
    destination: str | None = None
    gare_depart: str | None = None
    gare_arrivee: str | None = None
    supprime: bool = False


class TrainsResponse(BaseModel):
    sens: str
    date: str
    tranche: str
    gare_depart: str
    gare_arrivee: str
    nb_trains: int
    trains: list[TrainItem]


class CheckinRequest(BaseModel):
    id_telegram: str
    sens: str = Field(..., pattern="^(aller|retour)$")
    numero_train: str
    heure_theo: str
    heure_arrivee_theo: str | None = None
    destination: str | None = None
    date: str | None = None


class CheckinResponse(BaseModel):
    ok: bool
    date: str
    numero_train: str
    heure_theo: str
    sens: str
    statut: str = "en_attente"


class CheckoutActionRequest(BaseModel):
    id_telegram: str
    action: str = Field(..., pattern="^(ok|suppr|suiv|bus|rien)$")


class CheckoutPendingResponse(BaseModel):
    date: str
    nb_pending: int
    trajets: list[dict]


class CheckoutResultResponse(BaseModel):
    ok: bool
    statut: str | None = None
    retard_min: int | None = None
    suppression: bool | None = None
    detail: str | None = None


class PdfLinkResponse(BaseModel):
    token: str
    url: str
    expires_at: str
    mois: str
    mois_libelle: str
    ttl_sec: int


class PdfMoisItem(BaseModel):
    code: str
    libelle: str


class PdfMoisResponse(BaseModel):
    nb_mois: int
    mois: list[PdfMoisItem]


def _pdf_cache_dir():
    cache_dir = os.path.join(os.path.dirname(DB_PATH), "pdf_cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _purger_pdf_cache_expire():
    """Supprime les PDF en cache plus vieux que 2× TTL."""
    cache_dir = _pdf_cache_dir()
    limite = time.time() - PDF_TOKEN_TTL_SEC * 2
    for nom in os.listdir(cache_dir):
        path = os.path.join(cache_dir, nom)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < limite:
                os.remove(path)
        except OSError:
            pass


def _generer_pdf_fichier(id_telegram: str, mois: str):
    from bot_interactif import generer_et_envoyer_pdf

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    nom_pdf = f"{user.get('prenom', '')} {user.get('nom', '')}".strip() or user.get("nom", "Voyageur")
    try:
        meta = generer_et_envoyer_pdf(
            str(id_telegram),
            nom_pdf,
            user.get("gare_depart"),
            user.get("heures"),
            user.get("gare_retour"),
            user.get("heures_retour"),
            mois_filtre=mois,
            mode_api=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur génération PDF : {e}") from e
    if not meta:
        raise HTTPException(status_code=404, detail="Aucun incident éligible pour ce mois")
    return meta, user


def _file_response_pdf(meta: dict, id_telegram: str, mois: str, background_tasks: BackgroundTasks):
    path = meta["path"]
    filename = f"Bazo_Attestation_{mois}_{id_telegram}.pdf"

    def _cleanup():
        if os.path.exists(path):
            os.remove(path)

    background_tasks.add_task(_cleanup)
    return FileResponse(path, media_type="application/pdf", filename=filename)


@app.get("/health", response_model=HealthResponse)
def health(request: Request):
    enforce_rate_limit(request)
    from bazo_config import DB_BACKEND
    from bazo_geo import REGIONS, compter_axes_radar

    db_ok = os.path.isfile(DB_PATH)
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db="ok" if db_ok else "missing",
        date=datetime.now(tz_paris).isoformat(),
        version="0.5.0",
        backend=DB_BACKEND,
        regions=len(REGIONS),
        radar_axes=compter_axes_radar(list(REGIONS.keys())),
    )


@app.get("/v1/auth/check")
def auth_check(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    from bazo_api.security import resolve_user_id, _token_from_request

    enforce_rate_limit(request)
    token = _token_from_request(credentials, x_bazo_token)
    if not token:
        raise HTTPException(status_code=401, detail="Token requis")
    uid = resolve_user_id(token)
    if not uid:
        raise HTTPException(status_code=403, detail="Token invalide")
    return {"ok": True, "id_telegram": uid}


@app.get("/v1/profil/{id_telegram}", response_model=ProfilResponse)
def get_profil(
    id_telegram: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    return ProfilResponse(
        id_telegram=str(id_telegram),
        prenom=user.get("prenom"),
        nom=user.get("nom"),
        gare_depart=user.get("gare_depart"),
        heures=user.get("heures"),
        gare_retour=user.get("gare_retour"),
        heures_retour=user.get("heures_retour"),
        alertes_actives=bool(user.get("alertes_actives", 1)),
    )


@app.get("/v1/statut/{id_telegram}", response_model=StatutResponse)
def get_statut(
    id_telegram: str,
    request: Request,
    mois: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    mois = mois or datetime.now(tz_paris).strftime("%Y-%m")
    total_retard, total_suppr, nb_incidents, nb_trajets = bilan_mois_aligne_pdf(id_telegram, mois)
    statut, conseil = eligibilite_gf_message(total_retard, total_suppr)
    return StatutResponse(
        mois=mois,
        mois_libelle=libelle_mois(mois),
        retard_min=total_retard,
        suppressions=total_suppr,
        nb_incidents=nb_incidents,
        nb_trajets=nb_trajets,
        barre_gf=barre_progression_gf(total_retard, total_suppr),
        statut_gf=statut,
        conseil_gf=conseil,
    )


@app.get("/v1/incidents/{id_telegram}", response_model=IncidentsResponse)
def get_incidents(
    id_telegram: str,
    request: Request,
    mois: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bot_interactif import assembler_incidents_pdf

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    mois = mois or datetime.now(tz_paris).strftime("%Y-%m")
    incidents, _ = assembler_incidents_pdf(str(id_telegram), mois, user)
    liste = [
        {
            "date": v.get("date"),
            "train": v.get("train"),
            "trajet": v.get("trajet"),
            "heure": v.get("heure"),
            "type": v.get("type"),
            "retard": v.get("retard"),
            "motif": v.get("motif"),
            "sens": v.get("sens"),
            "gare_depart": v.get("gare_depart"),
            "gare_arrivee": v.get("gare_arrivee"),
            "direction_commerciale": v.get("direction_commerciale"),
            "origine": v.get("origine"),
            "provenance": v.get("provenance"),
            "provenance_libelle": v.get("provenance_libelle"),
        }
        for v in sorted(incidents.values(), key=lambda x: (x.get("date", ""), x.get("heure", "")))
    ]
    return IncidentsResponse(mois=mois, nb_incidents=len(liste), incidents=liste)


@app.get("/v1/trains/{id_telegram}", response_model=TrainsResponse)
def get_trains(
    id_telegram: str,
    request: Request,
    sens: str = Query(..., pattern="^(aller|retour)$"),
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bot_interactif import _charger_trains_apres_sens, _tranche_profil_utilisateur, gares_pour_sens

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    g_dep, g_arr = gares_pour_sens(user, sens)
    tranche = _tranche_profil_utilisateur(user, sens)
    trains = _charger_trains_apres_sens(user, sens)
    date_j = datetime.now(tz_paris).strftime("%Y-%m-%d")
    items = [
        TrainItem(
            numero_train=t["numero_train"],
            heure_theo=t["heure_theo"],
            heure_arrivee_theo=t.get("heure_arrivee_theo"),
            destination=t.get("destination"),
            gare_depart=g_dep,
            gare_arrivee=g_arr,
            supprime=bool(t.get("supprime")),
        )
        for t in trains
    ]
    return TrainsResponse(
        sens=sens,
        date=date_j,
        tranche=tranche,
        gare_depart=g_dep,
        gare_arrivee=g_arr,
        nb_trains=len(items),
        trains=items,
    )


def _verifier_date_checkin(date_j: str, heure_theo: str, now: datetime):
    """Un check-in se fait le jour même (ou la veille après 22h pour un train de soirée)."""
    try:
        d = datetime.strptime(date_j, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Date invalide (attendu AAAA-MM-JJ).")
    aujourd_hui = now.date()
    if d == aujourd_hui:
        return
    veille = aujourd_hui.fromordinal(aujourd_hui.toordinal() - 1)
    if d == veille and now.hour < 4:
        try:
            h = int(str(heure_theo).lower().replace("h", ":").split(":")[0])
        except Exception:
            h = 0
        if h >= 22:
            return
    raise HTTPException(status_code=400, detail="Le check-in se déclare le jour du trajet.")


def _train_connu_ce_jour(date_j: str, numero: str) -> bool:
    """Le numéro doit exister dans le snapshot théorique ou le radar du jour.
    Si aucune donnée n'existe encore pour ce jour (radar pas encore passé), on accepte."""
    from bazo_config import db_connect
    try:
        conn = db_connect(timeout=5)
        nb_jour = conn.execute(
            "SELECT COUNT(*) FROM horaires_theoriques_jour WHERE date = ?", (date_j,),
        ).fetchone()[0]
        if not nb_jour:
            conn.close()
            return True
        row = conn.execute(
            """
            SELECT 1 FROM horaires_theoriques_jour WHERE date = ? AND numero_train = ?
            UNION ALL
            SELECT 1 FROM historique WHERE date = ? AND numero_train = ?
            LIMIT 1
            """,
            (date_j, numero, date_j, numero),
        ).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return True


@app.post("/v1/checkin", response_model=CheckinResponse)
def post_checkin(
    body: CheckinRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, body.id_telegram, credentials, x_bazo_token)
    from bot_interactif import _tranche_profil_utilisateur, enregistrer_checkin

    user = charger_utilisateur(body.id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    now = datetime.now(tz_paris)
    date_j = body.date or now.strftime("%Y-%m-%d")
    _verifier_date_checkin(date_j, body.heure_theo, now)
    numero = body.numero_train.strip()
    if not numero or not _train_connu_ce_jour(date_j, numero):
        raise HTTPException(status_code=400, detail="Numéro de train inconnu pour cette date.")
    tranche = _tranche_profil_utilisateur(user, body.sens)
    train = {
        "numero_train": numero,
        "heure_theo": body.heure_theo.strip(),
        "heure_arrivee_theo": body.heure_arrivee_theo,
        # La destination reçue est la direction commerciale du train, pas la gare d'arrivée.
        "direction_commerciale": body.destination,
        "destination": body.destination or user.get("gare_retour" if body.sens == "aller" else "gare_depart"),
    }
    enregistrer_checkin(body.id_telegram, train, tranche, body.sens, user, date_j=date_j)
    return CheckinResponse(
        ok=True,
        date=date_j,
        numero_train=train["numero_train"],
        heure_theo=train["heure_theo"],
        sens=body.sens,
    )


class PropositionDecision(BaseModel):
    action: str = Field(..., pattern="^(accepter|refuser)$")


@app.get("/v1/propositions/{id_telegram}")
def get_propositions(
    id_telegram: str,
    request: Request,
    mois: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    """Trains perturbés vus par le radar dans le créneau de l'usager, sans check-in (à confirmer)."""
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bazo_metier import lister_propositions_radar
    props = lister_propositions_radar(str(id_telegram), mois)
    return {"nb": len(props), "propositions": props}


@app.post("/v1/propositions/{id_telegram}/{id_proposition}")
def post_proposition(
    id_telegram: str,
    id_proposition: int,
    body: PropositionDecision,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bazo_metier import accepter_proposition_radar, refuser_proposition_radar
    if body.action == "accepter":
        tid = accepter_proposition_radar(id_proposition, str(id_telegram))
        if not tid:
            raise HTTPException(status_code=404, detail="Proposition introuvable ou déjà traitée.")
        return {"ok": True, "action": "accepter", "trajet_id": tid}
    if not refuser_proposition_radar(id_proposition, str(id_telegram)):
        raise HTTPException(status_code=404, detail="Proposition introuvable ou déjà traitée.")
    return {"ok": True, "action": "refuser"}


# ---------------------------------------------------------------------------
# Phase 2 — Signalements communautaires (taxonomie fermée, rattachés au check-in)
# ---------------------------------------------------------------------------
class SignalementCreate(BaseModel):
    id_telegram: str
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    numero_train: str = Field(..., min_length=1, max_length=12)
    heure_theo: str = Field(..., min_length=4, max_length=5)
    code_type: str = Field(..., max_length=40)
    valeur: str | None = Field(default=None, max_length=4)


class SignalementVote(BaseModel):
    id_telegram: str
    vote: str = Field(..., pattern="^(confirme|refute)$")


class SignalementTrajet(BaseModel):
    id_telegram: str
    action: str = Field(..., pattern="^(inscrire)$")


_ERREURS_SIGNALEMENT_HTTP = {
    "type_inconnu": (400, "Type de signalement inconnu."),
    "valeur_requise": (400, "Valeur requise pour ce type (voie)."),
    "valeur_invalide": (400, "Valeur invalide (ex. 2, 3B, A)."),
    "checkin_requis": (409, "Un check-in sur ce train est requis."),
    "vote_invalide": (400, "Vote invalide."),
    "introuvable": (404, "Signalement introuvable."),
    "expire": (410, "Signalement expiré."),
    "fenetre_close": (409, "Ce train est passé depuis plus de 3 h : le signalement n'est plus possible."),
    "auteur": (403, "L'auteur ne vote pas sur son propre signalement."),
    "deja_vote": (409, "Un seul vote par compte."),
}


def _erreur_signalement(code: str):
    status, detail = _ERREURS_SIGNALEMENT_HTTP.get(code, (400, "Signalement impossible."))
    raise HTTPException(status_code=status, detail=detail)


@app.get("/v1/signalements/types")
def get_signalements_types(request: Request):
    enforce_rate_limit(request)
    from bazo_metier import MENTION_COMMUNAUTAIRE, types_signalement_publics
    return {"mention": MENTION_COMMUNAUTAIRE, "types": types_signalement_publics()}


@app.post("/v1/signalements", status_code=201)
def post_signalement(
    body: SignalementCreate,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    """Crée un signalement. Sans check-in de l'auteur sur ce train : 409. Aucun effet PDF."""
    require_user(request, body.id_telegram, credentials, x_bazo_token)
    from bazo_metier import creer_signalement
    res = creer_signalement(
        str(body.id_telegram), body.date, body.numero_train.strip(), body.heure_theo.strip(),
        body.code_type, valeur=body.valeur,
    )
    if not res.get("ok"):
        _erreur_signalement(res.get("erreur", ""))
    return {"ok": True, "existant": bool(res.get("existant")), "signalement": res["signalement"]}


@app.get("/v1/trains/{date}/{numero}/signalements")
def get_signalements_train(
    date: str,
    numero: str,
    request: Request,
    heure_theo: str | None = None,
    id_telegram: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    """Fil du train : signalements non expirés, toujours avec la mention communautaire."""
    if id_telegram:
        require_user(request, id_telegram, credentials, x_bazo_token)
    else:
        enforce_rate_limit(request)
    from bazo_metier import MENTION_COMMUNAUTAIRE, expirer_signalements, signalements_du_train
    expirer_signalements()
    sigs = signalements_du_train(date, numero, heure_theo)
    for s in sigs:
        s.pop("id_auteur", None)  # anonymisation côté public
    return {"mention": MENTION_COMMUNAUTAIRE, "nb": len(sigs), "signalements": sigs}


@app.post("/v1/signalements/{signalement_id}/vote")
def post_signalement_vote(
    signalement_id: int,
    body: SignalementVote,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, body.id_telegram, credentials, x_bazo_token)
    from bazo_metier import voter_signalement
    res = voter_signalement(signalement_id, str(body.id_telegram), body.vote)
    if not res.get("ok"):
        _erreur_signalement(res.get("erreur", ""))
    return {"ok": True, "statut": res["statut"]}


@app.post("/v1/signalements/{signalement_id}/trajet")
def post_signalement_trajet(
    signalement_id: int,
    body: SignalementTrajet,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    """« Est-ce ton trajet ? » : seul l'auteur inscrit son propre trajet, provenance « Déclaration du voyageur »."""
    require_user(request, body.id_telegram, credentials, x_bazo_token)
    from bazo_metier import charger_signalement, promouvoir_trajet_signalement
    sig = charger_signalement(signalement_id)
    if not sig or str(sig["id_auteur"]) != str(body.id_telegram):
        raise HTTPException(status_code=404, detail="Signalement introuvable.")
    if not sig.get("effet_pdf_possible"):
        raise HTTPException(status_code=400, detail="Ce type de signalement n'a pas d'effet sur l'attestation.")
    tid = promouvoir_trajet_signalement(
        str(body.id_telegram), sig["code_type"], id_trajet=sig["id_trajet"], signalement_id=signalement_id,
    )
    if not tid:
        raise HTTPException(status_code=409, detail="Trajet non inscriptible.")
    return {"ok": True, "trajet_id": tid}


@app.get("/v1/checkout/pending/{id_telegram}", response_model=CheckoutPendingResponse)
def get_checkout_pending(
    id_telegram: str,
    request: Request,
    date: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    date_j = date or datetime.now(tz_paris).strftime("%Y-%m-%d")
    trajets = lister_checkout_pending(id_telegram, date_j)
    return CheckoutPendingResponse(date=date_j, nb_pending=len(trajets), trajets=trajets)


@app.post("/v1/checkout/{trajet_id}", response_model=CheckoutResultResponse)
def post_checkout(
    trajet_id: int,
    body: CheckoutActionRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, body.id_telegram, credentials, x_bazo_token)
    from bot_interactif import executer_checkout

    result = executer_checkout(trajet_id, body.id_telegram, body.action)
    if not result.get("ok"):
        code = 403 if result.get("error") == "forbidden" else 404
        if result.get("error") == "invalid_action":
            code = 400
        raise HTTPException(status_code=code, detail=result.get("detail") or result.get("error"))
    return CheckoutResultResponse(
        ok=True,
        statut=result.get("statut"),
        retard_min=result.get("retard_min"),
        suppression=result.get("suppression"),
    )


@app.get("/v1/pdf/mois/{id_telegram}", response_model=PdfMoisResponse)
def get_pdf_mois(
    id_telegram: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bot_interactif import lister_mois_pdf_eligibles

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    mois_liste = lister_mois_pdf_eligibles(id_telegram, user)
    items = [PdfMoisItem(code=code, libelle=lib) for code, lib in mois_liste]
    return PdfMoisResponse(nb_mois=len(items), mois=items)


@app.post("/v1/pdf/{id_telegram}/link", response_model=PdfLinkResponse)
def post_pdf_link(
    id_telegram: str,
    request: Request,
    mois: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    """Génère le PDF et retourne une URL signée temporaire (partage mobile)."""
    mois = mois or datetime.now(tz_paris).strftime("%Y-%m")
    _purger_pdf_cache_expire()
    meta, _ = _generer_pdf_fichier(id_telegram, mois)
    path = meta["path"]
    token, exp = creer_token_pdf(str(id_telegram), mois)
    cache_path = os.path.join(_pdf_cache_dir(), f"{cache_key_pdf(token)}.pdf")
    try:
        os.replace(path, cache_path)
    except OSError:
        import shutil
        shutil.copy2(path, cache_path)
        if os.path.exists(path):
            os.remove(path)
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone(tz_paris).isoformat()
    url = f"{API_BASE_URL}/v1/pdf/download?token={token}"
    return PdfLinkResponse(
        token=token,
        url=url,
        expires_at=expires_at,
        mois=mois,
        mois_libelle=libelle_mois(mois),
        ttl_sec=PDF_TOKEN_TTL_SEC,
    )


@app.get("/v1/pdf/download")
def get_pdf_download(token: str, request: Request, background_tasks: BackgroundTasks):
    enforce_rate_limit(request)
    """Télécharge un PDF via jeton signé (sans ré-authentification)."""
    try:
        id_telegram, mois = verifier_token_pdf(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    cache_path = os.path.join(_pdf_cache_dir(), f"{cache_key_pdf(token)}.pdf")
    if os.path.isfile(cache_path):
        filename = f"Bazo_Attestation_{mois}_{id_telegram}.pdf"

        def _cleanup():
            if os.path.exists(cache_path):
                os.remove(cache_path)

        background_tasks.add_task(_cleanup)
        return FileResponse(cache_path, media_type="application/pdf", filename=filename)
    meta, _ = _generer_pdf_fichier(id_telegram, mois)
    return _file_response_pdf(meta, id_telegram, mois, background_tasks)


@app.get("/v1/pdf/{id_telegram}")
def get_pdf(
    id_telegram: str,
    request: Request,
    background_tasks: BackgroundTasks,
    mois: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    """Téléchargement direct (session authentifiée par id Telegram)."""
    mois = mois or datetime.now(tz_paris).strftime("%Y-%m")
    meta, _ = _generer_pdf_fichier(id_telegram, mois)
    return _file_response_pdf(meta, id_telegram, mois, background_tasks)


_PDF_DISPO: dict = {}


def _rafraichir_pdf_dispo(id_telegram: str, user: dict) -> None:
    mois_courant = datetime.now(tz_paris).strftime("%Y-%m")
    res = {"disponible": False, "mois": mois_courant, "mois_libelle": libelle_mois(mois_courant), "ts": time.time()}
    try:
        from bot_interactif import lister_mois_pdf_eligibles

        mois = lister_mois_pdf_eligibles(id_telegram, user)
        if mois:
            codes = [c for c, _ in mois]
            code = mois_courant if mois_courant in codes else codes[0]
            res.update({"disponible": True, "mois": code, "mois_libelle": dict(mois).get(code, code)})
    except Exception:
        return
    _PDF_DISPO[id_telegram] = res


def _snapshot_live(optionnel: bool = False) -> dict | None:
    import bazo_live

    snap = bazo_live.lire_snapshot()
    if not snap:
        if optionnel:
            return None
        raise HTTPException(status_code=503, detail="Données temps réel indisponibles")
    age = max(0, int(time.time()) - int(snap.get("generated_ts", 0)))
    snap["age_s"] = age
    snap["perime"] = age > bazo_live.PEREMPTION_S
    snap["serveur_ts"] = int(time.time())
    return snap


@app.get("/v1/live/sillon")
def get_live_sillon(request: Request):
    """Instantané public du Sillon Lorrain (mini-map) : tracé, état des tronçons, trains actifs."""
    enforce_rate_limit(request)
    return _snapshot_live()


@app.get("/v1/live/{id_telegram}")
def get_live_accueil(
    id_telegram: str,
    request: Request,
    background_tasks: BackgroundTasks,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    """Écran d'accueil : instantané du réseau + prochain train de l'usager (retard exact, alternatives) + PDF."""
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_geo
    import bazo_live

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    snap = _snapshot_live(optionnel=True)
    conn = db_connect(timeout=5)
    try:
        lignes = conn.execute(
            "SELECT gare_depart, gare_arrivee, heure_theo FROM trains_habituels WHERE id_telegram=?",
            (str(id_telegram),),
        ).fetchall()
    except Exception:
        lignes = []
    finally:
        conn.close()
    habituels: dict = {}
    for g_d, g_a, h in lignes:
        u_d, u_a = bazo_geo.uic_pour_gare(g_d, "grand_est"), bazo_geo.uic_pour_gare(g_a, "grand_est")
        if u_d and u_a:
            habituels.setdefault((u_d, u_a), []).append(h)
    vue = bazo_live.vue_usager(snap, user, habituels) if snap else {"prochain_train": None, "raison": "temps_reel_indisponible"}
    mois_courant = datetime.now(tz_paris).strftime("%Y-%m")
    pdf = _PDF_DISPO.get(str(id_telegram))
    if not pdf or time.time() - pdf["ts"] > 600:
        # Le calcul d'éligibilité est lourd (quelques secondes) : fait en arrière-plan, jamais dans la requête.
        background_tasks.add_task(_rafraichir_pdf_dispo, str(id_telegram), user)
    pdf = {k: v for k, v in (pdf or {"disponible": True, "mois": mois_courant,
                                      "mois_libelle": libelle_mois(mois_courant)}).items() if k != "ts"}
    return {
        "reseau": snap,
        "profil": {"prenom": user.get("prenom"), "gare_depart": user.get("gare_depart"),
                   "gare_retour": user.get("gare_retour")},
        **vue,
        "pdf": pdf,
    }


# ---------------------------------------------------------------------------
# Profil modifiable, trains habituels, grève, entraide (app mobile v2)
# ---------------------------------------------------------------------------
class ProfilUpdate(BaseModel):
    prenom: str | None = Field(default=None, max_length=40)
    gare_depart: str | None = Field(default=None, max_length=60)
    gare_retour: str | None = Field(default=None, max_length=60)
    heures: str | None = Field(default=None, pattern=r"^\d{2}h-\d{2}h$")
    heures_retour: str | None = Field(default=None, pattern=r"^\d{2}h-\d{2}h$")
    jours_actifs: str | None = Field(default=None, pattern=r"^[0-6](,[0-6])*$")
    alertes_actives: bool | None = None
    vacances_jusquau: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")


@app.put("/v1/profil/{id_telegram}", response_model=ProfilResponse)
def put_profil(
    id_telegram: str,
    body: ProfilUpdate,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_geo

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    champs = {}
    for cle in ("gare_depart", "gare_retour"):
        v = getattr(body, cle)
        if v is not None:
            if not bazo_geo.uic_pour_gare(v, "grand_est"):
                raise HTTPException(status_code=400, detail=f"Gare inconnue : {v}")
            champs[cle] = bazo_geo.nom_canonique_gare(v)
    if champs.get("gare_depart", user.get("gare_depart")) == champs.get("gare_retour", user.get("gare_retour")):
        raise HTTPException(status_code=400, detail="Les deux gares doivent être différentes.")
    for cle in ("prenom", "heures", "heures_retour", "jours_actifs", "vacances_jusquau"):
        v = getattr(body, cle)
        if v is not None:
            champs[cle] = v.strip() if isinstance(v, str) else v
    if body.alertes_actives is not None:
        champs["alertes_actives"] = 1 if body.alertes_actives else 0
    if champs:
        mettre_a_jour_inscription(str(id_telegram), **champs)
    return get_profil(id_telegram, request, credentials, x_bazo_token)


class HabituelCreate(BaseModel):
    heure: str = Field(..., pattern=r"^\d{2}h\d{2}$")
    sens: str = Field(..., pattern="^(aller|retour)$")
    numero_train: str | None = Field(default=None, max_length=12)


@app.get("/v1/habituels/{id_telegram}")
def get_habituels(
    id_telegram: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_greve

    bazo_greve.init_schema()
    conn = db_connect(timeout=5)
    rows = conn.execute("SELECT gare_depart, gare_arrivee, heure_theo, numero_train, manuel FROM trains_habituels "
                        "WHERE id_telegram=? ORDER BY heure_theo", (str(id_telegram),)).fetchall()
    conn.close()
    return {"nb": len(rows), "habituels": [
        {"gare_depart": r[0], "gare_arrivee": r[1], "heure": r[2], "numero_train": r[3], "manuel": bool(r[4])} for r in rows]}


@app.post("/v1/habituels/{id_telegram}", status_code=201)
def post_habituel(
    id_telegram: str,
    body: HabituelCreate,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_greve
    from bot_interactif import gares_pour_sens

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    g_d, g_a = gares_pour_sens(user, body.sens)
    if not g_d or not g_a:
        raise HTTPException(status_code=400, detail="Profil incomplet (gares).")
    bazo_greve.definir_train_habituel(str(id_telegram), g_d, g_a, body.heure, numero_train=body.numero_train)
    return get_habituels(id_telegram, request, credentials, x_bazo_token)


@app.delete("/v1/habituels/{id_telegram}")
def delete_habituel(
    id_telegram: str,
    request: Request,
    heure: str = Query(..., pattern=r"^\d{2}h\d{2}$"),
    sens: str = Query(..., pattern="^(aller|retour)$"),
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_geo
    import bazo_greve
    from bot_interactif import gares_pour_sens

    bazo_greve.init_schema()
    user = charger_utilisateur(id_telegram) or {}
    g_d, g_a = gares_pour_sens(user, sens)
    conn = db_connect(timeout=5)
    conn.execute("DELETE FROM trains_habituels WHERE id_telegram=? AND heure_theo=? AND gare_depart=? AND gare_arrivee=?",
                 (str(id_telegram), heure, bazo_geo.nom_canonique_gare(g_d or ""), bazo_geo.nom_canonique_gare(g_a or "")))
    conn.commit()
    conn.close()
    return get_habituels(id_telegram, request, credentials, x_bazo_token)


@app.get("/v1/greve/{id_telegram}")
def get_greve(
    id_telegram: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    """Plan de transport adapté (aujourd'hui + demain) sur les segments de l'usager."""
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_greve as G
    import bazo_sources_rt as S
    import bazo_temps_reel as TR

    G.init_schema()
    conn = db_connect(timeout=5)
    try:
        user = TR.charger_inscriptions(conn).get(str(id_telegram))
        if not user:
            raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
        now = datetime.now(tz_paris)
        jours = []
        for delta in (0, 1):
            d = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
            meta = conn.execute("SELECT mode, source, nb_total, nb_supprimes, updated_ts FROM greve_meta WHERE date=?", (d,)).fetchone()
            segs = []
            for seg in G.segments_utilisateur(conn, user, d):
                plan = G._plan_segment(conn, d, seg["uic_dep"], seg["uic_arr"])
                fen = seg["fenetre"]
                trains = [{"numero": p["numero"], "heure": S.epoch_vers_hhmm(p["dep_base"] or p["dep_rt"]) or "", "arrivee": S.epoch_vers_hhmm(p["arr_rt"] or p["arr_base"]) or "",
                           "statut": p["statut"]} for p in plan]
                dans_fenetre = [t for t in trains if t["heure"] and fen[0] <= int(t["heure"][:2]) < fen[1]]
                segs.append({"gare_depart": seg["gare_depart"], "gare_arrivee": seg["gare_arrivee"],
                             "fenetre": f"{fen[0]:02d}h-{fen[1]:02d}h", "trains": dans_fenetre,
                             "nb_supprimes": sum(1 for t in dans_fenetre if t["statut"] == "supprime")})
            jours.append({"date": d, "mode": meta[0] if meta else "veille", "actif": bool(meta and meta[0] in ("auto", "actif", "force")),
                          "source": meta[1] if meta else None, "nb_total": meta[2] if meta else 0,
                          "nb_supprimes": meta[3] if meta else 0, "maj_ts": meta[4] if meta else None, "segments": segs})
        return {"jours": jours}
    finally:
        conn.close()


class BloqueCreate(BaseModel):
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    numero_train: str = Field(..., max_length=12)
    heure_theo: str = Field(..., max_length=5)
    gare: str = Field(..., max_length=60)


class OffreCreate(BaseModel):
    hub_id: int
    destination: str = Field(..., max_length=60)
    places: int = Field(..., ge=1, le=7)
    point_rdv_code: str = Field(..., max_length=20)
    depart: str = Field(..., pattern=r"^\d{2}[h:]\d{2}$")
    participation_eur: float = Field(default=0, ge=0, le=200)
    contact: str | None = Field(default=None, max_length=80)
    accepte_charte: bool = False


class OffreStatut(BaseModel):
    statut: str = Field(..., pattern="^(partie|annulee)$")


class AbusCreate(BaseModel):
    motif: str = Field(default="", max_length=120)


_ERREURS_HUB = {
    "checkin_requis": (409, "Un check-in sur ce train est requis."), "gare_inconnue": (400, "Gare inconnue."),
    "hub_introuvable": (404, "Hub introuvable."), "hub_inactif": (409, "Le hub n'est pas encore actif."),
    "covoiturage_ferme": (409, "L'entraide voiture s'ouvre quand l'attente dépasse 2 h."),
    "majeur_requis": (403, "Réservé aux majeurs."), "offre_deja_active": (409, "Tu as déjà une offre active."),
    "compte_suspendu": (403, "Compte suspendu de l'entraide, en attente de revue."),
    "destination_invalide": (400, "Gare de destination invalide."), "participation_trop_elevee": (400, "Participation au-dessus du plafond."),
    "heure_invalide": (400, "Heure invalide."), "heure_passee": (400, "Heure déjà passée."),
    "charte_requise": (400, "Il faut accepter la charte."), "point_rdv_invalide": (400, "Point de rendez-vous invalide."),
    "places_invalides": (400, "Nombre de places invalide."), "offre_introuvable": (404, "Offre introuvable."),
    "conducteur": (400, "Tu es le conducteur."), "complete": (409, "Plus de place."), "trop_de_reservations": (409, "Deux réservations maximum."),
    "deja_reserve": (409, "Déjà réservé."), "reservation_introuvable": (404, "Réservation introuvable."), "statut_invalide": (400, "Statut invalide."),
}


def _hub_ou_erreur(res: dict):
    if res.get("ok"):
        return res
    status, detail = _ERREURS_HUB.get(res.get("erreur", ""), _ERREURS_SIGNALEMENT_HTTP.get(res.get("erreur", ""), (400, "Action impossible.")))
    if res.get("plafond_eur") is not None:
        detail = f"{detail} Plafond : {res['plafond_eur']:.2f} € par passager."
    raise HTTPException(status_code=status, detail=detail)


@app.get("/v1/hub/{id_telegram}")
def get_hub(
    id_telegram: str,
    request: Request,
    date: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_hub
    return bazo_hub.hubs_pour_usager(str(id_telegram), date)


@app.post("/v1/hub/{id_telegram}/bloque", status_code=201)
def post_hub_bloque(
    id_telegram: str, body: BloqueCreate, request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_hub
    return _hub_ou_erreur(bazo_hub.declarer_bloque(str(id_telegram), body.date, body.numero_train.strip(), body.heure_theo.strip(), body.gare))


@app.post("/v1/hub/{id_telegram}/offres", status_code=201)
def post_hub_offre(
    id_telegram: str, body: OffreCreate, request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_hub
    return _hub_ou_erreur(bazo_hub.publier_offre(str(id_telegram), body.hub_id, body.destination, body.places, body.point_rdv_code,
                                                 body.depart, body.participation_eur, body.contact, body.accepte_charte))


@app.post("/v1/hub/{id_telegram}/offres/{offre_id}/reserver")
def post_hub_reserver(
    id_telegram: str, offre_id: int, request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_hub
    return _hub_ou_erreur(bazo_hub.reserver(str(id_telegram), offre_id))


@app.delete("/v1/hub/{id_telegram}/offres/{offre_id}/reserver")
def delete_hub_reserver(
    id_telegram: str, offre_id: int, request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_hub
    return _hub_ou_erreur(bazo_hub.annuler_reservation(str(id_telegram), offre_id))


@app.post("/v1/hub/{id_telegram}/offres/{offre_id}/statut")
def post_hub_offre_statut(
    id_telegram: str, offre_id: int, body: OffreStatut, request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_hub
    return _hub_ou_erreur(bazo_hub.changer_statut_offre(str(id_telegram), offre_id, body.statut))


@app.post("/v1/hub/{id_telegram}/offres/{offre_id}/abus")
def post_hub_abus(
    id_telegram: str, offre_id: int, body: AbusCreate, request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    import bazo_hub
    return _hub_ou_erreur(bazo_hub.signaler_abus(str(id_telegram), offre_id, body.motif))


@app.get("/v1/beta/invitation")
def get_beta_invitation(request: Request):
    enforce_rate_limit(request)
    return {"texte": texte_invitation_beta(), "lien": "https://t.me/SbazoTrain_bot"}


@app.get("/v1/beta/stats")
def get_beta_stats(request: Request):
    enforce_rate_limit(request)
    from bazo_cache import cache_get, cache_set

    key = "beta:stats"
    cached = cache_get(key)
    if cached:
        return cached
    payload = stats_beta()
    cache_set(key, payload, ttl_sec=60)
    return payload


@app.get("/v1/geo/regions")
def get_geo_regions(request: Request, actives: bool = True):
    enforce_rate_limit(request)
    from bazo_cache import cache_get, cache_set
    from bazo_geo import liste_regions

    key = f"geo:regions:{actives}"
    cached = cache_get(key)
    if cached:
        return cached
    payload = {"regions": liste_regions(actives_seulement=actives)}
    cache_set(key, payload, ttl_sec=300)
    return payload


@app.get("/v1/geo/gares")
def get_geo_gares(request: Request, q: str = "", region: str = "grand_est"):
    enforce_rate_limit(request)
    from bazo_cache import cache_get, cache_set
    from bazo_geo import rechercher_gares, toutes_les_gares, uic_pour_gare

    key = f"geo:gares:{region}:{q or '*'}"
    cached = cache_get(key)
    if cached:
        return cached
    gares = rechercher_gares(q, region) if q else toutes_les_gares(region)
    payload = {
        "region": region,
        "q": q or None,
        "nb": len(gares),
        "gares": [{"nom": g, "uic": uic_pour_gare(g, region)} for g in gares],
    }
    cache_set(key, payload, ttl_sec=600)
    return payload


@app.get("/v1/geo/uic/{nom_gare}")
def get_geo_uic(request: Request, nom_gare: str, region: str = "grand_est"):
    enforce_rate_limit(request)
    from bazo_geo import uic_pour_gare
    uic = uic_pour_gare(nom_gare, region)
    if not uic:
        raise HTTPException(status_code=404, detail=f"UIC introuvable pour {nom_gare}")
    return {"nom": nom_gare, "uic": uic, "region": region}


@app.get("/v1/geo/radar")
def get_geo_radar(request: Request, region: str = "grand_est"):
    enforce_rate_limit(request)
    from bazo_cache import cache_get, cache_set
    from bazo_geo import trajets_radar

    key = f"geo:radar:{region}"
    cached = cache_get(key)
    if cached:
        return cached
    trajets = trajets_radar(region)
    payload = {"region": region, "nb": len(trajets), "trajets": trajets}
    cache_set(key, payload, ttl_sec=300)
    return payload


@app.get("/v1/geo/summary")
def get_geo_summary(request: Request):
    enforce_rate_limit(request)
    from bazo_geo import REGIONS, compter_axes_radar, liste_regions

    return {
        "regions_total": len(REGIONS),
        "regions_actives": sum(1 for r in liste_regions() if r.get("active")),
        "axes_radar_total": compter_axes_radar(list(REGIONS.keys())),
        "regions": liste_regions(actives_seulement=False),
    }


@app.get("/v1/tgv/radar")
def get_tgv_radar(request: Request):
    enforce_rate_limit(request)
    from bazo_tgv import TGV_ACTIVE, trajets_tgv

    trajets = trajets_tgv()
    return {"active": TGV_ACTIVE, "nb": len(trajets), "trajets": trajets}


@app.get("/v1/admin/stats")
def admin_stats(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    require_admin(request, x_admin_key)
    from bazo_cache import cache_status
    from bazo_db import backend_info, pg_ping
    from bazo_geo import compter_axes_radar, liste_regions, valider_radar_region
    from bazo_devices import compter_devices_total
    from bazo_push import push_disponible
    from bazo_quota import statut_quota

    s = stats_rapport_db() or {}
    beta = stats_beta()
    manquants = {r["id"]: valider_radar_region(r["id"]) for r in liste_regions(actives_seulement=False)}
    pg_ok, pg_msg = pg_ping()
    return {
        "inscrits": s.get("inscrits", 0),
        "checkin_jour": s.get("checkin_jour", 0),
        "eligible_gf": s.get("eligible_gf", 0),
        "devices_enregistres": compter_devices_total(),
        "beta": beta,
        "quota_sncf": statut_quota(),
        "seuils": {"retard_min": SEUIL_GF_RETARD_MIN, "suppressions": SEUIL_GF_SUPPR},
        "objectif_beta": BETA_OBJECTIF,
        "infra": {
            "db": backend_info(),
            "postgres_ping": {"ok": pg_ok, "detail": pg_msg},
            "cache": cache_status(),
            "push_fcm": push_disponible(),
            "radar_axes_actifs": compter_axes_radar(RADAR_REGIONS),
            "radar_regions": RADAR_REGIONS,
            "radar_scan_sec": int(os.getenv("BAZO_RADAR_SCAN_SEC", "720")),
            "radar_uic_manquants": {k: v for k, v in manquants.items() if v},
        },
    }


class AdminPushTest(BaseModel):
    id_telegram: str
    titre: str = "Test Bazo"
    message: str = "Notification test"


@app.post("/v1/admin/push-test")
def admin_push_test(
    request: Request,
    body: AdminPushTest,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    require_admin(request, x_admin_key)
    from bazo_push import envoyer_push_utilisateur, push_disponible

    if not push_disponible():
        raise HTTPException(status_code=503, detail="FCM non configuré (BAZO_FCM_*)")
    n = envoyer_push_utilisateur(body.id_telegram, body.titre, body.message)
    return {"ok": n > 0, "envoyes": n}


@app.get("/v1/account/{id_telegram}/export")
def account_export(
    id_telegram: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bazo_rgpd import exporter_donnees_utilisateur

    if not charger_utilisateur(id_telegram):
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    return exporter_donnees_utilisateur(id_telegram)


@app.delete("/v1/account/{id_telegram}")
def account_delete(
    id_telegram: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bazo_rgpd import supprimer_compte_utilisateur

    if not charger_utilisateur(id_telegram):
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    return supprimer_compte_utilisateur(id_telegram)


@app.get("/v1/reclamation/{id_telegram}")
def get_reclamation_kit(
    id_telegram: str,
    request: Request,
    mois: str | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bot_interactif import construire_message_reclamation, inferer_type_reclamation

    user = charger_utilisateur(id_telegram)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    mois = mois or datetime.now(tz_paris).strftime("%Y-%m")
    nom = f"{user.get('prenom', '')} {user.get('nom', '')}".strip() or user.get("nom", "Voyageur")
    type_rec = inferer_type_reclamation(user)
    return {
        "mois": mois,
        "type_reclamation": type_rec,
        "texte": construire_message_reclamation(type_rec, nom, mois),
    }


class DeviceRegister(BaseModel):
    device_token: str = Field(..., min_length=8, max_length=512)
    plateforme: str = Field(default="unknown", max_length=32)


@app.post("/v1/devices/{id_telegram}")
def register_device(
    id_telegram: str,
    body: DeviceRegister,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bazo_devices import enregistrer_device

    if not charger_utilisateur(id_telegram):
        raise HTTPException(status_code=404, detail="Utilisateur non inscrit")
    if not enregistrer_device(id_telegram, body.device_token, body.plateforme):
        raise HTTPException(status_code=400, detail="Enregistrement device échoué")
    return {"ok": True, "plateforme": body.plateforme}


@app.delete("/v1/devices/{id_telegram}")
def unregister_device(
    id_telegram: str,
    request: Request,
    device_token: str = Query(..., min_length=8),
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: str | None = Header(default=None, alias="X-Bazo-Token"),
):
    require_user(request, id_telegram, credentials, x_bazo_token)
    from bazo_devices import supprimer_device

    supprimer_device(id_telegram, device_token)
    return {"ok": True}


@app.post("/v1/webhooks/ping")
def webhook_ping(request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    require_admin(request, x_admin_key)
    return {"ok": True, "service": "bazo-api", "ts": datetime.now(tz_paris).isoformat()}


class DeployWebhook(BaseModel):
    version: str = "unknown"
    message: str = ""


@app.post("/v1/webhooks/deploy")
def webhook_deploy(
    request: Request,
    body: DeployWebhook,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """Notification déploiement CI/CD → admin Telegram."""
    require_admin(request, x_admin_key)
    import requests

    from bazo_config import ADMIN_CHAT_ID, TOKEN_TELEGRAM

    txt = f"🚀 *Bazo deploy* `{body.version}`\n{body.message or 'OK'}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": txt, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception:
        pass
    return {"ok": True, "version": body.version, "ts": datetime.now(tz_paris).isoformat()}


# --- Static : PWA mobile + landing + admin ---
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(_ROOT, "bazo_web")):
    app.mount("/app", StaticFiles(directory=os.path.join(_ROOT, "bazo_web"), html=True), name="webapp")
if os.path.isdir(os.path.join(_ROOT, "landing")):
    app.mount("/site", StaticFiles(directory=os.path.join(_ROOT, "landing"), html=True), name="landing")
if os.path.isdir(os.path.join(_ROOT, "bazo_admin")):
    app.mount("/admin", StaticFiles(directory=os.path.join(_ROOT, "bazo_admin"), html=True), name="admin_ui")
