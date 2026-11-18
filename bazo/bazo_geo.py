"""
Géographie TER Bazo — gares UIC, axes, régions extensibles (M2 S3).
Source unique pour bot, radar, GTFS et API mobile.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bazo.geo")

DEFAULT_REGION = "grand_est"
NAVITIA_COVERAGE = "sncf"

# --- Région Grand Est (Sillon Lorrain + extensions locales) ---
# Seul l'axe Nancy → Luxembourg est suivi. Les branches Forbach / Sarrebruck,
# Sarrebourg, Strasbourg, Épinal ne partagent que des gares (Metz, Nancy) :
# les garder faisait notifier un usager Metz → Luxembourg pour un train vers Sarrebruck.
_AXES_GRAND_EST = {
    "sillon": [
        "nancy", "frouard", "dieulouard", "pont-à-mousson", "pagny-sur-moselle", "novéant",
        "ars-sur-moselle", "metz", "metz nord", "woippy", "maizières-lès-metz", "hagondange",
        "uckange", "thionville", "hettange-grande", "bettembourg", "howald", "luxembourg",
    ],
}

# Clé normalisée → UIC stop_area (identifiants vérifiés sur l'API SNCF le 2026-11-04 ;
# plusieurs anciens identifiants pointaient sur de mauvaises gares ou n'existaient pas).
_UIC_ALIASES_GRAND_EST = {
    "metz": "87192039", "metz ville": "87192039", "luxembourg": "82001000",
    "nancy": "87141002", "thionville": "87191007", "forbach": "87193003",
    "epinal": "87144006", "épinal": "87144006", "woippy": "87192088", "hagondange": "87191114",
    "bettembourg": "82006030", "uckange": "87191130", "howald": "82002501",
    "metz nord": "87192070",
    "frouard": "87141077", "pont a mousson": "87141820", "pont-à-mousson": "87141820",
    "pagny": "87192468", "pagny-sur-moselle": "87192468",
    "ars sur moselle": "87192401", "ars-sur-moselle": "87192401",
    "novéant": "87192427", "noveant": "87192427",
    "maizieres": "87191106", "maizières-lès-metz": "87191106",
    "hettange": "87191163", "hettange-grande": "87191163",
    "dieulouard": "87141812", "champigneulles": "87141085", "bayon": "87141218",
    "charmes": "87144055", "remilly": "87192237", "rémilly": "87192237",
    "saint avold": "87193318", "saint-avold": "87193318",
    "béning": "87193250", "hombourg-haut": "87193276", "faulquemont": "87193359",
    "herny": "87193383", "sanry-sur-nied": "87192229", "courcelles-sur-nied": "87192211",
    "peltre": "87192203", "thaon": "87144097", "châtel-nomexy": "87144071",
    "blainville-damelevières": "87141135",
    "strasbourg": "87212027", "sarrebourg": "87215012",
    "sarreguemines": "87193615", "remiremont": "87144451",
    "saint-die": "87144014", "saint die": "87144014",
    "lunéville": "87141150", "luneville": "87141150",
    "belfort": "87184002",
}

# UIC → libellé affichage
_UIC_LABELS_GRAND_EST = {
    "87192039": "Metz", "82001000": "Luxembourg", "87141002": "Nancy",
    "87191007": "Thionville", "87193003": "Forbach", "87144006": "Épinal",
    "87192088": "Woippy", "87191114": "Hagondange", "82006030": "Bettembourg",
    "82002501": "Howald", "87191130": "Uckange", "87141077": "Frouard",
    "87141820": "Pont-à-Mousson", "87192468": "Pagny", "87192401": "Ars-sur-Moselle",
    "87191106": "Maizières", "87191163": "Hettange", "87141812": "Dieulouard",
    "87141085": "Champigneulles", "87141218": "Bayon", "87144055": "Charmes",
    "87192237": "Rémilly", "87193318": "Saint-Avold", "87212027": "Strasbourg",
    "87215012": "Sarrebourg", "87193615": "Sarreguemines", "87144451": "Remiremont",
    "87144014": "Saint-Dié", "87141150": "Lunéville", "87184002": "Belfort",
    "87192070": "Metz Nord", "87192427": "Novéant", "87193250": "Béning",
    "87193276": "Hombourg-Haut", "87193359": "Faulquemont", "87193383": "Herny",
    "87192229": "Sanry-sur-Nied", "87192211": "Courcelles-sur-Nied", "87192203": "Peltre",
    "87144097": "Thaon", "87144071": "Châtel-Nomexy", "87141135": "Blainville-Damelevières",
}

_TERMINUS_GRAND_EST = {
    "87212027": "Strasbourg", "87113001": "Paris-Est", "87547000": "Paris-Est",
    "87215012": "Sarrebourg", "87193615": "Sarreguemines", "87193250": "Béning",
    "87144451": "Remiremont", "87144014": "Saint-Dié",
}

# Paires principales pour le radar Navitia (nom affiché, gare A, gare B)
# Trains qui empruntent le Sillon, y compris les missions courtes
# (Metz–Thionville, Thionville–Metz, Nancy–Thionville). Pas les branches.
_RADAR_PAIRES_GRAND_EST = (
    ("Metz ➔ Luxembourg", "Metz", "Luxembourg"),
    ("Luxembourg ➔ Metz", "Luxembourg", "Metz"),
    ("Nancy ➔ Luxembourg", "Nancy", "Luxembourg"),
    ("Luxembourg ➔ Nancy", "Luxembourg", "Nancy"),
    ("Nancy ➔ Metz", "Nancy", "Metz"),
    ("Metz ➔ Nancy", "Metz", "Nancy"),
    ("Thionville ➔ Luxembourg", "Thionville", "Luxembourg"),
    ("Luxembourg ➔ Thionville", "Luxembourg", "Thionville"),
    ("Thionville ➔ Metz", "Thionville", "Metz"),
    ("Metz ➔ Thionville", "Metz", "Thionville"),
    ("Nancy ➔ Thionville", "Nancy", "Thionville"),
    ("Thionville ➔ Nancy", "Thionville", "Nancy"),
)

# --- Île-de-France (preview M9 — radar inactif par défaut) ---
_AXES_IDF = {
    "transilien_h": [
        "paris est", "noisy-le-grand", "tournan", "longueville", "provins",
    ],
    "melun": [
        "paris est", "vincennes", "melun", "montereau-fault-yonne",
    ],
    "saint_lazare": [
        "paris saint-lazare", "versailles-chantiers", "mantes-la-jolie",
    ],
    "nord": [
        "paris nord", "pontoise", "persan-beaumont",
    ],
}

_UIC_ALIASES_IDF = {
    "paris est": "87113001", "melun": "87651006", "montereau": "87683009",
    "montereau-fault-yonne": "87683009", "provins": "87113068",
    "paris saint-lazare": "87384008", "paris st lazare": "87384008",
    "versailles-chantiers": "87393009", "versailles": "87393009",
    "mantes-la-jolie": "87384000", "mantes": "87384000",
    "paris nord": "87271007", "pontoise": "87364016",
    "persan-beaumont": "87364025", "noisy-le-grand": "87113042",
    "tournan": "87113076", "longueville": "87113084", "vincennes": "87113034",
}

_UIC_LABELS_IDF = {
    "87113001": "Paris Est", "87651006": "Melun", "87683009": "Montereau",
    "87113068": "Provins", "87384008": "Paris Saint-Lazare",
    "87393009": "Versailles-Chantiers", "87384000": "Mantes-la-Jolie",
    "87271007": "Paris Nord", "87364016": "Pontoise", "87364025": "Persan-Beaumont",
    "87113042": "Noisy-le-Grand", "87113076": "Tournan", "87113084": "Longueville",
    "87113034": "Vincennes",
}

_RADAR_PAIRES_IDF = (
    ("Paris Est ➔ Melun", "Paris Est", "Melun"),
    ("Melun ➔ Paris Est", "Melun", "Paris Est"),
    ("Paris Est ➔ Provins", "Paris Est", "Provins"),
    ("Paris Saint-Lazare ➔ Versailles", "Paris Saint-Lazare", "Versailles-Chantiers"),
    ("Paris Nord ➔ Pontoise", "Paris Nord", "Pontoise"),
)

# --- Auvergne-Rhône-Alpes (preview M9) ---
_AXES_ARA = {
    "lyon_grenoble": [
        "lyon part-dieu", "bourgoin-jallieu", "grenoble",
    ],
    "lyon_chambery": [
        "lyon part-dieu", "amberieu", "chambery",
    ],
    "lyon_annecy": [
        "lyon part-dieu", "amberieu", "annecy",
    ],
    "lyon_st_etienne": [
        "lyon part-dieu", "givors", "saint-etienne chateaucreux",
    ],
}

_UIC_ALIASES_ARA = {
    "lyon part-dieu": "87723197", "lyon part dieu": "87723197",
    "lyon perrache": "87722025", "grenoble": "87747006",
    "chambery": "87741006", "chambéry": "87741006",
    "annecy": "87746008", "amberieu": "87743005", "ambérieu": "87743005",
    "bourgoin-jallieu": "87725703", "givors": "87722400",
    "saint-etienne chateaucreux": "87726009", "saint-étienne": "87726009",
    "valence": "87761007", "dijon": "87713040",
}

_UIC_LABELS_ARA = {
    "87723197": "Lyon Part-Dieu", "87722025": "Lyon Perrache",
    "87747006": "Grenoble", "87741006": "Chambéry",
    "87746008": "Annecy", "87743005": "Ambérieu",
    "87725703": "Bourgoin-Jallieu", "87722400": "Givors",
    "87726009": "Saint-Étienne Châteaucreux", "87761007": "Valence",
    "87713040": "Dijon",
}

_RADAR_PAIRES_ARA = (
    ("Lyon ➔ Grenoble", "Lyon Part-Dieu", "Grenoble"),
    ("Grenoble ➔ Lyon", "Grenoble", "Lyon Part-Dieu"),
    ("Lyon ➔ Chambéry", "Lyon Part-Dieu", "Chambéry"),
    ("Lyon ➔ Annecy", "Lyon Part-Dieu", "Annecy"),
    ("Lyon ➔ Saint-Étienne", "Lyon Part-Dieu", "Saint-Étienne Châteaucreux"),
)

# --- Hauts-de-France (preview M12) ---
_AXES_HDF = {
    "lille_paris": ["lille flandres", "paris nord"],
    "lille_calais": ["lille flandres", "calais ville"],
    "amiens": ["amiens", "paris nord"],
}

_UIC_ALIASES_HDF = {
    "lille flandres": "87286005", "lille": "87286005",
    "paris nord": "87271007", "calais": "87317001", "calais ville": "87317001",
    "amiens": "87313874",
}

_UIC_LABELS_HDF = {
    "87286005": "Lille Flandres", "87271007": "Paris Nord",
    "87317001": "Calais-Ville", "87313874": "Amiens",
}

_RADAR_PAIRES_HDF = (
    ("Lille ➔ Paris Nord", "Lille Flandres", "Paris Nord"),
    ("Paris Nord ➔ Lille", "Paris Nord", "Lille Flandres"),
    ("Lille ➔ Calais", "Lille Flandres", "Calais-Ville"),
)

# --- Occitanie (preview M12) ---
_AXES_OCC = {
    "toulouse_montpellier": ["toulouse matabiau", "montpellier saint-roch"],
    "toulouse_nimes": ["toulouse matabiau", "nimes"],
}

_UIC_ALIASES_OCC = {
    "toulouse matabiau": "87611004", "toulouse": "87611004",
    "montpellier saint-roch": "87773002", "montpellier": "87773002",
    "nimes": "87775007", "nîmes": "87775007",
}

_UIC_LABELS_OCC = {
    "87611004": "Toulouse Matabiau", "87773002": "Montpellier Saint-Roch",
    "87775007": "Nîmes",
}

_RADAR_PAIRES_OCC = (
    ("Toulouse ➔ Montpellier", "Toulouse Matabiau", "Montpellier Saint-Roch"),
    ("Montpellier ➔ Toulouse", "Montpellier Saint-Roch", "Toulouse Matabiau"),
    ("Toulouse ➔ Nîmes", "Toulouse Matabiau", "Nîmes"),
)

# --- Nouvelle-Aquitaine (preview M12) ---
_AXES_NAQ = {
    "bordeaux_arcachon": ["bordeaux saint-jean", "arcachon"],
    "bordeaux_pau": ["bordeaux saint-jean", "pau"],
}

_UIC_ALIASES_NAQ = {
    "bordeaux saint-jean": "87581009", "bordeaux": "87581009",
    "arcachon": "87582478", "pau": "87671108",
}

_UIC_LABELS_NAQ = {
    "87581009": "Bordeaux Saint-Jean", "87582478": "Arcachon", "87671108": "Pau",
}

_RADAR_PAIRES_NAQ = (
    ("Bordeaux ➔ Arcachon", "Bordeaux Saint-Jean", "Arcachon"),
    ("Arcachon ➔ Bordeaux", "Arcachon", "Bordeaux Saint-Jean"),
    ("Bordeaux ➔ Pau", "Bordeaux Saint-Jean", "Pau"),
)

# --- Bretagne (preview Phase 3) ---
_AXES_BRE = {
    "rennes_brest": ["rennes", "brest"],
    "rennes_quimper": ["rennes", "quimper"],
}

_UIC_ALIASES_BRE = {
    "rennes": "87471003", "brest": "87474007", "quimper": "87474098",
}

_UIC_LABELS_BRE = {
    "87471003": "Rennes", "87474007": "Brest", "87474098": "Quimper",
}

_RADAR_PAIRES_BRE = (
    ("Rennes ➔ Brest", "Rennes", "Brest"),
    ("Brest ➔ Rennes", "Brest", "Rennes"),
)

# --- PACA (preview Phase 3) ---
_AXES_PACA = {
    "marseille_nice": ["marseille saint-charles", "nice"],
    "marseille_toulon": ["marseille saint-charles", "toulon"],
}

_UIC_ALIASES_PACA = {
    "marseille saint-charles": "87751008", "marseille": "87751008",
    "nice": "87756056", "toulon": "87755009",
}

_UIC_LABELS_PACA = {
    "87751008": "Marseille Saint-Charles", "87756056": "Nice", "87755009": "Toulon",
}

_RADAR_PAIRES_PACA = (
    ("Marseille ➔ Nice", "Marseille Saint-Charles", "Nice"),
    ("Marseille ➔ Toulon", "Marseille Saint-Charles", "Toulon"),
)

# --- Normandie (preview Phase 3) ---
_AXES_NOR = {
    "rouen_caen": ["rouen rive droite", "caen"],
    "rouen_havre": ["rouen rive droite", "le havre"],
}

_UIC_ALIASES_NOR = {
    "rouen rive droite": "87411017", "rouen": "87411017",
    "caen": "87413001", "le havre": "87413013",
}

_UIC_LABELS_NOR = {
    "87411017": "Rouen Rive Droite", "87413001": "Caen", "87413013": "Le Havre",
}

_RADAR_PAIRES_NOR = (
    ("Rouen ➔ Caen", "Rouen Rive Droite", "Caen"),
    ("Rouen ➔ Le Havre", "Rouen Rive Droite", "Le Havre"),
)

# --- Bourgogne-Franche-Comté (preview Phase 3) ---
_AXES_BFC = {
    "dijon_besancon": ["dijon", "besancon viotte"],
}

_UIC_ALIASES_BFC = {
    "dijon": "87713040", "besancon viotte": "87718000", "besançon": "87718000",
}

_UIC_LABELS_BFC = {
    "87713040": "Dijon", "87718000": "Besançon Viotte",
}

_RADAR_PAIRES_BFC = (
    ("Dijon ➔ Besançon", "Dijon", "Besançon Viotte"),
    ("Besançon ➔ Dijon", "Besançon Viotte", "Dijon"),
)

# --- Centre-Val de Loire (preview Phase 3) ---
_AXES_CVL = {
    "tours_orleans": ["tours", "orleans"],
    "orleans_blois": ["orleans", "blois chambord"],
}

_UIC_ALIASES_CVL = {
    "tours": "87571000", "orleans": "87574007", "orléans": "87574007",
    "blois": "87575009", "blois chambord": "87575009",
}

_UIC_LABELS_CVL = {
    "87571000": "Tours", "87574007": "Orléans", "87575009": "Blois - Chambord",
}

_RADAR_PAIRES_CVL = (
    ("Tours ➔ Orléans", "Tours", "Orléans"),
    ("Orléans ➔ Blois", "Orléans", "Blois - Chambord"),
)

# --- Pays de la Loire (preview Phase 3) ---
_AXES_PDL = {
    "nantes_angers": ["nantes", "angers saint-laud"],
    "le_mans_nantes": ["le mans", "nantes"],
}

_UIC_ALIASES_PDL = {
    "nantes": "87481002", "angers": "87484009", "angers saint-laud": "87484009",
    "le mans": "87396002",
}

_UIC_LABELS_PDL = {
    "87481002": "Nantes", "87484009": "Angers Saint-Laud", "87396002": "Le Mans",
}

_RADAR_PAIRES_PDL = (
    ("Nantes ➔ Angers", "Nantes", "Angers Saint-Laud"),
    ("Le Mans ➔ Nantes", "Le Mans", "Nantes"),
)

# --- Corse (preview Phase 3) ---
_AXES_COR = {
    "ajaccio_bastia": ["ajaccio", "bastia"],
}

_UIC_ALIASES_COR = {
    "ajaccio": "87750107", "bastia": "87751205",
}

_UIC_LABELS_COR = {
    "87750107": "Ajaccio", "87751205": "Bastia",
}

_RADAR_PAIRES_COR = (
    ("Ajaccio ➔ Bastia", "Ajaccio", "Bastia"),
    ("Bastia ➔ Ajaccio", "Bastia", "Ajaccio"),
)

REGIONS: dict[str, dict[str, Any]] = {
    "grand_est": {
        "id": "grand_est",
        "label": "Grand Est (TER)",
        "coverage": NAVITIA_COVERAGE,
        "axes": _AXES_GRAND_EST,
        "uic_aliases": _UIC_ALIASES_GRAND_EST,
        "uic_labels": _UIC_LABELS_GRAND_EST,
        "terminus": _TERMINUS_GRAND_EST,
        "radar_paires": _RADAR_PAIRES_GRAND_EST,
        "active": True,
    },
    "ile_de_france": {
        "id": "ile_de_france",
        "label": "Île-de-France (TER)",
        "coverage": "fr-idf",
        "axes": _AXES_IDF,
        "uic_aliases": _UIC_ALIASES_IDF,
        "uic_labels": _UIC_LABELS_IDF,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_IDF,
        "active": False,
    },
    "auvergne_rhone_alpes": {
        "id": "auvergne_rhone_alpes",
        "label": "Auvergne-Rhône-Alpes (TER)",
        "coverage": "fr-sw",
        "axes": _AXES_ARA,
        "uic_aliases": _UIC_ALIASES_ARA,
        "uic_labels": _UIC_LABELS_ARA,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_ARA,
        "active": False,
    },
    "hauts_de_france": {
        "id": "hauts_de_france",
        "label": "Hauts-de-France (TER)",
        "coverage": "fr-nord",
        "axes": _AXES_HDF,
        "uic_aliases": _UIC_ALIASES_HDF,
        "uic_labels": _UIC_LABELS_HDF,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_HDF,
        "active": False,
    },
    "occitanie": {
        "id": "occitanie",
        "label": "Occitanie (TER)",
        "coverage": "fr-se",
        "axes": _AXES_OCC,
        "uic_aliases": _UIC_ALIASES_OCC,
        "uic_labels": _UIC_LABELS_OCC,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_OCC,
        "active": False,
    },
    "nouvelle_aquitaine": {
        "id": "nouvelle_aquitaine",
        "label": "Nouvelle-Aquitaine (TER)",
        "coverage": "fr-sw",
        "axes": _AXES_NAQ,
        "uic_aliases": _UIC_ALIASES_NAQ,
        "uic_labels": _UIC_LABELS_NAQ,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_NAQ,
        "active": False,
    },
    "bretagne": {
        "id": "bretagne",
        "label": "Bretagne (TER)",
        "coverage": "fr-bre",
        "axes": _AXES_BRE,
        "uic_aliases": _UIC_ALIASES_BRE,
        "uic_labels": _UIC_LABELS_BRE,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_BRE,
        "active": False,
    },
    "provence_alpes_cote_azur": {
        "id": "provence_alpes_cote_azur",
        "label": "PACA (TER)",
        "coverage": "fr-se",
        "axes": _AXES_PACA,
        "uic_aliases": _UIC_ALIASES_PACA,
        "uic_labels": _UIC_LABELS_PACA,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_PACA,
        "active": False,
    },
    "normandie": {
        "id": "normandie",
        "label": "Normandie (TER)",
        "coverage": "fr-nw",
        "axes": _AXES_NOR,
        "uic_aliases": _UIC_ALIASES_NOR,
        "uic_labels": _UIC_LABELS_NOR,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_NOR,
        "active": False,
    },
    "bourgogne_franche_comte": {
        "id": "bourgogne_franche_comte",
        "label": "Bourgogne-Franche-Comté (TER)",
        "coverage": "fr-sw",
        "axes": _AXES_BFC,
        "uic_aliases": _UIC_ALIASES_BFC,
        "uic_labels": _UIC_LABELS_BFC,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_BFC,
        "active": False,
    },
    "centre_val_de_loire": {
        "id": "centre_val_de_loire",
        "label": "Centre-Val de Loire (TER)",
        "coverage": "fr-cvl",
        "axes": _AXES_CVL,
        "uic_aliases": _UIC_ALIASES_CVL,
        "uic_labels": _UIC_LABELS_CVL,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_CVL,
        "active": False,
    },
    "pays_de_la_loire": {
        "id": "pays_de_la_loire",
        "label": "Pays de la Loire (TER)",
        "coverage": "fr-pdl",
        "axes": _AXES_PDL,
        "uic_aliases": _UIC_ALIASES_PDL,
        "uic_labels": _UIC_LABELS_PDL,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_PDL,
        "active": False,
    },
    "corse": {
        "id": "corse",
        "label": "Corse (TER)",
        "coverage": "fr-cors",
        "axes": _AXES_COR,
        "uic_aliases": _UIC_ALIASES_COR,
        "uic_labels": _UIC_LABELS_COR,
        "terminus": {},
        "radar_paires": _RADAR_PAIRES_COR,
        "active": False,
    },
}

# Rétrocompatibilité imports existants
AXES = _AXES_GRAND_EST
NOM_GARES = _UIC_LABELS_GRAND_EST
NOM_TERMINUS = _TERMINUS_GRAND_EST


def normaliser_cle_gare(nom: str) -> str:
    return (
        str(nom or "")
        .lower()
        .replace("-", " ")
        .replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("à", "a")
        .replace("ô", "o")
        .replace("ù", "u")
        .strip()
    )


def _region_cfg(region: str | None = None) -> dict:
    rid = region or DEFAULT_REGION
    cfg = REGIONS.get(rid)
    if not cfg:
        raise KeyError(f"Région inconnue : {rid}")
    return cfg


def liste_regions(actives_seulement: bool = False) -> list[dict]:
    out = []
    for cfg in REGIONS.values():
        if actives_seulement and not cfg.get("active"):
            continue
        out.append({
            "id": cfg["id"],
            "label": cfg["label"],
            "coverage": cfg["coverage"],
            "active": bool(cfg.get("active")),
            "nb_axes": len(cfg.get("axes") or {}),
            "nb_gares": len(toutes_les_gares(cfg["id"])),
            "nb_radar": len(cfg.get("radar_paires") or ()),
        })
    return out


def axes(region: str | None = None) -> dict[str, list[str]]:
    return _region_cfg(region)["axes"]


def toutes_les_gares(region: str | None = None) -> list[str]:
    seen: list[str] = []
    for liste in axes(region).values():
        for gare in liste:
            titre = gare.title()
            if titre not in seen:
                seen.append(titre)
    return seen


TOUTES_LES_GARES = toutes_les_gares()


def _meilleur_index(nom_gare: str, gares: list[str]) -> int:
    """Correspondance de gare : égalité, puis le libellé le plus précis contenu dans le nom."""
    n = normaliser_cle_gare(nom_gare)
    if not n:
        return -1
    contenu_i, contenu_len = -1, -1
    contenant_i, contenant_len = -1, 10**9
    for i, g in enumerate(gares):
        g_norm = g.replace("-", " ")
        if g_norm == n:
            return i
        if g_norm in n and len(g_norm) > contenu_len:
            contenu_i, contenu_len = i, len(g_norm)
        elif n in g_norm and len(g_norm) < contenant_len:
            contenant_i, contenant_len = i, len(g_norm)
    if contenu_i != -1:
        return contenu_i
    return contenant_i


def get_index_gare(nom_gare: str, region: str | None = None) -> int:
    nom_gare = normaliser_cle_gare(nom_gare)
    best_i, best_len = -1, -1
    for gares in axes(region).values():
        i = _meilleur_index(nom_gare, gares)
        if i == -1:
            continue
        g_norm = gares[i].replace("-", " ")
        score = len(g_norm) + (1000 if g_norm == nom_gare else 0)
        if score > best_len:
            best_i, best_len = i, score
    return best_i


def indices_sur_axe(
    gare_a: str, gare_b: str, region: str | None = None
) -> tuple[list[str], int, int] | None:
    """Retourne (liste_gares, idx_a, idx_b) si les deux gares sont sur le même axe."""
    if not gare_a or not gare_b:
        return None
    for gares in axes(region).values():
        idx_a = _meilleur_index(gare_a, gares)
        idx_b = _meilleur_index(gare_b, gares)
        if idx_a != -1 and idx_b != -1:
            return list(gares), idx_a, idx_b
    return None


def index_gare_sur_liste(nom_gare: str, gares: list[str]) -> int:
    return _meilleur_index(nom_gare, gares)


def incident_sur_segment(
    gare_incident: str,
    gare_dep_profil: str,
    gare_arr_profil: str,
    region: str | None = None,
) -> bool:
    """True si l'incident est sur le segment parcouru (même axe, entre départ et arrivée)."""
    resolved = indices_sur_axe(gare_dep_profil, gare_arr_profil, region)
    if not resolved:
        return False
    axe, idx_dep, idx_arr = resolved
    idx_inc = index_gare_sur_liste(gare_incident, axe)
    if idx_inc == -1:
        return False
    if idx_dep == idx_arr:
        return idx_inc == idx_dep
    if idx_arr > idx_dep:
        return idx_dep <= idx_inc <= idx_arr
    return idx_dep >= idx_inc >= idx_arr


