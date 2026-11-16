# Publier sans se griller

Un dépôt GitHub public est lu par des recruteurs **et** par des robots. Tout secret commité une fois reste dans l'historique, même après un commit de « nettoyage ».

## Interdit dans un commit

- `.env`, `SECRETS_*.txt`, clés SSH, cookies, tokens Telegram, clés SNCF / Navitia, clés admin
- Bases SQLite de production, sauvegardes, journaux, `archives_gtfs/`
- Fichiers Excel ou CSV de résidents (noms, téléphones, adresses, dossiers médicaux ou sociaux)
- Caches d'outils : `bazo-venv/`, `.pub-cache/`, `flutter/`, `.cursor/`
- Identifiants Telegram réels, URL d'administration avec clé dans le query string

## Avant le premier `git push`

```bash
# depuis une COPIE, jamais depuis le dossier de production
rg -n -i "token|api_key|password|secret|BEGIN (RSA |OPENSSH )" --glob '!.git/**' .
```

S'il y a un hit sur une vraie valeur : on la remplace par un placeholder, on ne publie pas le dossier d'origine, on crée un dépôt neuf. On ne réécrit pas l'historique d'un dépôt déjà poussé sans comprendre `git filter-repo`.

## Fichiers à fournir à la place

- `.env.example` avec des variables vides
- Jeu de données **fictif** (`resident_demo.csv` avec « Résident Démo »)
- Schémas exportés en PNG/SVG, pas les projets Packet Tracer s'ils contiennent des mots de passe d'équipement

## Dépôts prévus

| Nom GitHub suggéré | Contenu public |
|---|---|
| `bazo` | Architecture, extraits sans secret, `.env.example` |
| `conciergerie-zenith` | Besoin métier, modèle de données fictif, build Windows |
| `network-security-labs` | Labs, scripts, schémas |

Le code de production Bazo reste sur le VPS. Le dépôt public en est une vitrine relue, pas une copie brute du serveur.
