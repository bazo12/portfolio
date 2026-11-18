"""GTFS static — validation UIC gares (Phase 3 M13)."""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger("bazo.gtfs")


def charger_stops_depuis_zip(chemin_zip: str | Path) -> dict[str, str]:
    """Lit stops.txt d'une archive GTFS → {stop_id: stop_name}."""
    chemin = Path(chemin_zip)
    if not chemin.is_file():
        raise FileNotFoundError(chemin)
    with zipfile.ZipFile(chemin) as zf:
        nom = next((n for n in zf.namelist() if n.endswith("stops.txt")), None)
        if not nom:
            raise ValueError("stops.txt absent du GTFS")
        raw = zf.read(nom).decode("utf-8-sig", errors="replace")
    return charger_stops_depuis_csv(raw)


def charger_stops_depuis_csv(contenu: str) -> dict[str, str]:
    reader = csv.DictReader(io.StringIO(contenu))
    out: dict[str, str] = {}
    for row in reader:
        sid = (row.get("stop_id") or "").strip()
        name = (row.get("stop_name") or "").strip()
        if sid and name:
            out[sid] = name
    return out


def uic_depuis_stop_id(stop_id: str) -> str | None:
    """Extrait UIC SNCF depuis un stop_id GTFS (StopArea:OCE87611004 ou 87611004)."""
    if not stop_id:
        return None
    sid = stop_id.split(":")[-1]
    if sid.startswith("OCE") and len(sid) >= 11:
        return sid[3:11]
    if sid.isdigit() and len(sid) == 8:
        return sid
    return None


def valider_uic_dans_gtfs(uic: str, stops: dict[str, str]) -> bool:
    """True si l'UIC apparaît dans le GTFS (stop_id ou parent_station)."""
    if not uic:
        return False
    for sid in stops:
        if uic_depuis_stop_id(sid) == uic:
            return True
    return False


def rapport_uic_geo_vs_gtfs(stops: dict[str, str], region: str | None = None) -> dict:
    """Compare les UIC radar d'une région avec un GTFS local."""
    from bazo_geo import REGIONS, trajets_radar, uic_pour_gare

    rid = region or "grand_est"
    if rid not in REGIONS:
        raise KeyError(rid)
    ok, manquants = [], []
    for t in trajets_radar(rid):
        for cle in ("uic_depart", "uic_arrivee"):
            uic = t[cle]
            if valider_uic_dans_gtfs(uic, stops):
                ok.append(uic)
            else:
                manquants.append({"axe": t["nom"], "uic": uic, "gare": t.get("gare_depart")})
    return {
        "region": rid,
        "gtfs_stops": len(stops),
        "uic_ok": len(set(ok)),
        "uic_manquants": manquants,
    }