def gares_equivalentes(nom_a: str, nom_b: str, region: str | None = None) -> bool:
    if not nom_a or not nom_b:
        return False
    ua, ub = uic_pour_gare(nom_a, region), uic_pour_gare(nom_b, region)
    if ua and ub:
        return ua == ub
    na, nb = normaliser_cle_gare(nom_a), normaliser_cle_gare(nom_b)
    return na in nb or nb in na


def _nettoyer_nom_gare(nom: str) -> str:
    """Nettoyage d'affichage : espaces, retours à la ligne, suffixe entre parenthèses."""
    n = str(nom or "").replace("\n", " ").replace("\r", " ").strip()
    if "(" in n:
        n = n.split("(", 1)[0].strip()
    return " ".join(n.split())


def nom_canonique_gare(nom: str, region: str | None = None) -> str:
    """
    Libellé canonique d'une gare pour l'affichage PDF / API (règle R7).
    « Metz Ville » -> « Metz », « Luxembourg (Luxembourg) » -> « Luxembourg ».
    Correspondance d'alias exacte uniquement : « Metz Nord » reste « Metz Nord ».
    """
    n = _nettoyer_nom_gare(nom)
    if not n:
        return ""
    if "luxemb" in n.lower():
        return "Luxembourg"
    cfg = _region_cfg(region)
    cle = normaliser_cle_gare(n)
    for alias, uic in cfg["uic_aliases"].items():
        if normaliser_cle_gare(alias) == cle:
            label = cfg["uic_labels"].get(uic)
            if label:
                return label
    return n


