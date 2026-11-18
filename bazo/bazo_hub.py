"""Hub de perturbation majeure et entraide (covoiturage de dépannage) — PROJET_SUITE §5.

Un hub = (date, numéro de train, gare UIC). Il devient `actif` quand le train est supprimé / limité à
cette gare, qu'il accuse ≥ HUB_SEUIL_RETARD_MIN de retard, ou que ≥ HUB_QUORUM_BLOQUES comptes distincts
ayant un check-in déclarent « je suis bloqué » avec une corroboration temps réel minimale.
La partie covoiturage s'ouvre seulement si le prochain train utile est à ≥ HUB_SEUIL_ATTENTE_COVOIT_MIN.

Garde-fous : partage de frais plafonné (barème kilométrique), point de rendez-vous fixe en gare (liste
fermée), une offre active par conducteur, deux réservations actives par passager, aucune géolocalisation,
contact partagé uniquement par le conducteur et uniquement aux passagers confirmés.
Les informations des usagers ne sont jamais présentées comme des annonces SNCF.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import bazo_geo
import bazo_notify
from bazo_config import db_connect

tz_paris = ZoneInfo("Europe/Paris")

HUB_SEUIL_RETARD_MIN = 60
HUB_SEUIL_ATTENTE_COVOIT_MIN = 120
HUB_QUORUM_BLOQUES = 3
HUB_CORROBORATION_MIN = 15
BAREME_EUR_KM = 0.25            # plafond de partage de frais : distance route estimée × barème / occupants
FACTEUR_ROUTE = 1.3
MAX_RESERVATIONS_PASSAGER = 2
MAX_PLACES = 7
DUREE_HUB_S = 6 * 3600

POINTS_RDV = {
    "parvis": "Parvis principal de la gare",
    "depose": "Dépose-minute",
    "parking": "Parking de la gare",
}
MENTION = "Entraide entre voyageurs — partage de frais uniquement, jamais une annonce SNCF."
CHARTE = (
    "Covoiturage de dépannage au sens de l'article L. 3132-1 du code des transports : le conducteur fait "
    "le trajet pour son propre compte et ne perçoit qu'un partage de frais plafonné. Rendez-vous en gare "
    "uniquement, dépose en gare uniquement. Réservé aux majeurs. Bazo ne perçoit aucun paiement."
)

_SCHEMA_OK = False


def init_schema() -> None:
    global _SCHEMA_OK
    if _SCHEMA_OK:
        return
    conn = db_connect(timeout=10)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hubs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, numero_train TEXT NOT NULL, gare_uic TEXT NOT NULL, heure_theo TEXT,
            statut TEXT NOT NULL DEFAULT 'candidat',          -- candidat | actif | ferme
            motif_activation TEXT, covoiturage_ouvert INTEGER NOT NULL DEFAULT 0, attente_min INTEGER,
            created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL, ferme_ts INTEGER,
            UNIQUE (date, numero_train, gare_uic)
        );
        CREATE TABLE IF NOT EXISTS hub_participants (
            hub_id INTEGER NOT NULL, id_utilisateur TEXT NOT NULL, role TEXT NOT NULL,   -- bloque | conducteur | passager
            ts INTEGER NOT NULL, PRIMARY KEY (hub_id, id_utilisateur, role)
        );
        CREATE TABLE IF NOT EXISTS hub_offres (
            id INTEGER PRIMARY KEY AUTOINCREMENT, hub_id INTEGER NOT NULL, id_conducteur TEXT NOT NULL,
            destination_uic TEXT NOT NULL, places INTEGER NOT NULL, places_restantes INTEGER NOT NULL,
            point_rdv_code TEXT NOT NULL, depart_ts INTEGER NOT NULL, participation_eur REAL NOT NULL DEFAULT 0,
            contact TEXT, statut TEXT NOT NULL DEFAULT 'ouverte',   -- ouverte | complete | partie | annulee | expiree | suspendue
            created_ts INTEGER NOT NULL, expire_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_offres_hub ON hub_offres(hub_id, statut);
        CREATE TABLE IF NOT EXISTS hub_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offre_id INTEGER NOT NULL, id_passager TEXT NOT NULL,
            statut TEXT NOT NULL DEFAULT 'confirmee',               -- confirmee | annulee
            ts INTEGER NOT NULL, UNIQUE (offre_id, id_passager)
        );
        CREATE TABLE IF NOT EXISTS hub_signalements_abus (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offre_id INTEGER NOT NULL, id_auteur TEXT NOT NULL,
            motif TEXT, ts INTEGER NOT NULL, UNIQUE (offre_id, id_auteur)
        );
        """
    )
    conn.commit()
    conn.close()
    _SCHEMA_OK = True


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
_COORDS = {
    "Nancy": (48.6896, 6.1737), "Frouard": (48.7565, 6.1245), "Pont-à-Mousson": (48.9035, 6.0606),
    "Metz": (49.1097, 6.1775), "Hagondange": (49.2442, 6.1639), "Uckange": (49.3050, 6.1667),
    "Thionville": (49.3587, 6.1563), "Bettembourg": (49.5187, 6.1042), "Luxembourg": (49.5997, 6.1339),
    "Forbach": (49.1878, 6.9011), "Saint-Avold": (49.1053, 6.7068), "Épinal": (48.1797, 6.4425),
    "Strasbourg": (48.5850, 7.7348), "Sarrebourg": (48.7352, 7.0536), "Sarreguemines": (49.1107, 7.0668),
    "Remiremont": (48.0165, 6.5914),
}


