"""Instantané temps réel du Sillon Lorrain pour l'écran d'accueil mobile (mini-map + tiroir).

Le service temps réel construit un instantané à chaque cycle (`construire_snapshot`) et le stocke dans
`memoire_radar` (clé `live_sillon`). L'API ne fait que le lire : aucun appel externe côté requête.

Codes couleur : fluide (émeraude) · retard mineur ≥ 5 min (ambre) · suppression (rouge).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import bazo_geo
import bazo_sources_rt as S
import bazo_temps_reel as TR
from bazo_config import db_connect

tz_paris = ZoneInfo("Europe/Paris")

CLE_SNAPSHOT = "live_sillon"
SEUIL_RETARD_MINEUR = 5          # min : en dessous, le tracé reste vert
FENETRE_AVANT_S = 600            # un tronçon reste « actif » 10 min après le passage du train
FENETRE_APRES_S = 1800           # … et 30 min avant
HORIZON_TRAINS_S = 4 * 3600      # trains conservés pour « prochain train » et alternatives
PEREMPTION_S = 600               # au-delà : données jugées périmées par le client

# Schéma du tracé (WGS84 approximatif — carte volontairement épurée, pas un fond cartographique).
_STATIONS_AFFICHEES = [
    ("Nancy", 48.6896, 6.1737),
    ("Frouard", 48.7565, 6.1245),
    ("Pont-à-Mousson", 48.9035, 6.0606),
    ("Metz", 49.1097, 6.1775),
    ("Hagondange", 49.2442, 6.1639),
    ("Uckange", 49.3050, 6.1667),
    ("Thionville", 49.3587, 6.1563),
    ("Bettembourg", 49.5187, 6.1042),
    ("Luxembourg", 49.5997, 6.1339),
]
_RANG = {"fluide": 0, "retard": 1, "supprime": 2}


def stations_affichees() -> list[dict]:
    out = []
    for nom, lat, lon in _STATIONS_AFFICHEES:
        uic = bazo_geo.uic_pour_gare(nom, "grand_est")
        if uic:
            out.append({"nom": nom, "uic": uic, "lat": lat, "lon": lon})
    return out


def ordre_axe() -> dict[str, int]:
    """UIC -> rang le long du Sillon (Nancy = 0 … Luxembourg = n)."""
    ordre: dict[str, int] = {}
    for i, nom in enumerate(bazo_geo.axes("grand_est")["sillon"]):
        uic = bazo_geo.uic_pour_gare(nom, "grand_est")
        if uic and uic not in ordre:
            ordre[uic] = i
    return ordre


# --------------------------------------------------------------------------
# Construction de l'instantané
# --------------------------------------------------------------------------
def _arrets_reseau(etat: dict, ordre: dict[str, int]) -> list[dict]:
    pts = []
    for a in etat.get("arrets", []):
        if a["uic"] not in ordre:
            continue
        _p, t_in, r_in = TR.passage(etat, a["uic"], "arr")
        _p2, t_out, r_out = TR.passage(etat, a["uic"], "dep")
        t_in = t_in or t_out
        t_out = t_out or t_in
        if not t_in:
            continue
        pts.append({"uic": a["uic"], "ord": ordre[a["uic"]], "t_in": int(t_in), "t_out": int(t_out),
                    "skip": bool(a.get("skipped")), "ret": int(r_out if r_out is not None else r_in or 0),
                    "ret_in": int(r_in or 0)})
    # Un train n'est « sur le sillon » que si ses arrêts progressent dans un seul sens.
    if len(pts) < 2:
        return []
    sens = 1 if pts[-1]["ord"] > pts[0]["ord"] else -1
    pts = [p for i, p in enumerate(pts) if i == 0 or (p["ord"] - pts[i - 1]["ord"]) * sens > 0]
    return pts if len(pts) >= 2 else []


def _statut_train(etat: dict, pts: list[dict]) -> str:
    if etat.get("annule"):
        return "supprime"
    if S.arrets_supprimes_fin(etat) or S.arrets_supprimes_debut(etat):
        return "partiel"
    return "retard" if max((p["ret"] for p in pts if not p["skip"]), default=0) >= SEUIL_RETARD_MINEUR else "fluide"


def _hops(etat: dict, pts: list[dict]) -> list[tuple[int, int, str, int, int, int]]:
    """Tronçons entre arrêts consécutifs : (ord_a, ord_b, statut, retard, t_depart, t_arrivee)."""
    annule = bool(etat.get("annule"))
    servis = [i for i, p in enumerate(pts) if not p["skip"]]
    premier, dernier = (servis[0], servis[-1]) if servis else (None, None)
    out = []
    for i in range(len(pts) - 1):
        p, q = pts[i], pts[i + 1]
        coupe = annule or premier is None or i < premier or (i + 1) > dernier
        ret = max(p["ret"], q["ret_in"], 0)
        statut = "supprime" if coupe else ("retard" if ret >= SEUIL_RETARD_MINEUR else "fluide")
        out.append((p["ord"], q["ord"], statut, ret, p["t_out"], q["t_in"]))
    return out


def construire_snapshot(finals: dict, now_ts: int | None = None) -> dict:
    now_ts = int(now_ts or time.time())
    stations = stations_affichees()
    ordre = ordre_axe()
    rang_aff = [ordre[s["uic"]] for s in stations]
    segments = [{"a": i, "b": i + 1, "statut": "fluide", "retard_max": 0, "nb_trains": 0}
                for i in range(len(stations) - 1)]
    trains, nb_suppr, nb_ret = [], 0, 0
    for (_date, numero), etat in finals.items():
        pts = _arrets_reseau(etat, ordre)
        if not pts:
            continue
        t_debut, t_fin = pts[0]["t_out"], pts[-1]["t_in"]
        if t_fin < now_ts - FENETRE_AVANT_S or t_debut > now_ts + HORIZON_TRAINS_S:
            continue
        statut = _statut_train(etat, pts)
        sens = 1 if pts[-1]["ord"] > pts[0]["ord"] else -1
        for (oa, ob, st_h, ret_h, t_dep, t_arr) in _hops(etat, pts):
            if t_dep > now_ts + FENETRE_APRES_S or t_arr < now_ts - FENETRE_AVANT_S:
                continue
            lo, hi = min(oa, ob), max(oa, ob)
            for seg in segments:
                a_ord, b_ord = rang_aff[seg["a"]], rang_aff[seg["b"]]
                if max(lo, a_ord) < min(hi, b_ord):          # le tronçon recouvre le segment affiché
                    seg["nb_trains"] += 1
                    seg["retard_max"] = max(seg["retard_max"], ret_h if st_h != "supprime" else 0)
                    if _RANG[st_h] > _RANG[seg["statut"]]:
                        seg["statut"] = st_h
        if statut in ("supprime", "partiel") and t_fin >= now_ts - FENETRE_AVANT_S and t_debut <= now_ts + FENETRE_APRES_S:
            nb_suppr += 1
        elif statut == "retard":
            nb_ret += 1
        trains.append({
            "numero": numero, "sens": sens, "statut": statut,
            "retard": max((p["ret"] for p in pts if not p["skip"]), default=0),
            "stops": [[p["uic"], p["t_in"], p["t_out"], 1 if p["skip"] else 0, p["ret"]] for p in pts],
        })
    pire = max((s["statut"] for s in segments), key=lambda x: _RANG[x], default="fluide")
    return {
        "generated_ts": now_ts,
        "stations": stations,
        "segments": segments,
        "trains": sorted(trains, key=lambda t: t["stops"][0][2]),
        "resume": {"statut": pire, "nb_trains": len(trains), "nb_retards": nb_ret, "nb_supprimes": nb_suppr},
    }


def sauver_snapshot(conn, snap: dict) -> None:
    conn.execute(
        "INSERT INTO memoire_radar (cle, valeur, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur, updated_at=excluded.updated_at",
        (CLE_SNAPSHOT, json.dumps(snap, ensure_ascii=False, separators=(",", ":"))),
    )
    conn.commit()


def lire_snapshot() -> dict | None:
    conn = db_connect(timeout=5)
    try:
        row = conn.execute("SELECT valeur FROM memoire_radar WHERE cle=?", (CLE_SNAPSHOT,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Prochain train de l'usager (+ alternatives)
# --------------------------------------------------------------------------
def _hhmm(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz_paris).strftime("%Hh%M") if ts else None


def _etiquette(statut: str, retard: int) -> str:
    if statut == "supprime":
        return "Supprimé"
    if statut == "partiel":
        return "Terminus anticipé"
    return f"+{retard} min" if retard >= 1 else "À l'heure"


def _vue_train(t: dict, uic_dep: str, uic_arr: str) -> dict | None:
    """Vue d'un train de l'instantané sur le segment (uic_dep -> uic_arr) ; None s'il ne le dessert pas."""
    idx = {s[0]: i for i, s in enumerate(t["stops"])}
    if uic_dep not in idx or uic_arr not in idx or idx[uic_arr] <= idx[uic_dep]:
        return None
    d, a = t["stops"][idx[uic_dep]], t["stops"][idx[uic_arr]]
    statut = t["statut"]
    if statut == "partiel":                       # partiel : ne concerne l'usager que si SON segment est coupé
        statut = "supprime" if (d[3] or a[3]) else ("retard" if max(d[4], a[4]) >= SEUIL_RETARD_MINEUR else "fluide")
    ret_dep, ret_arr = int(d[4] or 0), int(a[4] or 0)
    return {
        "numero": t["numero"], "statut": statut,
        "depart_ts": d[2], "arrivee_ts": a[1],
        "depart_plan": _hhmm(d[2] - ret_dep * 60), "depart_reel": _hhmm(d[2]),
        "arrivee_plan": _hhmm(a[1] - ret_arr * 60), "arrivee_reelle": _hhmm(a[1]),
        "retard_depart": ret_dep, "retard_arrivee": ret_arr,
        "etiquette": _etiquette(statut, max(ret_dep, ret_arr)),
    }


