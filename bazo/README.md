# Bazo — preuves de parcours TER, sans tableur

**Anglais, une ligne :** production Linux service that watches regional trains, alerts the right station, and files delay/cancellation evidence as a PDF.

Service tiers, non affilié à la SNCF. Région d'exploitation : Grand Est (Sillon lorrain et axes voisins).

## Le besoin

Un abonné TER qui veut la Garantie Fiabilité doit prouver des retards et des suppressions sur **ses** gares et **ses** horaires. Relever ça à la main, train par train, ne tient pas un mois. Les applis grand public ne produisent pas un dossier daté, filtrable, exportable.

Bazo fait ce relevé à la place de l'usager.

## Ce que l'usager obtient

- Alertes Telegram seulement si le train concerne **sa** montée, **sa** descente et **son** créneau (pas la gare d'à côté, pas la ligne d'à côté).
- Enregistrement automatique des retards d'au moins 10 minutes et des suppressions.
- Dossier mensuel : PDF + texte prêt à coller dans le formulaire SNCF.
- Statut du mois : minutes cumulées, nombre de suppressions, seuil de garantie.

Déclarer un trajet reste possible. Ce n'est plus une condition pour qu'une suppression radar entre dans le dossier.

## Ce que l'exploitation exige

| Brique | Rôle |
|---|---|
| Radar (`scanner_ligne.py`) | Interroge l'API horaires, détecte retard et suppression, écrit l'historique |
| Filtre gares (`bazo_geo.py`, `bazo_metier.py`) | Même axe que le profil, correspondance de nom la plus précise, sens du train jusqu'à la gare de descente |
| Bot Telegram | Profil, alertes, PDF |
| API FastAPI | App mobile / PWA, auth par jeton, admin séparé |
| SQLite | Historique, trajets, inscriptions. PostgreSQL prévu, pas obligatoire pour tourner |
| systemd | `bazo-bot`, `bazo-radar`, `bazo-api` redémarrent seuls |
| Quota | Budget quotidien d'appels API, rotation des axes, réserve pour le check-in |

Le filtre n'est pas une liste de gares en dur. Il calcule le segment entre la gare de départ et la gare d'arrivée du profil. Une suppression en amont de la montée (la gare précédente sur l'axe) ne part pas à l'usager. Une suppression sur sa montée, ou entre sa montée et sa descente, dans la fenêtre d'alerte, est notifiée **et** comptée.

## Pourquoi c'est un sujet d'admin système

- Un processus qui tourne 24 h/24 sur un VPS, avec journalisation et redémarrage.
- Une API externe limitée : il faut choisir quoi scanner, sinon le quota est mort à midi.
- Des données personnelles (identité Telegram, gares, horaires) : auth, pas de secrets dans le dépôt, export/suppression prévus.
- Un document juridique-pratique (le PDF) qui doit coller à ce que l'usager a vraiment reçu comme alerte.

## Ce dépôt

Vitrine relue. Elle ne contient pas le code de production, pas de base, pas de jeton. Le fichier `.env.example` ne liste que des noms de variables, avec des valeurs vides.

## Hors dépôt public

Base de production, sauvegardes, archives GTFS, journaux, fichier de secrets du serveur, identifiant admin.