def _nom(uic: str) -> str:
    return bazo_geo.nom_pour_uic(uic) or uic


def distance_route_km(uic_a: str, uic_b: str) -> float | None:
    a, b = _COORDS.get(_nom(uic_a)), _COORDS.get(_nom(uic_b))
    if not a or not b:
        return None
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(h)) * FACTEUR_ROUTE, 1)


def plafond_participation(uic_a: str, uic_b: str, places: int) -> float | None:
    """Plafond par passager : coût kilométrique partagé entre le conducteur et les passagers."""
    d = distance_route_km(uic_a, uic_b)
    if d is None:
        return None
    return round(d * BAREME_EUR_KM / (places + 1), 2)


def _majeur(user: dict | None) -> bool:
    age = str((user or {}).get("age") or "")
    return not any(x in age.lower() for x in ("-18", "moins de 18", "mineur", "<18"))


def _a_checkin(conn, idt: str, date_j: str, numero: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM trajets_valides WHERE id_telegram=? AND date=? AND numero_train=? "
        "AND statut_validation NOT IN ('annule') LIMIT 1", (str(idt), date_j, numero)).fetchone())


# --------------------------------------------------------------------------
# Activation
# --------------------------------------------------------------------------
def _etat_train(conn, date_j: str, numero: str) -> dict | None:
    row = conn.execute("SELECT statut, terminus_effectif, retard_max_min, detail_json FROM etats_trains "
                       "WHERE date=? AND numero_train=?", (date_j, numero)).fetchone()
    if not row:
        return None
    return {"statut": row[0], "terminus_effectif": row[1], "retard_max": int(row[2] or 0),
            "detail": json.loads(row[3]) if row[3] else {}}


