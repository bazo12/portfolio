"""Sources temps réel multi-fournisseurs et modèle d'état « train » unifié.

Sources :
  - SNCF GTFS-RT TripUpdates (transport.data.gouv.fr)  : annulations, arrêts SKIPPED, retards, 1 appel / réseau.
  - CFL / mobiliteit.lu (HAFAS ATP OpenData)             : tronçon Bettembourg - Luxembourg (TER SNCF inclus).
  - SNCF Navitia vehicle_journeys (perturbations)         : repli ciblé sur un train suivi.

Modèle d'état d'un train (dict) :
    {"numero", "date", "sources": [..], "annule": bool, "ajoute": bool,
     "arrets": [{"uic","nom","skipped","ret_arr","ret_dep","t_arr","t_dep","plan_arr","plan_dep"}], "ts"}
Les temps sont des epoch UTC (secondes). Les retards sont en minutes entières.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import bazo_geo

logger = logging.getLogger("bazo.sources_rt")
tz_paris = ZoneInfo("Europe/Paris")

URL_GTFS_RT = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
CFL_BASE = "https://cdt.hafas.de/opendata/apiserver"

# Gares CFL (extId HAFAS) utilisées pour interroger les tableaux de départs.
CFL_GARES = {
    "luxembourg": "300031019",
    "bettembourg": "300134002",
    "thionville": "400000098",
    "metz": "400000071",
}
# extId HAFAS -> UIC (stop_area SNCF) pour recouper avec GTFS-RT.
CFL_EXTID_UIC = {
    "300031019": "82001000",   # Luxembourg
    "300010002": "82002501",   # Howald
    "300134002": "82006030",   # Bettembourg
    "400000098": "87191007",   # Thionville
    "400000094": "87191130",   # Uckange
    "400000012": "87191114",   # Hagondange
    "400000071": "87192039",   # Metz-Ville
}

_HTTP = requests.Session()
_cfl_pause_jusqua = 0.0


class SourceIndisponible(Exception):
    pass


def compter_appel_sncf(source: str) -> None:
    """Incrémente le quota SNCF sans jamais faire échouer l'appelant (verrou SQLite transitoire)."""
    import sqlite3
    from bazo_quota import enregistrer_appel
    for _ in range(4):
        try:
            enregistrer_appel(source)
            return
        except sqlite3.OperationalError:
            time.sleep(0.6)
    logger.warning("[QUOTA] compteur non incrémenté (base verrouillée) pour %s", source)


# --------------------------------------------------------------------------
# Utilitaires purs
# --------------------------------------------------------------------------
def numero_depuis_texte(texte: str) -> str | None:
    """'TER88627' -> '88627' ; 'RE 5135' -> None (hors TER SNCF)."""
    m = re.match(r"^\s*TER\s*0*(\d{3,6})\s*$", str(texte or ""), re.IGNORECASE)
    return m.group(1) if m else None


def uic_depuis_stop_id(stop_id: str) -> str | None:
    m = re.search(r"(\d{7,8})\s*$", str(stop_id or ""))
    return m.group(1) if m else None


def date_iso(start_date: str) -> str:
    s = str(start_date or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def epoch_vers_hhmm(ts: int | None) -> str | None:
    """Epoch -> 'HHhMM' heure de Paris (format bot)."""
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz_paris).strftime("%Hh%M")


def hhmm_vers_epoch(date_j: str, hhmm: str) -> int | None:
    """('2026-11-05','08h33') -> epoch. None si format invalide."""
    m = re.match(r"^\s*(\d{1,2})[h:](\d{2})\s*$", str(hhmm or ""))
    if not m:
        return None
    try:
        d = datetime.strptime(date_j, "%Y-%m-%d").replace(
            hour=int(m.group(1)), minute=int(m.group(2)), tzinfo=tz_paris,
        )
        return int(d.timestamp())
    except ValueError:
        return None


def retard_arret(a: dict) -> int | None:
    """Retard (min) à un arrêt : arrivée si connue, sinon départ."""
    r = a.get("ret_arr")
    return r if r is not None else a.get("ret_dep")