def gare_sur_segment(
    gare: str,
    gare_depart: str | None,
    gare_arrivee: str | None,
    region: str | None = None,
) -> bool:
    """
    True si `gare` est la gare de départ, la gare d'arrivée, ou une gare intermédiaire
    du segment usager (règle R4). Sans aucune gare de référence : False.
    """
    if not gare:
        return False
    if gare_depart and gares_equivalentes(gare, gare_depart, region):
        return True
    if gare_arrivee and gares_equivalentes(gare, gare_arrivee, region):
        return True
    if gare_depart and gare_arrivee:
        return incident_sur_segment(gare, gare_depart, gare_arrivee, region)
    return False


def _axe_sillon() -> list[str]:
    return list(_AXES_GRAND_EST["sillon"])


def terminus_corridor(direction: str, dernier_arret: str | None = None) -> str | None:
    """
    Destination à afficher et à filtrer : une gare du Sillon Nancy–Luxembourg.
    « Sarrebruck », « Forbach », « Strasbourg » ne sont pas sur cet axe : on retombe
    alors sur le dernier arrêt du trajet qui est encore sur le Sillon (souvent Metz),
    ou on écarte le train s'il ne dessert aucune gare du corridor.
    """
    axe = _axe_sillon()
    if index_gare_sur_liste(direction, axe) >= 0:
        return _nettoyer_nom_gare(direction) or direction
    if dernier_arret and index_gare_sur_liste(dernier_arret, axe) >= 0:
        return _nettoyer_nom_gare(dernier_arret) or dernier_arret
    return None


