# Labs réseaux & cybersécurité — BTS CIEL

**Anglais, une ligne :** subnetting drills, topology notes, and small diagnostic scripts — written the way a junior operator should document a network.

Les fichiers Packet Tracer (`.pkt`) et projets GNS3 sont sur le PC. On ne les invente pas ici. On pose le cadre dans lequel ils seront ajoutés, plus des scripts que l'on peut lancer sans matériel.

## Pourquoi ces labs existent

En CIEL, le risque est d'empiler des captures d'écran sans dire **ce que le réseau doit garantir**. Un recruteur infrastructure lit un dépôt pour voir si la personne :

- calcule un plan d'adressage avant de brancher ;
- sait expliquer une topologie (qui parle à qui, où est le filtrage) ;
- diagnostique avec des commandes, pas seulement avec un clic ;
- applique une hygiène de base (ANSSI) au lieu de laisser `admin/admin` « pour le lab ».

## Sommaire

| Dossier | Contenu |
|---|---|
| `scripts/sous_reseaux.py` | Découpe d'un réseau en sous-réseaux de taille égale |
| `scripts/diagnostic_reseau.sh` | Ping, port, résolution DNS — lecture seule |
| `docs/checklist-anssi.md` | Réflexes de durcissement pour un lab et pour un poste |
| `topologies/` | Emplacement des schémas exportés (PNG/SVG). Pas de mots de passe d'équipement dans l'image |

## Exemple de plan d'adressage

Réseau école fictif `10.10.0.0/16`, 4 sous-réseaux :

```bash
python3 scripts/sous_reseaux.py 10.10.0.0/16 4
```

Chaque ligne donne réseau, masque, première et dernière adresse utilisable, broadcast. C'est le réflexe avant de remplir un routeur.

## Topologies à déposer depuis le PC

Pour chaque TP, un fichier `topologies/tp-XX.md` :

- objectif (une phrase) ;
- schéma exporté ;
- table d'adressage ;
- ce qui a échoué la première fois et la commande qui l'a montré.

Les `.pkt` peuvent rester hors Git s'ils sont lourds ou s'ils contiennent des comptes. L'export image + la table suffisent à un recruteur.

## Ce que ce dépôt n'est pas

Un toolkit d'attaque. Les scripts ne scannent pas un réseau tiers, ne bruteforcent rien, n'emballent pas d'exploit. Le diagnostic vise une cible que l'on possède (`127.0.0.1` ou une IP de lab passée en argument).
