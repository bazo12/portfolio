# Guide utilisateur Bazo

Bot Telegram : **@SbazoTrain_bot**  
Région prod : TER Grand Est (Sillon Lorrain) · autres régions en preview

---

## Démarrage rapide

1. Ouvre **@SbazoTrain_bot** sur Telegram → `/start`
2. **⚙️ Configuration Aller-Retour** : tes gares + créneaux 2 h (ex. Metz 08h–10h, Luxembourg 18h–20h)
3. Le matin : **🚆 Déclarer mon trajet** → choisis ton train
4. Le soir (~20 h) : réponds au **check-out** (1 clic)
5. Fin de mois : **📂 Mon PDF Instantané** pour ton dossier SNCF

---

## App web (PWA)

Accès : **http://127.0.0.1:8080/app/** (ou ton domaine `/app/`)

1. Envoie **`/token`** au bot en **message privé** → copie ton token API
2. Dans l'app : ID Telegram + token → **Enregistrer**
3. Onglets disponibles :
   - **Profil** — tes gares et alertes
   - **Trajet** — check-in
   - **Statut** — progression Garantie Fiabilité
   - **Check-out** — trajets en attente
   - **PDF** — lien attestation mensuelle
   - **Réclamation** — texte copier-coller SNCF
   - **Incidents** — signalements du mois
   - **Compte** — export RGPD, déconnexion, suppression

Pour révoquer ton token : **`/token regen`** en MP Telegram.

Commandes utiles :
- **`/token`** en MP — token API pour l'app web
- **`/app`** — lien et instructions PWA
- **`/beta`** — progression beta + texte de partage

---

## Boutons principaux (Telegram)

| Bouton | Action |
|--------|--------|
| 📊 Mon Statut | Bilan du mois + progression Garantie Fiabilité |
| 📂 Mon PDF Instantané | Attestation PDF + texte réclamation SNCF |
| 🚆 Déclarer mon trajet | Check-in matinal (trains de ton créneau) |
| ⚠️ Signaler un incident | Retard, suppression, photo |
| 🔴 Trafic radar | Perturbations sur ton axe aujourd'hui |
| 💸 Bazo - Ton Allié Réclamation | Lien + texte copier-coller SNCF |
| ⚙️ Configuration | Gares aller/retour + créneaux 2 h |
| 📅 Jours & Vacances | Jours actifs + mode vacances (alertes off, check-out actif) |

---

## Alertes retard / suppression

- Tu reçois une alerte si :
  - ton **créneau 2 h** est concerné (± 1 h de marge pour l’alerte)
  - le train est sur **ton axe** (ex. Hagondange compte si tu es Metz ↔ Luxembourg)
  - tu as fait un **check-in** sur ce train → alerte prioritaire « TON train »

- Pas d’alerte si le train est **hors de tes horaires** configurés → élargis ton créneau dans ⚙️ Configuration.

---

## PDF et réclamation SNCF

- Max **2 incidents/jour** (1 aller + 1 retour) sauf **suppression de ton train déclaré**
- Retards **≥ 10 min** comptent pour le PDF
- Seuils Garantie Fiabilité abonnement : **120 min cumulées** ou **3 suppressions/mois**

---

## Admin

- `/admin` — panneau Telegram (stats, modération, PDF global)
- Dashboard web : `/admin/` (clé `BAZO_ADMIN_API_KEY`)

---

## Aide

- **📖 Aide** ou `/aide` dans le bot
- Bug ou question : **💬 Contact / Bug**

---

*Bazo est un service tiers indépendant, non affilié à la SNCF.*