def train_atteint_arrivee(
    destination: str,
    gare_incident: str,
    gare_montee: str,
    gare_descente: str,
    axe: list[str],
) -> bool:
    """
    True si la direction du train est sur le même axe et va au moins jusqu'à la
    gare de descente de l'usager (sens compris). Un train Metz → Thionville ne
    concerne pas un usager Metz → Luxembourg ; un train Metz → Nancy non plus.
    """
    idx_dest = index_gare_sur_liste(destination, axe)
    idx_inc = index_gare_sur_liste(gare_incident, axe)
    idx_board = index_gare_sur_liste(gare_montee, axe)
    idx_arr = index_gare_sur_liste(gare_descente, axe)
    if min(idx_dest, idx_inc, idx_board, idx_arr) < 0:
        return False
    if idx_board == idx_arr:
        return idx_dest == idx_board and idx_inc == idx_board
    direction = 1 if idx_arr > idx_board else -1
    if (idx_dest - idx_inc) * direction < 0:
        return False
    if idx_dest == idx_inc and idx_inc != idx_arr:
        return False
    return (idx_dest - idx_arr) * direction >= 0


def calculer_sens(gare_dep: str, gare_arr: str, region: str | None = None) -> int:
    if not gare_dep or not gare_arr:
        return 0
    for gares in axes(region).values():
        idx1 = _meilleur_index(gare_dep, gares)
        idx2 = _meilleur_index(gare_arr, gares)
        if idx1 != -1 and idx2 != -1:
            if idx1 == idx2:
                return 0
            return 1 if idx2 > idx1 else -1
    return 0


