"""Moteur d'alternatives : train précédent / suivant qui circule réellement.

Entrée : un segment (UIC départ -> UIC arrivée), une heure de référence (le train habituel ou
supprimé) et des filtres de vérité terrain (états temps réel fusionnés, plan de grève).
Sortie : au plus un train AVANT et un train APRÈS, avec heure de départ et arrivée estimée.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import bazo_geo
import bazo_sources_rt as S

logger = logging.getLogger("bazo.alternatives")
tz_paris = ZoneInfo("Europe/Paris")
_HTTP = requests.Session()


def _ts_navitia(s: str | None) -> int | None:
    if not s or len(s) < 15:
        return None
    try:
        return int(datetime.strptime(s, "%Y%m%dT%H%M%S").replace(tzinfo=tz_paris).timestamp())
    except ValueError:
        return None


def parser_journeys_directs(donnees: dict) -> list[dict]:
    """Extrait les trajets directs d'une réponse Navitia /journeys."""
    out = []
    for j in donnees.get("journeys", []) or []:
        pts = [s for s in j.get("sections", []) if s.get("type") == "public_transport"]
        if len(pts) != 1:
            continue
        s = pts[0]
        info = s.get("display_informations", {}) or {}
        blob = " ".join(str(info.get(k, "")) for k in ("commercial_mode", "network", "physical_mode")).upper()
        if any(x in blob for x in ("TGV", "OUIGO", "AUTOCAR", "BUS")):
            continue
        sdt = s.get("stop_date_times") or []
        if len(sdt) < 2:
            continue
        dep_rt = _ts_navitia(sdt[0].get("departure_date_time"))
        dep_plan = _ts_navitia(sdt[0].get("base_departure_date_time")) or dep_rt
        arr_rt = _ts_navitia(sdt[-1].get("arrival_date_time"))
        arr_plan = _ts_navitia(sdt[-1].get("base_arrival_date_time")) or arr_rt
        if not dep_rt or not arr_rt:
            continue
        statut = str(j.get("status", "")).upper()
        out.append({
            "numero": str(info.get("headsign") or info.get("trip_short_name") or "").strip(),
            "dep_plan": dep_plan, "dep_rt": dep_rt, "arr_plan": arr_plan, "arr_rt": arr_rt,
            "ret_dep": round((dep_rt - dep_plan) / 60), "ret_arr": round((arr_rt - arr_plan) / 60),
            "annule": statut in ("NO_SERVICE",),
            "source": "navitia",
        })
    out.sort(key=lambda c: c["dep_rt"])
    return out


def journeys_directs(uic_from: str, uic_to: str, depuis: datetime, count: int = 12,
                     fraicheur: str = "realtime") -> list[dict]:
    """Appel Navitia (quota compté). Liste vide si indisponible."""
    from bazo_config import TOKEN_SNCF
    from bazo_quota import RESERVE_CHECKIN, peut_appeler

    if not peut_appeler("alternatives", reserve=RESERVE_CHECKIN):
        return []
    try:
        r = _HTTP.get(
            "https://api.sncf.com/v1/coverage/sncf/journeys",
            params={
                "from": f"stop_area:SNCF:{uic_from}", "to": f"stop_area:SNCF:{uic_to}",
                "datetime": depuis.strftime("%Y%m%dT%H%M%S"), "datetime_represents": "departure",
                "count": count, "data_freshness": fraicheur, "max_nb_transfers": 0,
            },
            auth=(TOKEN_SNCF, ""), timeout=25,
        )
        S.compter_appel_sncf("alternatives")
    except requests.RequestException as e:
        logger.warning("[ALT] Navitia journeys : %s", e)
        return []
    if r.status_code != 200:
        return []
    return parser_journeys_directs(r.json())


def candidats_depuis_etats(finals: dict, date_j: str, uic_from: str, uic_to: str) -> list[dict]:
    """Trains desservant uic_from puis uic_to d'après les états temps réel fusionnés (sans quota)."""
    out = []
    for (d, numero), e in finals.items():
        if d != date_j or e.get("annule"):
            continue
        i_f, i_t = S.index_arret(e, uic_from), S.index_arret(e, uic_to)
        if i_f < 0 or i_t <= i_f:
            continue
        a_f, a_t = e["arrets"][i_f], e["arrets"][i_t]
        if a_f.get("skipped") or a_t.get("skipped"):
            continue
        if any(a.get("skipped") for a in e["arrets"][i_f:i_t + 1]) and e["arrets"][i_t]["skipped"]:
            continue
        ret_dep = a_f.get("ret_dep") if a_f.get("ret_dep") is not None else a_f.get("ret_arr")
        ret_arr = a_t.get("ret_arr") if a_t.get("ret_arr") is not None else a_t.get("ret_dep")
        dep_plan = a_f.get("plan_dep") or a_f.get("plan_arr")
        arr_plan = a_t.get("plan_arr") or a_t.get("plan_dep")
        dep_rt = a_f.get("t_dep") or a_f.get("t_arr") or (dep_plan + (ret_dep or 0) * 60 if dep_plan else None)
        arr_rt = a_t.get("t_arr") or a_t.get("t_dep") or (arr_plan + (ret_arr or 0) * 60 if arr_plan else None)
        if not dep_rt or not arr_rt:
            continue
        out.append({
            "numero": numero, "dep_plan": dep_plan or dep_rt, "dep_rt": dep_rt,
            "arr_plan": arr_plan or arr_rt, "arr_rt": arr_rt,
            "ret_dep": ret_dep or 0, "ret_arr": ret_arr or 0, "annule": False, "source": "temps_reel",
        })
    out.sort(key=lambda c: c["dep_rt"])
    return out