def _attente_prochain_train(date_j: str, numero: str, gare_uic: str, now_ts: int) -> int | None:
    """Minutes jusqu'au prochain train réellement circulant depuis la gare vers la même destination."""
    try:
        import bazo_live as L
        snap = L.lire_snapshot()
        if not snap:
            return None
        ref = next((t for t in snap["trains"] if t["numero"] == numero), None)
        if not ref:
            return None
        dest = ref["stops"][-1][0]
        pt = L.prochain_train(snap, gare_uic, dest, now_ts)
        if not pt:
            return None
        cands = [v for v in [pt] + pt.get("alternatives", []) + pt.get("suivants", [])
                 if v["statut"] != "supprime" and v["numero"] != numero and v["depart_ts"] >= now_ts]
        if not cands:
            return 24 * 60
        return max(0, (min(c["depart_ts"] for c in cands) - now_ts) // 60)
    except Exception:
        return None


def evaluer_activation(conn, date_j: str, numero: str, gare_uic: str, now_ts: int | None = None) -> dict:
    """Retourne {"actif": bool, "motif": str|None, "attente_min": int|None, "covoiturage": bool}."""
    now_ts = now_ts or int(time.time())
    etat = _etat_train(conn, date_j, numero)
    motif = None
    if etat:
        if etat["statut"] == "supprime":
            motif = "train_supprime"
        elif etat["statut"] == "partiel" and etat.get("terminus_effectif") == gare_uic:
            motif = "terminus_anticipe"
        elif etat["retard_max"] >= HUB_SEUIL_RETARD_MIN:
            motif = f"retard_{etat['retard_max']}_min"
    if not motif:
        nb = conn.execute(
            "SELECT COUNT(DISTINCT s.id_auteur) FROM signalements s JOIN trajets_valides t ON t.id=s.id_trajet "
            "WHERE s.date=? AND s.numero_train=? AND s.gare_uic=? AND s.code_type='je_suis_bloque' AND s.created_ts>?",
            (date_j, numero, gare_uic, now_ts - DUREE_HUB_S)).fetchone()[0]
        corrobore = etat is None or etat["retard_max"] >= HUB_CORROBORATION_MIN or etat["statut"] in ("supprime", "partiel")
        if nb >= HUB_QUORUM_BLOQUES and corrobore:
            motif = f"quorum_{nb}_bloques"
    attente = _attente_prochain_train(date_j, numero, gare_uic, now_ts) if motif else None
    return {"actif": bool(motif), "motif": motif, "attente_min": attente,
            "covoiturage": bool(motif) and attente is not None and attente >= HUB_SEUIL_ATTENTE_COVOIT_MIN}


def obtenir_ou_creer_hub(conn, date_j: str, numero: str, gare_uic: str, heure_theo: str | None = None,
                         now_ts: int | None = None) -> dict:
    init_schema()
    now_ts = now_ts or int(time.time())
    row = conn.execute("SELECT id FROM hubs WHERE date=? AND numero_train=? AND gare_uic=?",
                       (date_j, numero, gare_uic)).fetchone()
    if not row:
        conn.execute("INSERT INTO hubs (date, numero_train, gare_uic, heure_theo, created_ts, updated_ts) VALUES (?,?,?,?,?,?)",
                     (date_j, numero, gare_uic, heure_theo, now_ts, now_ts))
        conn.commit()
    return rafraichir_hub(conn, date_j, numero, gare_uic, now_ts)


def rafraichir_hub(conn, date_j: str, numero: str, gare_uic: str, now_ts: int | None = None) -> dict:
    now_ts = now_ts or int(time.time())
    row = conn.execute("SELECT id, statut, created_ts FROM hubs WHERE date=? AND numero_train=? AND gare_uic=?",
                       (date_j, numero, gare_uic)).fetchone()
    hub_id, statut, created = row
    if statut != "ferme":
        ev = evaluer_activation(conn, date_j, numero, gare_uic, now_ts)
        if now_ts - created > DUREE_HUB_S:
            statut = "ferme"
            conn.execute("UPDATE hubs SET statut='ferme', ferme_ts=?, updated_ts=? WHERE id=?", (now_ts, now_ts, hub_id))
            conn.execute("UPDATE hub_offres SET statut='expiree' WHERE hub_id=? AND statut IN ('ouverte','complete')", (hub_id,))
        else:
            statut = "actif" if ev["actif"] or statut == "actif" else "candidat"
            conn.execute("UPDATE hubs SET statut=?, motif_activation=COALESCE(?, motif_activation), "
                         "covoiturage_ouvert=MAX(covoiturage_ouvert, ?), attente_min=?, updated_ts=? WHERE id=?",
                         (statut, ev["motif"], 1 if ev["covoiturage"] else 0, ev["attente_min"], now_ts, hub_id))
        conn.execute("UPDATE hub_offres SET statut='expiree' WHERE hub_id=? AND statut IN ('ouverte','complete') AND expire_ts<?",
                     (hub_id, now_ts))
        conn.commit()
    return charger_hub(conn, hub_id)


def charger_hub(conn, hub_id: int, pour: str | None = None) -> dict:
    r = conn.execute("SELECT id, date, numero_train, gare_uic, heure_theo, statut, motif_activation, covoiturage_ouvert, "
                     "attente_min, created_ts FROM hubs WHERE id=?", (hub_id,)).fetchone()
    nb_bloques = conn.execute("SELECT COUNT(*) FROM hub_participants WHERE hub_id=? AND role='bloque'", (hub_id,)).fetchone()[0]
    offres = [charger_offre(conn, o[0], pour) for o in conn.execute(
        "SELECT id FROM hub_offres WHERE hub_id=? AND statut IN ('ouverte','complete','partie') ORDER BY depart_ts", (hub_id,))]
    moi_bloque = bool(pour and conn.execute("SELECT 1 FROM hub_participants WHERE hub_id=? AND id_utilisateur=? AND role='bloque'",
                                            (hub_id, str(pour))).fetchone())
    return {
        "id": r[0], "date": r[1], "numero_train": r[2], "gare_uic": r[3], "gare": _nom(r[3]), "heure_theo": r[4],
        "statut": r[5], "motif": r[6], "covoiturage_ouvert": bool(r[7]), "attente_min": r[8], "created_ts": r[9],
        "nb_bloques": nb_bloques, "moi_bloque": moi_bloque, "offres": offres,
        "points_rdv": POINTS_RDV, "mention": MENTION, "charte": CHARTE,
    }


def charger_offre(conn, offre_id: int, pour: str | None = None) -> dict:
    o = conn.execute("SELECT id, hub_id, id_conducteur, destination_uic, places, places_restantes, point_rdv_code, depart_ts, "
                     "participation_eur, contact, statut, created_ts FROM hub_offres WHERE id=?", (offre_id,)).fetchone()
    prenom = conn.execute("SELECT prenom FROM inscriptions WHERE id_telegram=?", (o[2],)).fetchone()
    passagers = conn.execute("SELECT id_passager FROM hub_reservations WHERE offre_id=? AND statut='confirmee'", (offre_id,)).fetchall()
    est_conducteur = pour is not None and str(pour) == str(o[2])
    est_passager = pour is not None and any(str(p[0]) == str(pour) for p in passagers)
    out = {
        "id": o[0], "hub_id": o[1], "conducteur": (prenom[0] if prenom and prenom[0] else "Voyageur"),
        "destination_uic": o[3], "destination": _nom(o[3]), "places": o[4], "places_restantes": o[5],
        "point_rdv_code": o[6], "point_rdv": POINTS_RDV.get(o[6], o[6]),
        "depart_ts": o[7], "depart": datetime.fromtimestamp(o[7], tz_paris).strftime("%Hh%M"),
        "participation_eur": o[8], "statut": o[10], "nb_passagers": len(passagers),
        "est_conducteur": est_conducteur, "est_passager": est_passager,
    }
    if est_conducteur or est_passager:
        out["contact"] = o[9]
    if est_conducteur:
        noms = conn.execute(
            f"SELECT prenom FROM inscriptions WHERE id_telegram IN ({','.join('?' * len(passagers))})",
            tuple(p[0] for p in passagers)).fetchall() if passagers else []
        out["passagers"] = [n[0] or "Voyageur" for n in noms]
    return out


# --------------------------------------------------------------------------
# Actions usager
# --------------------------------------------------------------------------
def declarer_bloque(idt: str, date_j: str, numero: str, heure_theo: str, gare: str,
                    now_ts: int | None = None) -> dict:
    """« Je suis bloqué » : signalement communautaire + participation au hub (check-in exigé)."""
    init_schema()
    now_ts = now_ts or int(time.time())
    from bazo_metier import creer_signalement
    uic = bazo_geo.uic_pour_gare(gare, "grand_est")
    if not uic:
        return {"ok": False, "erreur": "gare_inconnue"}
    res = creer_signalement(str(idt), date_j, numero, heure_theo, "je_suis_bloque")
    if not res.get("ok") and res.get("erreur") != "existant":
        return res
    conn = db_connect(timeout=10)
    try:
        hub = obtenir_ou_creer_hub(conn, date_j, numero, uic, heure_theo, now_ts)
        conn.execute("INSERT OR IGNORE INTO hub_participants (hub_id, id_utilisateur, role, ts) VALUES (?,?,'bloque',?)",
                     (hub["id"], str(idt), now_ts))
        conn.commit()
        hub = rafraichir_hub(conn, date_j, numero, uic, now_ts)
        return {"ok": True, "hub": charger_hub(conn, hub["id"], str(idt))}
    finally:
        conn.close()


def publier_offre(idt: str, hub_id: int, destination: str, places: int, point_rdv_code: str, depart_hhmm: str,
                  participation_eur: float, contact: str | None, accepte_charte: bool) -> dict:
    init_schema()
    if not accepte_charte:
        return {"ok": False, "erreur": "charte_requise"}
    if point_rdv_code not in POINTS_RDV:
        return {"ok": False, "erreur": "point_rdv_invalide"}
    if not 1 <= int(places) <= MAX_PLACES:
        return {"ok": False, "erreur": "places_invalides"}
    conn = db_connect(timeout=10)
    try:
        hub = conn.execute("SELECT date, numero_train, gare_uic, statut, covoiturage_ouvert FROM hubs WHERE id=?", (hub_id,)).fetchone()
        if not hub:
            return {"ok": False, "erreur": "hub_introuvable"}
        if hub[3] != "actif":
            return {"ok": False, "erreur": "hub_inactif"}
        if not hub[4]:
            return {"ok": False, "erreur": "covoiturage_ferme"}
        user = conn.execute("SELECT age FROM inscriptions WHERE id_telegram=?", (str(idt),)).fetchone()
        if not _majeur({"age": user[0] if user else ""}):
            return {"ok": False, "erreur": "majeur_requis"}
        if not conn.execute("SELECT 1 FROM trajets_valides WHERE id_telegram=? AND statut_validation LIKE 'valide%' LIMIT 1",
                            (str(idt),)).fetchone() and not _a_checkin(conn, idt, hub[0], hub[1]):
            return {"ok": False, "erreur": "checkin_requis"}
        if conn.execute("SELECT 1 FROM hub_offres WHERE id_conducteur=? AND statut IN ('ouverte','complete')", (str(idt),)).fetchone():
            return {"ok": False, "erreur": "offre_deja_active"}
        if conn.execute("SELECT COUNT(*) FROM hub_signalements_abus a JOIN hub_offres o ON o.id=a.offre_id WHERE o.id_conducteur=?",
                        (str(idt),)).fetchone()[0] >= 3:
            return {"ok": False, "erreur": "compte_suspendu"}
        dest_uic = bazo_geo.uic_pour_gare(destination, "grand_est")
        if not dest_uic or dest_uic == hub[2]:
            return {"ok": False, "erreur": "destination_invalide"}
        plafond = plafond_participation(hub[2], dest_uic, int(places))
        part = float(participation_eur or 0)
        if part < 0 or (plafond is not None and part > plafond + 0.005):
            return {"ok": False, "erreur": "participation_trop_elevee", "plafond_eur": plafond}
        try:
            h, m = depart_hhmm.lower().replace(":", "h").split("h")
            depart_ts = int(datetime.strptime(hub[0], "%Y-%m-%d").replace(hour=int(h), minute=int(m), tzinfo=tz_paris).timestamp())
        except Exception:
            return {"ok": False, "erreur": "heure_invalide"}
        now = int(time.time())
        if depart_ts < now - 300:
            return {"ok": False, "erreur": "heure_passee"}
        cur = conn.execute(
            "INSERT INTO hub_offres (hub_id, id_conducteur, destination_uic, places, places_restantes, point_rdv_code, depart_ts, "
            "participation_eur, contact, created_ts, expire_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (hub_id, str(idt), dest_uic, int(places), int(places), point_rdv_code, depart_ts, part,
             (contact or "").strip()[:80] or None, now, depart_ts + 15 * 60))
        conn.execute("INSERT OR IGNORE INTO hub_participants (hub_id, id_utilisateur, role, ts) VALUES (?,?,'conducteur',?)",
                     (hub_id, str(idt), now))
        conn.commit()
        return {"ok": True, "offre": charger_offre(conn, cur.lastrowid, str(idt)), "plafond_eur": plafond}
    finally:
        conn.close()