def uic_pour_gare(nom_gare: str, region: str | None = None) -> str | None:
    """Résolution UIC locale (sans appel API)."""
    cfg = _region_cfg(region)
    aliases: dict[str, str] = cfg["uic_aliases"]
    n = normaliser_cle_gare(nom_gare)
    for cle, sid in aliases.items():
        cle_n = normaliser_cle_gare(cle)
        if cle_n in n or n in cle_n:
            return sid
    for _nom_axe, gares in cfg["axes"].items():
        for g in gares:
            g_clean = g.replace("-", " ")
            if g_clean in n or n in g_clean:
                for cle, sid in aliases.items():
                    cle_n = normaliser_cle_gare(cle)
                    if cle_n in g_clean or g_clean in cle_n:
                        return sid
    return None


def stop_area_pour_gare(nom_gare: str, region: str | None = None) -> str | None:
    return uic_pour_gare(nom_gare, region)


def nom_pour_uic(uic: str, region: str | None = None) -> str | None:
    if not uic:
        return None
    cfg = _region_cfg(region)
    if uic in cfg["uic_labels"]:
        return cfg["uic_labels"][uic]
    if uic in cfg.get("terminus", {}):
        return cfg["terminus"][uic]
    return None


def tous_uic_surveilles(region: str | None = None) -> set[str]:
    cfg = _region_cfg(region)
    return set(cfg["uic_labels"].keys())


