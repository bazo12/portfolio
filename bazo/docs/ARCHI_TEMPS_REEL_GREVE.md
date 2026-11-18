# Bazo — Temps réel multi-sources, report de train, grève (architecture)

Statut : code prêt, testé sur copies de la base (22 tests temps réel + 102 tests parcours critique). **Rien n'est déployé** : aucun service redémarré, schéma de prod inchangé, aucun message envoyé aux usagers.

## 1. Rapport d'analyse — incident du 04/11 (Bettembourg)

| Constat | Preuve | Conséquence |
|---|---|---|
| Aucun trajet validé ce jour-là | `trajets_valides` vide pour 2026-11-04 | Aucun usager « suivi » sur le 08h33 → rien à alerter nominativement |
| Pas de train habituel connu | `inscriptions` = fenêtres horaires seulement (8h-10h) ; check-ins de l'usager = 88726 / 88724, pas 88571 (08h33) | Le ciblage « ton train » n'existait pas |
| Scan théorique incomplet | `horaires_theoriques_jour` ne contenait que Metz→Luxembourg (35 lignes) : verrou SQLite (écriture ouverte + compteur de quota) interrompait le snapshot après le 1er axe | Sens retour et axes proches de Bettembourg sans référence : disparitions et retards non comparables → détection bidirectionnelle partielle |
| Identifiants de gares erronés | Sarrebourg→Mulhouse, Sarreguemines→Longwy, Howald→Luxembourg, Thionville inexistant dans `bazo_geo` | Axes entiers scannés sur de mauvaises gares |
| Aucune trace de 88571 / 88724 dans `historique` | requêtes sans fenêtre rétrospective, incidents « périmés » traités comme à notifier | Trains en retard au départ invisibles |
| Suppression partielle non modélisée | seule l'annulation totale était lue ; aucun terminus effectif | Terminus imprévu à Thionville jamais détecté |
| Aucun cumul de retard | pas de notion de « 2ᵉ train de remplacement » | Impossible de produire 1h30 |

Archives GTFS-RT horaires (boîte noire) pour le 04/11 :

- 88571 (Metz 08h33 → Lux. 09h24) : 08h33 à l'heure à 08h00, puis **+15 min** à 09h01 (départ 08h48), absent dès 10h01 ; **aucun arrêt « supprimé » publié** (terminus inchangé). Le flux SNCF n'a donc pas, à ces instants, annoncé la suppression partielle : la détection par arrêts supprimés ne suffit pas, d'où le filet « faux maintenu ».
- 88724 (Metz 09h03 → Lux. 10h03) : +20 min au départ, arrivée prévue 10h33 à 10h01, puis 11h03 à 11h02, soit **99 min** par rapport à l'arrivée initiale de 88571 (09h24). L'usager indique ≈ 1h30 : l'heure réelle d'arrivée reste à confirmer (voir §6).
- Limite : une photo par heure ; la boucle à 60 s ne rattrapera pas le passé mais capture désormais chaque transition.

## 2. Architecture

```
GTFS-RT SNCF (0,2 s)  ─┐
CFL mobiliteit.lu     ─┼─►  bazo_sources_rt.fusionner_sources()  ──►  état unifié par train
  (départs Bettembourg)│        « le plus défavorable gagne », comptage des sources
Navitia (ciblé, quota)─┘
                                   │
             temps_reel_service.cycle()  (60 s, service bazo-temps-reel)
                                   ▼
 bazo_temps_reel : états/évènements ─ impacts usagers ─ faux maintenus ─ reports
 bazo_greve      : plan J et J+1 ─ import grille officielle ─ trains habituels ─ alertes ciblées
 bazo_alternatives : train précédent / suivant réellement circulant + arrivée estimée
 bazo_notify     : unique point d'envoi Telegram (simulation, coupe-circuit)
```

Clé `.env` : `MOBILITEIT_API_KEY` uniquement (lue par `os.getenv`, jamais écrite ailleurs).

Tables ajoutées (additives) : `etats_trains`, `evenements_trains`, `alertes_temps_reel` (dédoublonnage par signature), `suivi_immobilisation`, `reports_trajet`, `trains_habituels`, `greve_plan`, `greve_meta`, `greve_import`, plus colonnes phase 2 (`provenance`, `retard_cumule_min`, statut `valide_report`).

## 3. Règles de détection

1. **Suppression partielle** : arrêts finaux `SKIPPED`/annulés ⇒ terminus effectif ≠ terminus prévu ; fusion SNCF + CFL.
2. **Bascule automatique** : pour chaque usager suivi (check-in ou train habituel) dont la section est coupée, recherche du premier train réel depuis la gare de rupture (`trouver_remplacant`), enregistrement dans `reports_trajet`, statut `valide_report`, message unique (signature). Re-bascule si le remplaçant est lui-même supprimé.
3. **Faux maintenu** (filet de sécurité) : train à ≥ 15 min de retard dont le prochain arrêt ne change plus et dont la première prévision est dépassée de > 5 min, alors qu'un train suivant atteint la même gare avant lui ⇒ alerte préventive « attendre / basculer » avec l'alternative.
4. **Retard réel cumulé** = arrivée réelle du 2ᵉ train − arrivée **initialement prévue** du 1ᵉʳ (`calculer_retard_cumule`), stocké dans `reports_trajet.retard_cumule_min`, `trajets_valides.retard_arrivee_min` et `evenements_trains` (type `retard_cumule`). Sert au PDF (motif « Arrêt à X : changement de train forcé »).
5. **Fiabilité** : commit par axe/segment/cible ; compteur de quota tolérant au verrou ; aucune écriture n'écrase une donnée d'une autre source (états fusionnés, évènements en ajout).