def reserver(idt: str, offre_id: int) -> dict:
    init_schema()
    conn = db_connect(timeout=10)
    try:
        o = conn.execute("SELECT hub_id, id_conducteur, places_restantes, statut, depart_ts FROM hub_offres WHERE id=?", (offre_id,)).fetchone()
        if not o:
            return {"ok": False, "erreur": "offre_introuvable"}
        if str(o[1]) == str(idt):
            return {"ok": False, "erreur": "conducteur"}
        if o[3] != "ouverte" or o[2] <= 0:
            return {"ok": False, "erreur": "complete"}
        user = conn.execute("SELECT age FROM inscriptions WHERE id_telegram=?", (str(idt),)).fetchone()
        if not _majeur({"age": user[0] if user else ""}):
            return {"ok": False, "erreur": "majeur_requis"}
        nb = conn.execute("SELECT COUNT(*) FROM hub_reservations r JOIN hub_offres o ON o.id=r.offre_id WHERE r.id_passager=? "
                          "AND r.statut='confirmee' AND o.statut IN ('ouverte','complete')", (str(idt),)).fetchone()[0]
        if nb >= MAX_RESERVATIONS_PASSAGER:
            return {"ok": False, "erreur": "trop_de_reservations"}
        if conn.execute("SELECT 1 FROM hub_reservations WHERE offre_id=? AND id_passager=? AND statut='confirmee'", (offre_id, str(idt))).fetchone():
            return {"ok": False, "erreur": "deja_reserve"}
        now = int(time.time())
        conn.execute("INSERT INTO hub_reservations (offre_id, id_passager, statut, ts) VALUES (?,?,'confirmee',?) "
                     "ON CONFLICT(offre_id, id_passager) DO UPDATE SET statut='confirmee', ts=excluded.ts", (offre_id, str(idt), now))
        restantes = o[2] - 1
        conn.execute("UPDATE hub_offres SET places_restantes=?, statut=? WHERE id=?",
                     (restantes, "complete" if restantes == 0 else "ouverte", offre_id))
        conn.execute("INSERT OR IGNORE INTO hub_participants (hub_id, id_utilisateur, role, ts) VALUES (?,?,'passager',?)", (o[0], str(idt), now))
        conn.commit()
        offre = charger_offre(conn, offre_id, str(idt))
        prenom = conn.execute("SELECT prenom FROM inscriptions WHERE id_telegram=?", (str(idt),)).fetchone()
        _notifier(o[1], f"🚗 *Entraide Bazo* — {prenom[0] if prenom and prenom[0] else 'Un voyageur'} a réservé une place "
                        f"dans ta voiture (départ {offre['depart']}, {offre['point_rdv']}). Places restantes : {restantes}.")
        return {"ok": True, "offre": offre}
    finally:
        conn.close()


