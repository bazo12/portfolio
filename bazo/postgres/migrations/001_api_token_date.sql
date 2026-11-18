-- Migration 001 — api_token + date_inscription (juin 2026)
ALTER TABLE inscriptions ADD COLUMN IF NOT EXISTS api_token TEXT;
ALTER TABLE inscriptions ADD COLUMN IF NOT EXISTS date_inscription TIMESTAMPTZ;
