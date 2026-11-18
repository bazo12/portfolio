# TP — réseau école `10.10.0.0/16`

Objectif : 4 sous-réseaux de taille égale, avant de configurer un routeur.

```bash
python3 ../scripts/sous_reseaux.py 10.10.0.0/16 4
```

| Réseau | Masque | Première | Dernière | Broadcast |
|---|---|---|---|---|
| 10.10.0.0/18 | 255.255.192.0 | 10.10.0.1 | 10.10.63.254 | 10.10.63.255 |
| 10.10.64.0/18 | 255.255.192.0 | 10.10.64.1 | 10.10.127.254 | 10.10.127.255 |
| 10.10.128.0/18 | 255.255.192.0 | 10.10.128.1 | 10.10.191.254 | 10.10.191.255 |
| 10.10.192.0/18 | 255.255.192.0 | 10.10.192.1 | 10.10.255.254 | 10.10.255.255 |

Affectation lab : admin / salles / Wi-Fi / serveurs. Pas le plan d'un client.