def annuler_reservation(idt: str, offre_id: int) -> dict:
    init_schema()
    conn = db_connect(timeout=10)
    try:
        r = conn.execute("SELECT 1 FROM hub_reservations WHERE offre_id=? AND id_passager=? AND statut='confirmee'", (offre_id, str(idt))).fetchone()
        if not r:
            return {"ok": False, "erreur": "reservation_introuvable"}
        conn.execute("UPDATE hub_reservations SET statut='annulee' WHERE offre_id=? AND id_passager=?", (offre_id, str(idt)))
        conn.execute("UPDATE hub_offres SET places_restantes=MIN(places, places_restantes+1), "
                     "statut=CASE WHEN statut='complete' THEN 'ouverte' ELSE statut END WHERE id=?", (offre_id,))
        conn.commit()
        return {"ok": True, "offre": charger_offre(conn, offre_id, str(idt))}
    finally:
        conn.close()


def changer_statut_offre(idt: str, offre_id: int, statut: str) -> dict:
    """Conducteur : `partie` (je pars) ou `annulee`."""
    init_schema()
    if statut not in ("partie", "annulee"):
        return {"ok": False, "erreur": "statut_invalide"}
    conn = db_connect(timeout=10)
    try:
        o = conn.execute("SELECT id_conducteur, statut FROM hub_offres WHERE id=?", (offre_id,)).fetchone()
        if not o or str(o[0]) != str(idt):
            return {"ok": False, "erreur": "offre_introuvable"}
        conn.execute("UPDATE hub_offres SET statut=? WHERE id=?", (statut, offre_id))
        conn.commit()
        if statut == "annulee":
            for (p,) in conn.execute("SELECT id_passager FROM hub_reservations WHERE offre_id=? AND statut='confirmee'", (offre_id,)):
                _notifier(p, "🚗 *Entraide Bazo* — le conducteur a annulé son offre. Regarde les autres places disponibles dans l'application.")
        return {"ok": True, "offre": charger_offre(conn, offre_id, str(idt))}
    finally:
        conn.close()


