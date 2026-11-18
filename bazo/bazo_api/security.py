"""Authentification et durcissement API Bazo."""
from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bazo_config import ADMIN_API_KEY, ADMIN_LOCALHOST_ONLY, API_REQUIRE_AUTH, db_connect

_bearer = HTTPBearer(auto_error=False)

# Rate limit simple en mémoire : IP -> (fenêtre, compteur)
_RATE: dict[str, list[float]] = defaultdict(list)
_ADMIN_RATE: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = int(os.getenv("BAZO_API_RATE_LIMIT", "120"))  # req / minute / IP
ADMIN_RATE_LIMIT = int(os.getenv("BAZO_ADMIN_RATE_LIMIT", "30"))
RATE_WINDOW = 60


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def enforce_rate_limit(request: Request) -> None:
    limit = int(os.getenv("BAZO_API_RATE_LIMIT", str(RATE_LIMIT)))
    if limit <= 0:
        return
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _RATE[ip] if now - t < RATE_WINDOW]
    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="Trop de requêtes — réessaie dans 1 min")
    hits.append(now)
    _RATE[ip] = hits


def enforce_admin_rate_limit(request: Request) -> None:
    limit = int(os.getenv("BAZO_ADMIN_RATE_LIMIT", str(ADMIN_RATE_LIMIT)))
    if limit <= 0:
        return
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _ADMIN_RATE[ip] if now - t < RATE_WINDOW]
    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="Trop de requêtes admin")
    hits.append(now)
    _ADMIN_RATE[ip] = hits


def _admin_ip_allowed(request: Request) -> bool:
    if not ADMIN_LOCALHOST_ONLY:
        return True
    ip = _client_ip(request)
    return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")


def _token_from_request(
    credentials: HTTPAuthorizationCredentials | None,
    x_bazo_token: str | None,
) -> str | None:
    if credentials and credentials.credentials:
        return credentials.credentials.strip()
    if x_bazo_token:
        return x_bazo_token.strip()
    return None


def resolve_user_id(api_token: str) -> str | None:
    if not api_token or len(api_token) < 16:
        return None
    try:
        conn = db_connect(timeout=5)
        row = conn.execute(
            "SELECT id_telegram FROM inscriptions WHERE api_token = ?",
            (api_token,),
        ).fetchone()
        conn.close()
        return str(row[0]) if row else None
    except Exception:
        return None


def require_user(
    request: Request,
    id_telegram: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_bazo_token: Annotated[str | None, Header()] = None,
) -> str:
    """Vérifie que le token API correspond à id_telegram (anti-IDOR)."""
    enforce_rate_limit(request)
    if not API_REQUIRE_AUTH:
        return str(id_telegram)
    token = _token_from_request(credentials, x_bazo_token)
    if not token:
        raise HTTPException(status_code=401, detail="Token API requis (Authorization: Bearer …)")
    owner = resolve_user_id(token)
    if not owner or str(owner) != str(id_telegram):
        raise HTTPException(status_code=403, detail="Token invalide ou accès refusé")
    return owner


def require_user_body_token(
    request: Request,
    id_telegram: str,
    api_token: str | None,
) -> str:
    enforce_rate_limit(request)
    if not API_REQUIRE_AUTH:
        return str(id_telegram)
    if not api_token:
        raise HTTPException(status_code=401, detail="api_token requis dans le corps")
    owner = resolve_user_id(api_token.strip())
    if not owner or str(owner) != str(id_telegram):
        raise HTTPException(status_code=403, detail="Token invalide")
    return owner


def require_admin(
    request: Request,
    x_admin_key: Annotated[str | None, Header()] = None,
) -> None:
    enforce_admin_rate_limit(request)
    if ADMIN_LOCALHOST_ONLY and not _admin_ip_allowed(request):
        raise HTTPException(status_code=403, detail="Admin réservé au localhost")
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Clé admin non configurée")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=403, detail="Clé admin invalide")
