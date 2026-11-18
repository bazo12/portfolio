"""Jetons signés HMAC pour téléchargement PDF temporaire (M2 S1)."""
import base64
import hashlib
import hmac
import time

from bazo_config import API_SECRET, PDF_TOKEN_TTL_SEC


def cache_key_pdf(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def creer_token_pdf(id_telegram: str, mois: str, ttl_sec: int | None = None) -> tuple[str, int]:
    """Retourne (token, expires_at unix)."""
    ttl = ttl_sec if ttl_sec is not None else PDF_TOKEN_TTL_SEC
    exp = int(time.time()) + ttl
    payload = f"{id_telegram}:{mois}:{exp}"
    sig = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return token, exp


def verifier_token_pdf(token: str) -> tuple[str, str]:
    """Valide le jeton — retourne (id_telegram, mois) ou lève ValueError."""
    if not token or not token.strip():
        raise ValueError("token manquant")
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + padding).decode()
    except Exception as exc:
        raise ValueError("token invalide") from exc
    if raw.count(":") < 3:
        raise ValueError("token mal formé")
    id_tg, mois, exp_str, sig = raw.rsplit(":", 3)
    payload = f"{id_tg}:{mois}:{exp_str}"
    attendu = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(attendu, sig):
        raise ValueError("signature invalide")
    try:
        exp = int(exp_str)
    except ValueError as exc:
        raise ValueError("expiration invalide") from exc
    if time.time() > exp:
        raise ValueError("token expiré")
    return id_tg, mois