def signaler_abus(idt: str, offre_id: int, motif: str) -> dict:
    init_schema()
    conn = db_connect(timeout=10)
    try:
        if not conn.execute("SELECT 1 FROM hub_offres WHERE id=?", (offre_id,)).fetchone():
            return {"ok": False, "erreur": "offre_introuvable"}
        conn.execute("INSERT OR IGNORE INTO hub_signalements_abus (offre_id, id_auteur, motif, ts) VALUES (?,?,?,?)",
                     (offre_id, str(idt), (motif or "")[:120], int(time.time())))
        nb = conn.execute("SELECT COUNT(*) FROM hub_signalements_abus WHERE offre_id=?", (offre_id,)).fetchone()[0]
        if nb >= 3:
            conn.execute("UPDATE hub_offres SET statut='suspendue' WHERE id=?", (offre_id,))
        conn.commit()
        return {"ok": True, "nb_signalements": nb}
    finally:
        conn.close()


def _notifier(idt: str, texte: str) -> None:
    """Message transactionnel (déclenché par une action d'un autre usager) : passe par bazo_notify (simulation respectée)."""
    try:
        if bazo_notify.SIMULATION or bazo_notify.envoi_actif():
            bazo_notify.envoyer(idt, texte)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Vue usager
# --------------------------------------------------------------------------
def hubs_pour_usager(idt: str, date_j: str | None = None) -> dict:
    """Hubs du jour pertinents : mes check-ins + mes gares de profil. Les hubs candidats n'apparaissent
    que si j'y participe ; les hubs actifs de mes gares apparaissent toujours."""
    init_schema()
    date_j = date_j or datetime.now(tz_paris).strftime("%Y-%m-%d")
    now = int(time.time())
    conn = db_connect(timeout=10)
    try:
        user = conn.execute("SELECT gare_depart, gare_retour FROM inscriptions WHERE id_telegram=?", (str(idt),)).fetchone()
        mes_gares = {bazo_geo.uic_pour_gare(g, "grand_est") for g in (user or ()) if g}
        mes_trains = {r[0] for r in conn.execute("SELECT numero_train FROM trajets_valides WHERE id_telegram=? AND date=?", (str(idt), date_j))}
        for (hid, num, uic) in conn.execute("SELECT id, numero_train, gare_uic FROM hubs WHERE date=? AND statut!='ferme'", (date_j,)).fetchall():
            rafraichir_hub(conn, date_j, num, uic, now)
        out = []
        for (hid, num, uic, statut) in conn.execute("SELECT id, numero_train, gare_uic, statut FROM hubs WHERE date=? ORDER BY created_ts DESC", (date_j,)).fetchall():
            participe = bool(conn.execute("SELECT 1 FROM hub_participants WHERE hub_id=? AND id_utilisateur=?", (hid, str(idt))).fetchone())
            pertinent = participe or num in mes_trains or (statut == "actif" and uic in mes_gares)
            if pertinent and (statut == "actif" or participe):
                out.append(charger_hub(conn, hid, str(idt)))
        mes_checkins = [{"numero_train": r[0], "heure_theo": r[1], "gare_depart": r[2], "gare_arrivee": r[3]} for r in conn.execute(
            "SELECT numero_train, heure_theo, gare_depart, gare_arrivee FROM trajets_valides WHERE id_telegram=? AND date=? "
            "AND statut_validation!='annule' ORDER BY heure_theo", (str(idt), date_j))]
        return {"date": date_j, "hubs": out, "mes_checkins": mes_checkins, "seuils": {
            "retard_min": HUB_SEUIL_RETARD_MIN, "attente_covoit_min": HUB_SEUIL_ATTENTE_COVOIT_MIN, "quorum": HUB_QUORUM_BLOQUES},
            "points_rdv": POINTS_RDV, "mention": MENTION, "charte": CHARTE, "bareme_eur_km": BAREME_EUR_KM}
    finally:
        conn.close()


def ouvrir_hubs_depuis_etats(conn, changements: list[dict], now_ts: int) -> int:
    """Appelé par le service temps réel : un train supprimé / limité ouvre automatiquement un hub (candidat→actif)
    à sa gare de rupture pour les usagers ayant un check-in dessus."""
    init_schema()
    nb = 0
    for c in changements:
        if c.get("type") not in ("suppression_totale", "suppression_partielle", "retard_majeur"):
            continue
        date_j, numero = c.get("date"), c.get("numero")
        if not date_j or not numero:
            continue
        gares = conn.execute("SELECT DISTINCT gare_depart, heure_theo FROM trajets_valides WHERE date=? AND numero_train=? "
                             "AND statut_validation!='annule'", (date_j, numero)).fetchall()
        etat = c.get("etat") or {}
        rupture = None
        if c.get("type") == "suppression_partielle":
            import bazo_sources_rt as S
            rupture = S.terminus_effectif_uic(etat)
        for g, h in gares:
            uic = rupture or bazo_geo.uic_pour_gare(g, "grand_est")
            if uic:
                obtenir_ou_creer_hub(conn, date_j, numero, uic, h, now_ts)
                nb += 1
    return nb
