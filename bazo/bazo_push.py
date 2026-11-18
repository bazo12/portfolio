"""Push notifications Firebase FCM (M19)."""
from __future__ import annotations

import logging
import os

from bazo_config import make_session

logger = logging.getLogger("bazo.push")

FCM_ENABLED = os.getenv("BAZO_FCM_ENABLED", "0") == "1"
FCM_SERVER_KEY = os.getenv("BAZO_FCM_SERVER_KEY", "").strip()
FCM_URL = "https://fcm.googleapis.com/fcm/send"
_HTTP = make_session(pool=4)


def push_disponible() -> bool:
    return FCM_ENABLED and bool(FCM_SERVER_KEY)


def envoyer_push(device_token: str, titre: str, corps: str, data: dict | None = None) -> bool:
    """Envoie une notification FCM (API legacy)."""
    if not push_disponible() or not device_token:
        return False
    payload = {
        "to": device_token.strip(),
        "priority": "high",
        "notification": {"title": titre[:120], "body": corps[:500]},
    }
    if data:
        payload["data"] = {str(k): str(v) for k, v in data.items()}
    try:
        r = _HTTP.post(
            FCM_URL,
            headers={
                "Authorization": f"key={FCM_SERVER_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning("[FCM] HTTP %s : %s", r.status_code, r.text[:200])
            return False
        body = r.json()
        if body.get("failure", 0) > 0:
            logger.warning("[FCM] échec envoi : %s", body)
            return False
        return True
    except Exception as e:
        logger.error("[FCM] erreur : %s", e)
        return False


def envoyer_push_utilisateur(id_telegram: str, titre: str, corps: str, data: dict | None = None) -> int:
    """Envoie à tous les devices enregistrés d'un utilisateur. Retourne le nb de succès."""
    from bazo_devices import lister_devices

    n = 0
    for dev in lister_devices(id_telegram):
        if envoyer_push(dev["device_token"], titre, corps, data):
            n += 1
    return n