def prochain_train(snap: dict, uic_dep: str, uic_arr: str, now_ts: int,
                   habituels: list[str] | None = None) -> dict | None:
    """Prochain train de l'usager sur (uic_dep -> uic_arr). `habituels` = horaires 'HHhMM' déclarés (prioritaires)."""
    toutes = [v for v in (_vue_train(t, uic_dep, uic_arr) for t in snap.get("trains", [])) if v]
    vues = sorted((v for v in toutes if v["depart_ts"] >= now_ts - 120), key=lambda v: v["depart_ts"])
    hab = set(habituels or [])
    # Le train habituel reste affiché tant qu'il n'est pas arrivé (ex. immobilisé en route : c'est lui qui compte).
    en_route = sorted((v for v in toutes if v["depart_ts"] < now_ts - 120 and v["arrivee_ts"] >= now_ts - 120
                       and v["depart_plan"] in hab), key=lambda v: v["depart_ts"])
    if not vues and not en_route:
        return None
    choisi, habituel = (vues[0] if vues else en_route[0]), False
    if en_route:
        choisi, habituel = en_route[0], True
    elif hab:
        for v in vues:
            if v["depart_plan"] in hab and v["depart_ts"] <= now_ts + 4 * 3600:
                choisi, habituel = v, True
                break
    choisi = {**choisi, "en_cours": choisi["depart_ts"] < now_ts - 120}
    alternatives = []
    if choisi["statut"] == "supprime" or choisi["retard_depart"] >= 15 or choisi["retard_arrivee"] >= 15:
        circulants = [v for v in vues if v["statut"] != "supprime" and v["numero"] != choisi["numero"]]
        prec = [v for v in circulants if v["depart_ts"] < choisi["depart_ts"]]
        suiv = [v for v in circulants if v["depart_ts"] >= choisi["depart_ts"] or choisi["en_cours"]]
        if prec:
            alternatives.append({**prec[-1], "relation": "precedent"})
        alternatives += [{**v, "relation": "suivant"} for v in suiv[:2]]
    nom = lambda u: bazo_geo.nom_pour_uic(u) or u
    suivants = [v for v in vues if v["numero"] != choisi["numero"] and v["depart_ts"] > choisi["depart_ts"]][:4]
    return {**choisi, "habituel": habituel, "depart_gare": nom(uic_dep), "arrivee_gare": nom(uic_arr),
            "uic_dep": uic_dep, "uic_arr": uic_arr, "alternatives": alternatives, "suivants": suivants}


