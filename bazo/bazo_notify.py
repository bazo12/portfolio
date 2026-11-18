"""Envoi Telegram centralisé pour les modules temps réel / grève.

- `SIMULATION = True` (ou BAZO_SIMULATION=1) : aucun message n'est envoyé, ils sont
  collectés dans `MESSAGES_SIMULES` (tests, dry-run, répétitions avant la grève).
- Les identifiants non numériques (comptes app_ / usr_) sont ignorés : pas de Telegram.
- Envoi réel UNIQUEMENT si BAZO_ALERTES_TEMPS_REEL=1 (défaut : coupé, aucun message aux usagers).
"""
from __future__ import annotations

import logging
import os

import requests

from bazo_config import TOKEN_TELEGRAM

logger = logging.getLogger("bazo.notify")

SIMULATION: bool = os.getenv("BAZO_SIMULATION", "0") == "1"
MESSAGES_SIMULES: list[dict] = []
_HTTP = requests.Session()


def envoi_actif() -> bool:
    # Sécurité : aucun envoi automatique aux usagers sans activation explicite.
    return os.getenv("BAZO_ALERTES_TEMPS_REEL", "0") == "1"


def est_chat_telegram(chat_id) -> bool:
    return str(chat_id or "").strip().isdigit()


def envoyer(chat_id, texte: str, reply_markup=None) -> bool:
    """Retourne True si le message est parti (ou simulé)."""
    if not est_chat_telegram(chat_id):
        return False
    if SIMULATION:
        MESSAGES_SIMULES.append({"chat_id": str(chat_id), "texte": texte})
        return True
    if not envoi_actif():
        logger.info("[NOTIFY] envoi désactivé (BAZO_ALERTES_TEMPS_REEL=0) -> %s", chat_id)
        return False
    url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage"
    payload = {"chat_id": chat_id, "text": texte, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = _HTTP.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        payload.pop("parse_mode", None)
        r2 = _HTTP.post(url, json=payload, timeout=10)
        if r2.status_code != 200:
            logger.warning("[NOTIFY] échec vers %s : HTTP %s", chat_id, r2.status_code)
            return False
        return True
    except Exception as e:  # réseau
        logger.warning("[NOTIFY] impossible vers %s : %s", chat_id, e)
        return False
