-- Schéma PostgreSQL Bazo — M2 S2 (équivalent sillon_lorrain.db)
-- Usage : psql -U bazo -d bazo -f postgres/schema.sql

BEGIN;

CREATE TABLE IF NOT EXISTS inscriptions (
    id_telegram TEXT PRIMARY KEY,
    nom TEXT,
    prenom TEXT DEFAULT 'Voyageur',
    gare_depart TEXT,
    heures TEXT,
    gare_retour TEXT,
    heures_retour TEXT,
    age TEXT DEFAULT 'Non précisé',
    profils_sauvegardes TEXT DEFAULT '{}',
    jours_actifs TEXT DEFAULT '1,2,3,4,5',
    alertes_actives INTEGER DEFAULT 1,
    vacances_jusquau TEXT,
    api_token TEXT,
    date_inscription TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS historique (
    id SERIAL PRIMARY KEY,
    date TEXT,
    numero_train TEXT,
    ligne TEXT,
    gare TEXT,
    type TEXT,
    retard_min INTEGER,
    motif TEXT,
    heure_theo TEXT,
    heure_reel TEXT,
    retard_depart_min INTEGER,
    retard_arrivee_min INTEGER
);

CREATE TABLE IF NOT EXISTS surveillance_trains (
    id SERIAL PRIMARY KEY,
    date TEXT NOT NULL,
    numero_train TEXT NOT NULL,
    gare_depart TEXT NOT NULL,
    gare_arrivee TEXT NOT NULL,
    destination TEXT,
    heure_theo TEXT,
    retard_depart_min INTEGER,
    retard_arrivee_min INTEGER,
    statut TEXT DEFAULT 'surveillance',
    motif TEXT,
    UNIQUE (date, numero_train, gare_depart)
);

CREATE TABLE IF NOT EXISTS trajets_valides (
    id SERIAL PRIMARY KEY,
    id_telegram TEXT NOT NULL,
    numero_train TEXT NOT NULL,
    date TEXT NOT NULL,
    heure_theo TEXT NOT NULL,
    heure_arrivee_theo TEXT,
    heure_reelle_arrivee TEXT,
    statut_validation TEXT DEFAULT 'en_attente',
    tranche_horaire TEXT,
    sens TEXT,
    gare_depart TEXT,
    gare_arrivee TEXT,
    destination TEXT,
    retard_arrivee_min INTEGER,
    motif_validation TEXT,
    UNIQUE (id_telegram, date, numero_train, heure_theo)
);

CREATE TABLE IF NOT EXISTS horaires_theoriques_jour (
    id SERIAL PRIMARY KEY,
    date TEXT NOT NULL,
    axe TEXT,
    numero_train TEXT NOT NULL,
    gare_depart TEXT,
    gare_arrivee TEXT,
    destination TEXT,
    heure_theo TEXT,
    heure_arrivee_theo TEXT,
    vu_temps_reel INTEGER DEFAULT 0,
    statut TEXT DEFAULT 'theorique',
    UNIQUE (date, numero_train, gare_depart)
);

CREATE TABLE IF NOT EXISTS rapports_incidents (
    id SERIAL PRIMARY KEY,
    date TEXT NOT NULL,
    heure TEXT,
    id_telegram TEXT NOT NULL,
    prenom TEXT,
    message TEXT,
    statut TEXT DEFAULT 'en_attente',
    retard_min INTEGER DEFAULT 0,
    traite_par TEXT,
    date_traitement TEXT,
    type_signalement TEXT,
    photo_file_id TEXT,
    numero_train TEXT,
    gare TEXT,
    id_trajet INTEGER
);

CREATE TABLE IF NOT EXISTS memoire_radar (
    cle TEXT PRIMARY KEY,
    valeur TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS device_tokens (
    id SERIAL PRIMARY KEY,
    id_telegram TEXT NOT NULL,
    device_token TEXT NOT NULL,
    plateforme TEXT DEFAULT 'unknown',
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    UNIQUE (id_telegram, device_token)
);

CREATE INDEX IF NOT EXISTS idx_device_user ON device_tokens (id_telegram);

CREATE INDEX IF NOT EXISTS idx_hist_date ON historique (date);
CREATE INDEX IF NOT EXISTS idx_hist_date_train ON historique (date, numero_train);
CREATE INDEX IF NOT EXISTS idx_hist_train_heure ON historique (date, numero_train, gare, heure_theo);
CREATE INDEX IF NOT EXISTS idx_tv_user_date ON trajets_valides (id_telegram, date);
CREATE INDEX IF NOT EXISTS idx_tv_date ON trajets_valides (date);
CREATE INDEX IF NOT EXISTS idx_surv_date ON surveillance_trains (date, statut);
CREATE INDEX IF NOT EXISTS idx_rap_user_statut ON rapports_incidents (id_telegram, statut);

COMMIT;