def filtrer_candidats(cands: list[dict], finals: dict | None, date_j: str, exclure: set[str] | None = None,
                      statuts_plan: dict | None = None) -> list[dict]:
    """Retire les trains annulés / limités (états temps réel) ou supprimés (plan de grève)."""
    exclure = exclure or set()
    out = []
    for c in cands:
        n = c["numero"]
        if n in exclure or c.get("annule"):
            continue
        if finals:
            e = finals.get((date_j, n))
            if e and (e.get("annule")):
                continue
        if statuts_plan and statuts_plan.get(n) == "supprime":
            continue
        out.append(c)
    return out


def choisir_alternatives(cands: list[dict], ref_ts: int, now_ts: int) -> tuple[dict | None, dict | None]:
    """Précédent = dernier train partant avant `ref_ts` et encore attrapable (>= maintenant).
    Suivant = premier train partant après `ref_ts` (et >= maintenant)."""
    precedent = None
    suivant = None
    for c in cands:
        if c["dep_rt"] < now_ts - 60:
            continue
        if c["dep_rt"] < ref_ts - 60:
            precedent = c
        elif c["dep_rt"] > ref_ts + 60 and suivant is None:
            suivant = c
    return precedent, suivant


def _hh(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz_paris).strftime("%Hh%M")


def libelle_candidat(c: dict, nom_arrivee: str) -> str:
    ret_d = f" (+{c['ret_dep']} min)" if c.get("ret_dep", 0) >= 3 else ""
    ret_a = f" (+{c['ret_arr']} min)" if c.get("ret_arr", 0) >= 3 else ""
    return (f"départ *{_hh(c['dep_rt'])}*{ret_d}, arrivée {nom_arrivee} estimée *{_hh(c['arr_rt'])}*{ret_a}"
            f" — train {c['numero']}")


def formater_alternatives(precedent: dict | None, suivant: dict | None, nom_arrivee: str) -> str:
    lignes = []
    if precedent:
        lignes.append(f"⬅️ *Train précédent* : {libelle_candidat(precedent, nom_arrivee)}")
    else:
        lignes.append("⬅️ *Train précédent* : aucun train encore atteignable")
    if suivant:
        lignes.append(f"➡️ *Train suivant* : {libelle_candidat(suivant, nom_arrivee)}")
    else:
        lignes.append("➡️ *Train suivant* : aucun train annoncé pour l'instant")
    return "\n".join(lignes)


def chercher_alternatives(date_j: str, uic_from: str, uic_to: str, ref_ts: int, now: datetime | None = None,
                          finals: dict | None = None, statuts_plan: dict | None = None,
                          exclure: set[str] | None = None, utiliser_navitia: bool = True) -> dict:
    """Retourne {"precedent":.., "suivant":.., "texte":..}. Navitia est la source du plan ; les états
    temps réel et le plan de grève servent de filtre de vérité terrain."""
    now = now or datetime.now(tz_paris)
    cands: list[dict] = []
    if finals:
        cands = candidats_depuis_etats(finals, date_j, uic_from, uic_to)
    if utiliser_navitia:
        depuis = datetime.fromtimestamp(min(ref_ts, int(now.timestamp())) - 3 * 3600, tz_paris)
        nav = journeys_directs(uic_from, uic_to, depuis, count=16)
        deja = {c["numero"] for c in cands}
        cands.extend(c for c in nav if c["numero"] not in deja)
        cands.sort(key=lambda c: c["dep_rt"])
    cands = filtrer_candidats(cands, finals, date_j, exclure, statuts_plan)
    prec, suiv = choisir_alternatives(cands, ref_ts, int(now.timestamp()))
    nom_arr = bazo_geo.nom_pour_uic(uic_to) or "destination"
    return {"precedent": prec, "suivant": suiv, "texte": formater_alternatives(prec, suiv, nom_arr)}