def url_journeys(
    uic_dep: str,
    uic_arr: str,
    *,
    coverage: str = NAVITIA_COVERAGE,
    count: int = 30,
    freshness: str = "realtime",
) -> str:
    return (
        f"https://api.sncf.com/v1/coverage/{coverage}/journeys"
        f"?from=stop_area:SNCF:{uic_dep}&to=stop_area:SNCF:{uic_arr}"
        f"&count={count}&data_freshness={freshness}"
    )


def trajets_radar(region: str | None = None) -> list[dict[str, str]]:
    """Liste des axes scannés par le radar (équivalent scanner_ligne.trajets)."""
    cfg = _region_cfg(region)
    resultat = []
    for nom, g_a, g_b in cfg.get("radar_paires") or ():
        uic_a = uic_pour_gare(g_a, region)
        uic_b = uic_pour_gare(g_b, region)
        if not uic_a or not uic_b:
            logger.warning("[GEO] UIC manquant radar %s : %s / %s", nom, g_a, g_b)
            continue
        resultat.append({
            "nom": nom,
            "gare_depart": g_a,
            "gare_arrivee": g_b,
            "uic_depart": uic_a,
            "uic_arrivee": uic_b,
            "url": url_journeys(uic_a, uic_b, coverage=cfg["coverage"]),
            "region": cfg["id"],
        })
    return resultat