def propager_retards(arrets: list[dict]) -> list[dict]:
    """Un arrêt sans donnée hérite du dernier retard connu (convention GTFS-RT)."""
    dernier = None
    for a in arrets:
        r = retard_arret(a)
        if r is not None:
            dernier = r
        elif dernier is not None and not a.get("skipped"):
            a["ret_arr"] = dernier
            a["propage"] = True
    return arrets


def arrets_desservis(etat: dict) -> list[dict]:
    return [a for a in etat.get("arrets", []) if not a.get("skipped")]


def terminus_prevu_uic(etat: dict) -> str | None:
    ar = etat.get("arrets") or []
    return ar[-1]["uic"] if ar else None


def terminus_effectif_uic(etat: dict) -> str | None:
    d = arrets_desservis(etat)
    return d[-1]["uic"] if d else None


def arrets_supprimes_fin(etat: dict) -> list[str]:
    """UIC des arrêts non desservis situés APRÈS le dernier arrêt desservi (limitation de parcours)."""
    ar = etat.get("arrets") or []
    out: list[str] = []
    for a in reversed(ar):
        if a.get("skipped"):
            out.append(a["uic"])
        else:
            break
    return list(reversed(out)) if len(out) < len(ar) else []


def arrets_supprimes_debut(etat: dict) -> list[str]:
    """Arrêts non desservis AVANT le premier arrêt desservi (départ anticipé / origine supprimée)."""
    ar = etat.get("arrets") or []
    out: list[str] = []
    for a in ar:
        if a.get("skipped"):
            out.append(a["uic"])
        else:
            break
    return out if len(out) < len(ar) else []


def retard_max(etat: dict) -> int:
    vals = [retard_arret(a) for a in etat.get("arrets", []) if not a.get("skipped")]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else 0


def classifier(etat: dict) -> str:
    """normal | retard | supprime | partiel"""
    if etat.get("annule"):
        return "supprime"
    if arrets_supprimes_fin(etat) or arrets_supprimes_debut(etat):
        return "partiel"
    return "retard" if retard_max(etat) >= 10 else "normal"


def index_arret(etat: dict, uic: str) -> int:
    for i, a in enumerate(etat.get("arrets", [])):
        if a["uic"] == uic:
            return i
    return -1


def nom_gare(uic: str | None) -> str:
    if not uic:
        return "arrêt inconnu"
    return bazo_geo.nom_pour_uic(uic) or f"gare {uic}"


# --------------------------------------------------------------------------
# GTFS-RT SNCF
# --------------------------------------------------------------------------
def _uic_surveilles() -> set[str]:
    s = set(bazo_geo.tous_uic_surveilles("grand_est"))
    s.update(CFL_EXTID_UIC.values())
    return s


def lire_gtfs_rt(contenu: bytes | None = None, uic_filtre: set[str] | None = None) -> dict:
    """Retourne {(date_iso, numero): etat}. `contenu` permet de rejouer un fichier archivé."""
    from google.transit import gtfs_realtime_pb2 as pb

    if contenu is None:
        r = _HTTP.get(URL_GTFS_RT, timeout=25)
        if r.status_code != 200:
            raise SourceIndisponible(f"GTFS-RT HTTP {r.status_code}")
        contenu = r.content
    feed = pb.FeedMessage()
    feed.ParseFromString(contenu)
    filtre = uic_filtre if uic_filtre is not None else _uic_surveilles()
    ts_feed = int(feed.header.timestamp or time.time())
    out: dict = {}
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        m = re.search(r"OCESN0*(\d+)F", tu.trip.trip_id or "")
        if not m:
            continue
        numero = m.group(1)
        arrets = []
        for su in tu.stop_time_update:
            uic = uic_depuis_stop_id(su.stop_id)
            if not uic:
                continue
            arr = su.arrival if su.HasField("arrival") else None
            dep = su.departure if su.HasField("departure") else None
            ret_arr = round(arr.delay / 60) if arr is not None and arr.HasField("delay") else None
            ret_dep = round(dep.delay / 60) if dep is not None and dep.HasField("delay") else None
            t_arr = int(arr.time) if arr is not None and arr.HasField("time") and arr.time else None
            t_dep = int(dep.time) if dep is not None and dep.HasField("time") and dep.time else None
            plan_arr = t_arr - arr.delay if (t_arr and arr is not None and arr.HasField("delay")) else None
            plan_dep = t_dep - dep.delay if (t_dep and dep is not None and dep.HasField("delay")) else None
            arrets.append({
                "uic": uic, "nom": nom_gare(uic),
                "skipped": su.schedule_relationship == 1,
                "ret_arr": ret_arr, "ret_dep": ret_dep,
                "t_arr": t_arr, "t_dep": t_dep, "plan_arr": plan_arr, "plan_dep": plan_dep,
            })
        if not any(a["uic"] in filtre for a in arrets) and tu.trip.schedule_relationship == 0:
            continue
        etat = {
            "numero": numero, "date": date_iso(tu.trip.start_date), "sources": ["sncf_gtfs_rt"],
            "annule": tu.trip.schedule_relationship == 3,
            "ajoute": tu.trip.schedule_relationship == 1,
            "arrets": propager_retards(arrets), "ts": ts_feed, "trip_id": tu.trip.trip_id,
        }
        if etat["annule"] and not any(a["uic"] in filtre for a in arrets):
            continue
        cle = (etat["date"], numero)
        # En cas de doublon (train ajouté vs programmé) on garde le plus complet.
        if cle not in out or len(arrets) > len(out[cle]["arrets"]):
            out[cle] = etat
    return out