## 4. Grève (jeudi 05/11)

- `veille_greve` : plan J+1 dès le service lancé (mode veille), alertes **à partir de 17h00 la veille**, mise à jour jour J (intervalle 5–15 min selon activité).
- Plan par segment : horaires de base vs temps réel Navitia ⇒ `maintenu` / `modifie` / `supprime` ; bascule automatique en mode `auto` quand la proportion de suppressions le justifie (test réel : 228 / 521 trains supprimés détectés pour le 05/11).
- Grille officielle SNCF : `bazo_greve_admin.py importer` (liste de numéros ou CSV `numero,statut,heure`). Par défaut **exhaustive** : tout train absent est supprimé ; `--non-exhaustif` pour une liste partielle.
- Message personnalisé : si un train habituel existe (`/montrain 08h33`, déduction automatique après ≥ 2 check-ins identiques, ou saisie opérateur), alerte ciblée + train précédent/suivant réel avec heure de départ et arrivée estimée ; sinon récapitulatif du créneau de l'usager.

## 5. Exploitation

```bash
# Répétition à blanc (aucun envoi), sur une copie :
BAZO_DB_PATH=/tmp/copie.db python3 bazo_greve_admin.py repetition
python3 bazo_greve_admin.py statut --date 2026-11-05
python3 bazo_greve_admin.py importer --date 2026-11-05 --fichier grille.csv
python3 bazo_greve_admin.py forcer --date 2026-11-05 --mode force
python3 bazo_greve_admin.py habituel --id <telegram> --depart Metz --arrivee Luxembourg --heure 08h33
python3 bazo_greve_admin.py report --id <telegram> --date 2026-11-04 --train1 88571 --train2 88724 \
    --depart Metz --arrivee Luxembourg --rupture Thionville --arrivee-reelle 10h54
```

Sécurités : `BAZO_ALERTES_TEMPS_REEL` absent ou ≠ 1 ⇒ aucun message automatique ; `BAZO_SIMULATION=1` ⇒ messages collectés en mémoire seulement.

## 6. Déploiement (à valider explicitement)

1. Sauvegarde base + code ; appliquer le schéma additif (`init_schema()` des modules, idempotent).
2. Redémarrer `bazo-radar` (correctifs scan), `bazo-bot` (`/montrain`, statut `valide_report`), `bazo-api`, `bazo-mobile`.
3. Installer `deploy/bazo-temps-reel.service` (envoi coupé par défaut) ; vérifier `bazo_greve_admin.py statut`.
4. Pour activer les alertes aux usagers : décision explicite, puis `Environment=BAZO_ALERTES_TEMPS_REEL=1`.
5. Saisir le cumul du 04/11 pour l'usager concerné (commande `report`) une fois l'heure d'arrivée réelle confirmée.

## 7. Correctif « Déclarer mon trajet » (04/11, déployé)

Cause : `recuperer_trains_theoriques` n'interrogeait Navitia qu'au milieu du créneau (9h00 pour 8h-10h) ⇒ tout train partant avant (08h33) était invisible.

Correctifs (`bot_interactif.py`) :
- recherche en `base_schedule` depuis le **début** du créneau, pagination jusqu'à la fin ;
- trains directs d'abord, repli sur le 1er tronçon des correspondances s'il n'y a pas de direct ;
- TGV exclus de la requête (`forbidden_uris`) pour qu'ils ne masquent pas des TER plus lents ;
- trains supprimés conservés et marqués `supprime` (« ❌ supprimé » dans le menu) ;
- croisement `departures` de la gare × `arrivals` de la destination (même numéro) pour retrouver les TER « dominés » que l'API d'itinéraires omet ;
- cache `v2_…`, jamais alimenté en cas d'erreur API.

Vérification : comparaison avec le GTFS-RT du jour sur 24 trajets × 3 créneaux ; seuls les TGV INOUI (exclus par conception) manquent. Test de non-régression : `checkin_liste_creneau_complet`.

## 8. État au 04/11 21h20

Déployé : schéma additif, services `bazo-radar`, `bazo-bot`, `bazo-api`, `bazo-mobile` redémarrés, `bazo-temps-reel` actif.
Alertes automatiques aux usagers : **activées le 04/11 à 21h23** sur décision explicite (`Environment=BAZO_ALERTES_TEMPS_REEL=1` dans l'unité). Coupe-circuit : retirer cette ligne + `systemctl restart bazo-temps-reel`.
En attente : heure d'arrivée réelle + identifiant pour enregistrer le retard cumulé du 04/11 (`bazo_greve_admin.py report`).