def compter_axes_radar(regions: list[str] | None = None) -> int:
    return len(trajets_radar_multi(regions))


def valider_radar_region(region: str | None = None) -> list[str]:
    """Retourne les paires radar sans UIC résolu (contrôle qualité M7)."""
    cfg = _region_cfg(region)
    manquants = []
    for nom, g_a, g_b in cfg.get("radar_paires") or ():
        if not uic_pour_gare(g_a, region) or not uic_pour_gare(g_b, region):
            manquants.append(nom)
    return manquants


def trajets_radar_multi(regions: list[str] | None = None) -> list[dict[str, str]]:
    """Radar multi-régions — BAZO_RADAR_REGIONS=grand_est,idf,ara."""
    if not regions:
        regions = [DEFAULT_REGION]
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for rid in regions:
        rid = (rid or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        if rid not in REGIONS:
            logger.warning("[GEO] Région radar inconnue : %s", rid)
            continue
        out.extend(trajets_radar(rid))
    return out


def rechercher_gares(requete: str, region: str | None = None, limit: int = 12) -> list[str]:
    q = normaliser_cle_gare(requete)
    if not q:
        return toutes_les_gares(region)[:limit]
    return [g for g in toutes_les_gares(region) if q in normaliser_cle_gare(g)][:limit]


def resoudre_stop_area(
    nom_gare: str,
    *,
    http_get=None,
    token_sncf: str = "",
    region: str | None = None,
    log=None,
) -> str | None:
    """UIC stop_area — cache local puis API Navitia Places si besoin."""
    log = log or logger
    sid = stop_area_pour_gare(nom_gare, region)
    if sid:
        log.info("[GEO] UIC local '%s' -> SNCF:%s", nom_gare, sid)
        return sid
    if not http_get or not token_sncf:
        log.error("[GEO] Échec UIC '%s' (pas de fallback API)", nom_gare)
        return None
    try:
        import requests

        q = requests.utils.quote(str(nom_gare))
        coverage = _region_cfg(region)["coverage"]
        url = f"https://api.sncf.com/v1/coverage/{coverage}/places?q={q}&count=8"
        log.info("[GEO] Recherche UIC Places : %s", url)
        r = http_get(url, auth=(token_sncf, ""), timeout=15)
        if r.status_code != 200:
            log.warning("[GEO] Places HTTP %s pour '%s'", r.status_code, nom_gare)
            return None
        for place in r.json().get("places", []):
            pid = place.get("id", "")
            if "stop_area" in pid:
                found = pid.split(":")[-1]
                log.info("[GEO] UIC Places '%s' -> SNCF:%s", place.get("name"), found)
                return found
    except Exception as e:
        log.error("[GEO] Erreur Places pour '%s' : %s", nom_gare, e)
    log.error("[GEO] ÉCHEC résolution UIC pour '%s'", nom_gare)
    return None