# --------------------------------------------------------------------------
# CFL / mobiliteit.lu (HAFAS)
# --------------------------------------------------------------------------
def _cle_cfl() -> str:
    return os.getenv("MOBILITEIT_API_KEY", "")


def _dt_cfl(date_s: str | None, heure_s: str | None, repli_date: str) -> int | None:
    if not heure_s:
        return None
    d = date_s or repli_date
    try:
        dt = datetime.strptime(f"{d} {heure_s[:8]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_paris)
        return int(dt.timestamp())
    except ValueError:
        return None


def _retard_min(plan: int | None, rt: int | None) -> int | None:
    if plan is None or rt is None:
        return None
    diff = rt - plan
    if diff < -6 * 3600:      # franchissement minuit mal daté
        diff += 86400
    return round(diff / 60)


def etats_depuis_board_cfl(donnees: dict) -> dict:
    """Convertit une réponse departureBoard?passlist=1 en {(date, numero): etat}."""
    out: dict = {}
    for x in donnees.get("Departure", []) or []:
        numero = numero_depuis_texte(x.get("name"))
        if not numero:
            continue
        date_j = x.get("date") or ""
        arrets = []
        for s in (x.get("Stops") or {}).get("Stop", []) or []:
            ext = str(s.get("extId") or "")
            uic = CFL_EXTID_UIC.get(ext) or bazo_geo.uic_pour_gare(
                re.sub(r",\s*Gare.*$", "", str(s.get("name") or "")), "grand_est"
            )
            if not uic:
                continue
            p_arr = _dt_cfl(s.get("arrDate"), s.get("arrTime"), date_j)
            r_arr = _dt_cfl(s.get("rtArrDate"), s.get("rtArrTime"), date_j) if s.get("rtArrTime") else None
            p_dep = _dt_cfl(s.get("depDate"), s.get("depTime"), date_j)
            r_dep = _dt_cfl(s.get("rtDepDate"), s.get("rtDepTime"), date_j) if s.get("rtDepTime") else None
            a_dep_seul = "depTime" in s and "arrTime" not in s
            a_arr_seul = "arrTime" in s and "depTime" not in s
            annule_arret = bool(s.get("cancelled")) or (
                bool(s.get("depCancelled")) and (a_dep_seul or bool(s.get("arrCancelled")))
            ) or (bool(s.get("arrCancelled")) and a_arr_seul)
            arrets.append({
                "uic": uic, "nom": nom_gare(uic), "skipped": annule_arret,
                "ret_arr": _retard_min(p_arr, r_arr), "ret_dep": _retard_min(p_dep, r_dep),
                "t_arr": r_arr or None, "t_dep": r_dep or None,
                "plan_arr": p_arr, "plan_dep": p_dep,
            })
        if not arrets:
            continue
        annule = bool(x.get("cancelled")) or str(x.get("JourneyStatus") or "") == "C" or all(a["skipped"] for a in arrets)
        etat = {
            "numero": numero, "date": date_j, "sources": ["cfl_mobiliteit"], "annule": annule,
            "ajoute": False, "arrets": propager_retards(arrets), "ts": int(time.time()),
        }
        out[(date_j, numero)] = etat
    return out


def lire_cfl_board(gare: str = "bettembourg", duree_min: int = 150, retro_min: int = 30,
                   max_journeys: int = 80, maintenant: datetime | None = None) -> dict:
    """Tableau de départs CFL (passlist) autour de `maintenant`. Lève SourceIndisponible si KO."""
    global _cfl_pause_jusqua
    cle = _cle_cfl()
    if not cle:
        raise SourceIndisponible("MOBILITEIT_API_KEY absente")
    if time.time() < _cfl_pause_jusqua:
        raise SourceIndisponible("CFL en pause (backoff)")
    maintenant = maintenant or datetime.now(tz_paris)
    debut = maintenant - timedelta(minutes=retro_min)
    params = {
        "accessId": cle, "id": CFL_GARES[gare], "format": "json", "passlist": 1,
        "date": debut.strftime("%Y-%m-%d"), "time": debut.strftime("%H:%M"),
        "duration": duree_min + retro_min, "maxJourneys": max_journeys,
    }
    try:
        r = _HTTP.get(f"{CFL_BASE}/departureBoard", params=params, timeout=20)
    except requests.RequestException as e:
        raise SourceIndisponible(f"CFL réseau : {e}") from e
    if r.status_code in (429, 503):
        _cfl_pause_jusqua = time.time() + 300
        raise SourceIndisponible(f"CFL HTTP {r.status_code} (pause 5 min)")
    if r.status_code != 200:
        raise SourceIndisponible(f"CFL HTTP {r.status_code}")
    try:
        return etats_depuis_board_cfl(r.json())
    except ValueError as e:
        raise SourceIndisponible(f"CFL JSON invalide : {e}") from e


# --------------------------------------------------------------------------
# Navitia (repli ciblé sur un train suivi)
# --------------------------------------------------------------------------
def _hms_vers_sec(s: str | None) -> int | None:
    if not s or len(s) < 6:
        return None
    try:
        return int(s[0:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6])
    except ValueError:
        return None


def etat_depuis_navitia_vj(vj_json: dict, numero: str, date_j: str) -> dict | None:
    """Construit un état depuis /vehicle_journeys?depth>=2 (horaire de base + perturbations)."""
    vjs = vj_json.get("vehicle_journeys") or []
    if not vjs:
        return None
    vj = vjs[0]
    jour0 = datetime.strptime(date_j, "%Y-%m-%d").replace(tzinfo=tz_paris)
    arrets = []
    for st in vj.get("stop_times", []):
        sp = st.get("stop_point") or {}
        sa = (sp.get("stop_area") or {}).get("id", "") or sp.get("id", "")
        uic = uic_depuis_stop_id(sa) or uic_depuis_stop_id(sp.get("id", ""))
        if not uic:
            continue
        arr_s = _hms_vers_sec(st.get("arrival_time"))
        dep_s = _hms_vers_sec(st.get("departure_time"))
        arrets.append({
            "uic": uic, "nom": nom_gare(uic), "skipped": False, "ret_arr": None, "ret_dep": None,
            "t_arr": None, "t_dep": None,
            "plan_arr": int(jour0.timestamp()) + arr_s if arr_s is not None else None,
            "plan_dep": int(jour0.timestamp()) + dep_s if dep_s is not None else None,
        })
    if not arrets:
        return None
    annule = False
    for d in vj_json.get("disruptions", []) or []:
        if str(d.get("status", "active")).lower() not in ("active", "future"):
            continue
        effet = str((d.get("severity") or {}).get("effect", "")).upper()
        objets = d.get("impacted_objects") or []
        touche_stops = False
        for o in objets:
            for isp in o.get("impacted_stops", []) or []:
                touche_stops = True
                sp = isp.get("stop_point") or {}
                sa = (sp.get("stop_area") or {}).get("id", "") or sp.get("id", "")
                uic = uic_depuis_stop_id(sa) or uic_depuis_stop_id(sp.get("id", ""))
                eff = str(isp.get("stop_time_effect", "")).lower()
                for a in arrets:
                    if a["uic"] != uic:
                        continue
                    if eff == "deleted" or str(isp.get("arrival_status", "")).lower() == "deleted" \
                            or str(isp.get("departure_status", "")).lower() == "deleted":
                        a["skipped"] = True
                    ba, aa = _hms_vers_sec(isp.get("base_arrival_time")), _hms_vers_sec(isp.get("amended_arrival_time"))
                    bd, ad = _hms_vers_sec(isp.get("base_departure_time")), _hms_vers_sec(isp.get("amended_departure_time"))
                    if ba is not None and aa is not None:
                        a["ret_arr"] = round((aa - ba) / 60)
                    if bd is not None and ad is not None:
                        a["ret_dep"] = round((ad - bd) / 60)
        if effet == "NO_SERVICE" and not touche_stops:
            annule = True
    if annule:
        for a in arrets:
            a["skipped"] = True
    return {
        "numero": numero, "date": date_j, "sources": ["sncf_navitia"], "annule": annule or all(a["skipped"] for a in arrets),
        "ajoute": False, "arrets": propager_retards(arrets), "ts": int(time.time()),
    }


def lire_navitia_train(numero: str, date_j: str, token: str | None = None) -> dict | None:
    """1 appel Navitia (quota compté). None si quota insuffisant ou train inconnu."""
    from bazo_config import TOKEN_SNCF
    from bazo_quota import RESERVE_CHECKIN, peut_appeler

    if not peut_appeler("temps_reel", reserve=RESERVE_CHECKIN):
        return None
    tok = token or TOKEN_SNCF
    d = date_j.replace("-", "")
    try:
        r = _HTTP.get(
            "https://api.sncf.com/v1/coverage/sncf/vehicle_journeys",
            params={"headsign": numero, "since": f"{d}T000000", "until": f"{d}T235959",
                    "data_freshness": "realtime", "depth": 3, "count": 3},
            auth=(tok, ""), timeout=20,
        )
        compter_appel_sncf("temps_reel")
    except requests.RequestException as e:
        logger.warning("[NAVITIA] vehicle_journeys %s : %s", numero, e)
        return None
    if r.status_code != 200:
        return None
    return etat_depuis_navitia_vj(r.json(), numero, date_j)


# --------------------------------------------------------------------------
# Fusion multi-sources
# --------------------------------------------------------------------------
def fusionner_etats(etats: list[dict]) -> dict:
    """Fusionne plusieurs états du même train. Règle : on garde l'information la plus défavorable
    à l'usager et on indique le nombre de sources concordantes."""
    etats = [e for e in etats if e]
    if not etats:
        raise ValueError("aucun état à fusionner")
    base = max(etats, key=lambda e: len(e.get("arrets", [])))
    arrets = []
    for a in base["arrets"]:
        fusion = dict(a)
        for e in etats:
            if e is base:
                continue
            for b in e.get("arrets", []):
                if b["uic"] != a["uic"]:
                    continue
                fusion["skipped"] = fusion["skipped"] or b["skipped"]
                for champ in ("ret_arr", "ret_dep"):
                    vals = [v for v in (fusion.get(champ), b.get(champ)) if v is not None]
                    fusion[champ] = max(vals) if vals else None
                for champ in ("t_arr", "t_dep", "plan_arr", "plan_dep"):
                    if fusion.get(champ) is None and b.get(champ) is not None:
                        fusion[champ] = b[champ]
                    elif champ.startswith("t_") and b.get(champ) and fusion.get(champ):
                        fusion[champ] = max(fusion[champ], b[champ])
        arrets.append(fusion)
    sources = sorted({s for e in etats for s in e.get("sources", [])})
    n_annule = sum(1 for e in etats if e.get("annule"))
    return {
        "numero": base["numero"], "date": base["date"], "sources": sources,
        "annule": n_annule > 0, "n_sources_annule": n_annule,
        "n_sources_partiel": sum(1 for e in etats if arrets_supprimes_fin(e)),
        "ajoute": any(e.get("ajoute") for e in etats),
        "arrets": arrets, "ts": max(e.get("ts", 0) for e in etats),
        "trip_id": base.get("trip_id"),
    }


def fusionner_sources(*dicts_etats: dict) -> dict:
    """Fusionne plusieurs {(date, numero): etat} en un seul."""
    cles: set = set()
    for d in dicts_etats:
        cles.update(d.keys())
    return {c: fusionner_etats([d[c] for d in dicts_etats if c in d]) for c in cles}
