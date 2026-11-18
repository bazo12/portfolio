"""Module TGV premium — preview Phase 3 M22 (axes grand axe)."""
from __future__ import annotations

from bazo_geo import url_journeys

# Paires TGV principales (UIC voyageurs)
_TGV_PAIRES = (
    ("Paris ➔ Lyon TGV", "87113001", "87723197"),  # Paris-Est → Lyon Part-Dieu
    ("Lyon ➔ Paris TGV", "87723197", "87113001"),
    ("Paris ➔ Marseille TGV", "87113001", "87751008"),
    ("Marseille ➔ Paris TGV", "87751008", "87113001"),
    ("Paris ➔ Bordeaux TGV", "87391003", "87581009"),  # Montparnasse → Bordeaux
    ("Bordeaux ➔ Paris TGV", "87581009", "87391003"),
)

TGV_ACTIVE = False  # premium — désactivé par défaut


def trajets_tgv() -> list[dict[str, str]]:
    if not TGV_ACTIVE:
        return []
    out = []
    for nom, uic_a, uic_b in _TGV_PAIRES:
        out.append({
            "nom": nom,
            "uic_depart": uic_a,
            "uic_arrivee": uic_b,
            "url": url_journeys(uic_a, uic_b, coverage="sncf"),
            "type": "tgv",
        })
    return out
