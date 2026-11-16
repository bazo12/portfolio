# Conciergerie Zénith — le terrain sans Excel

**Anglais, une ligne :** desktop tool that replaced a shared spreadsheet for a 114+ resident building: rounds, notifications, and a Windows program staff can double-click.

Le code source est sur le poste Windows. Il n'est pas sur le serveur Linux. Ce dossier décrit le produit et la façon de le publier **sans** les données des résidents.

## Le besoin

Une conciergerie suit plus d'une centaine de résidents : présences, demandes, passages, relances. Le fichier Excel partagé devient le logiciel par défaut. Il casse dès qu'il y a deux personnes dessus, aucune trace claire, et aucune notification fiable. Le personnel n'est pas informaticien : un outil qui demande Python ou un serveur est un outil qui ne sera pas utilisé.

Le logiciel devait donc :

- reprendre la base déjà là (Excel / SQLite) au lieu d'imposer une ressaisie ;
- envoyer les notifications qui partaient avant à la main ;
- s'installer comme un programme Windows autonome (double-clic, pas de runtime à expliquer).

## Ce que ça démontre

- Partir d'un flux réel, pas d'un cahier des charges imaginaire.
- Livrer à des utilisateurs non techniques : écran, vocabulaire métier, exécutable.
- Séparer le programme et les données. Le programme peut être public. La base des 114 résidents, non.

## Modèle (données fictives)

| Entité | Exemples de champs | Jamais dans Git |
|---|---|---|
| Résident | code interne, bâtiment, lot | nom, téléphone, adresse réels |
| Passage | date, type, statut | compte-rendu nominatif |
| Notification | canal, horaire, modèle de message | jetons SMS / mail |

Jeu de démo accepté dans le dépôt : `DEMO-001`, « Résident Démo », bâtiment A. Rien d'autre.

## Publication prévue

1. Copier le projet Windows dans ce dossier, **sans** le `.xlsx` de production.
2. Chercher mots de passe, clés SMTP, chemins `C:\Users\...`.
3. Ajouter un `README` de build (PyInstaller ou l'outil réellement utilisé) et une capture d'écran avec le jeu fictif.
4. Pousser cette copie, pas le dossier de travail.

## Compétences visibles pour un recruteur

Recueil du besoin sur le terrain, modèle de données simple, automatisation des relances, empaquetage Windows, hygiène des données personnelles (ne pas publier la base pour « faire vrai »).
