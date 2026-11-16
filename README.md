# Portfolio — outils métier, exploitation, réseaux

Profil orienté **livraison d'outils utilisables**, pas une liste de langages.

Trois dépôts, trois preuves :

| Dépôt | Problème traité | Ce qu'un recruteur doit retenir |
|---|---|---|
| [bazo](./bazo/README.md) | Les usagers TER perdent des preuves de retard et de suppression | Service Linux en production : radar, base, bot, PDF, quotas API |
| [conciergerie-zenith](./conciergerie-zenith/README.md) | Une résidence de 114+ personnes tournait encore sur Excel | Logiciel terrain pour des non-informaticiens, exécutable Windows autonome |
| [network-security-labs](./network-security-labs/README.md) | BTS CIEL : savoir concevoir, diagnostiquer, durcir | Sous-réseaux, topologies, scripts, hygiène ANSSI |

## Ce qui n'est jamais dans ces dépôts

Jetons d'API, mots de passe, fichiers `.env`, bases de production, exports Excel de résidents, journaux, archives temps réel, caches Flutter/SDK.

La checklist de publication est dans [PUBLICATION.md](./PUBLICATION.md).

## Angle de lecture

Chaque README répond à quatre questions :

1. Quel irritant métier a justifié l'outil ?
2. Qui s'en sert, et qu'est-ce qui a été retiré de leur charge manuelle ?
3. Quelle architecture tient en production ou sur un poste isolé ?
4. Quelles compétences cela démontre, au-delà du code ?

Les sources de la conciergerie et les fichiers Packet Tracer / GNS3 sont sur le poste Windows. Ils ne sont pas sur le serveur. Ils n'entrent ici qu'après relecture des secrets et des données personnelles.
