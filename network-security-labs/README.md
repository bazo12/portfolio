# Labs réseaux — BTS CIEL

Adresser. Diagnostiquer. Durcir. Rien d'autre.

| | |
|---|---|
| [`scripts/sous_reseaux.py`](./scripts/sous_reseaux.py) | découpe CIDR → N sous-réseaux |
| [`scripts/diagnostic_reseau.sh`](./scripts/diagnostic_reseau.sh) | ping, port, DNS — lecture seule, cible locale |
| [`docs/checklist-anssi.md`](./docs/checklist-anssi.md) | comptes, surface, preuve |
| [`topologies/tp-ecole.md`](./topologies/tp-ecole.md) | plan `10.10.0.0/16` → 4 blocs |

```bash
python3 scripts/sous_reseaux.py 10.10.0.0/16 4
./scripts/diagnostic_reseau.sh 127.0.0.1 443
```

Les `.pkt` / GNS3 restent sur le PC. Ici : schéma + table, sans mot de passe d'équipement.

Pas un toolkit d'attaque. La cible d'un diagnostic est une machine que l'on possède.