_RE_HEURE = re.compile(r"^\d{2}h\d{2}$")


def vue_usager(snap: dict | None, user: dict, habituels_par_trajet: dict, now_ts: int | None = None) -> dict:
    """Bloc personnalisé de l'écran d'accueil : prochain train de l'usager (aller ou retour, le plus proche)."""
    now_ts = int(now_ts or time.time())
    if not snap:
        return {"prochain_train": None, "raison": "donnees_indisponibles"}
    ordre = ordre_axe()
    g_d, g_r = user.get("gare_depart"), user.get("gare_retour")
    u_d = bazo_geo.uic_pour_gare(g_d, "grand_est") if g_d else None
    u_r = bazo_geo.uic_pour_gare(g_r, "grand_est") if g_r else None
    if not u_d or not u_r or u_d not in ordre or u_r not in ordre or u_d == u_r:
        return {"prochain_train": None, "raison": "trajet_hors_sillon"}
    candidats = []
    for ud, ua in ((u_d, u_r), (u_r, u_d)):
        hab = [h for h in habituels_par_trajet.get((ud, ua), []) if _RE_HEURE.match(h or "")]
        pt = prochain_train(snap, ud, ua, now_ts, hab)
        if pt:
            candidats.append(pt)
    if not candidats:
        return {"prochain_train": None, "raison": "aucun_train"}
    return {"prochain_train": min(candidats, key=lambda p: p["depart_ts"])}
