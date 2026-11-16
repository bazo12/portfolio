#!/usr/bin/env python3
"""Découpe un réseau IPv4 en N sous-réseaux de taille égale.

Usage :
    python3 sous_reseaux.py 10.10.0.0/16 4
"""
from __future__ import annotations

import ipaddress
import math
import sys


def decouper(cidr: str, nombre: int) -> list[ipaddress.IPv4Network]:
    if nombre < 1:
        raise SystemExit("Le nombre de sous-réseaux doit être >= 1")
    reseau = ipaddress.ip_network(cidr, strict=False)
    bits = math.ceil(math.log2(nombre))
    nouveau = reseau.prefixlen + bits
    if nouveau > 30:
        raise SystemExit("Préfixe trop long : il ne reste plus d'adresses hôtes")
    return list(reseau.subnets(prefixlen_diff=bits))[:nombre]


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage : sous_reseaux.py <réseau/préfixe> <nombre>")
    blocs = decouper(sys.argv[1], int(sys.argv[2]))
    print(f"{'Réseau':<20} {'Masque':<16} {'Première':<16} {'Dernière':<16} Broadcast")
    for bloc in blocs:
        hotes = list(bloc.hosts())
        premiere = str(hotes[0]) if hotes else "-"
        derniere = str(hotes[-1]) if hotes else "-"
        print(f"{str(bloc):<20} {str(bloc.netmask):<16} {premiere:<16} {derniere:<16} {bloc.broadcast_address}")


if __name__ == "__main__":
    main()
