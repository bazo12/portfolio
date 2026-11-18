"""Configuration Bazo — secrets via .env ou SECRETS_BAZO_LOCAL.txt (jamais en dur dans le code)."""
import logging
import os
import sqlite3
import hashlib

logger_quota = logging.getLogger("bazo.quota")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SECRETS_FILE = os.path.join(_BASE_DIR, "SECRETS_BAZO_LOCAL.txt")


def _charger_dotenv():
    path = os.path.join(_BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            cle, _, val = line.partition("=")
            cle, val = cle.strip(), val.strip().strip('"').strip("'")
            if cle and cle not in os.environ:
                os.environ[cle] = val


def _charger_secrets_fichier():
    """Secours : lit SECRETS_BAZO_LOCAL.txt si .env absent."""
    if not os.path.isfile(_SECRETS_FILE):
        return
    mapping = {
        "BAZO_TOKEN_TELEGRAM": ("Token Telegram", "BAZO_TOKEN_TELEGRAM"),
        "BAZO_TOKEN_SNCF": ("Token API SNCF", "BAZO_TOKEN_SNCF"),
        "BAZO_ADMIN_CHAT_ID": ("Admin Telegram ID", "BAZO_ADMIN_CHAT_ID"),
        "BAZO_DB_PATH": ("Chemin", "BAZO_DB_PATH"),
        "BAZO_ADMIN_API_KEY": ("Clé API admin", "BAZO_ADMIN_API_KEY"),
        "BAZO_API_SECRET": ("Secret API", "BAZO_API_SECRET"),
    }
    with open(_SECRETS_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    for env_key, (label, _) in mapping.items():
        if os.environ.get(env_key):
            continue
        for i, line in enumerate(lines):
            if label in line and i + 1 < len(lines):
                val = lines[i + 1].strip()
                if val and not val.startswith("---") and not val.startswith("="):
                    os.environ[env_key] = val
                    break


_charger_dotenv()
_charger_secrets_fichier()

TOKEN_TELEGRAM = os.getenv("BAZO_TOKEN_TELEGRAM", "")
TOKEN_SNCF = os.getenv("BAZO_TOKEN_SNCF", "")
ADMIN_CHAT_ID = os.getenv("BAZO_ADMIN_CHAT_ID", "")
DB_PATH = os.getenv("BAZO_DB_PATH", os.path.join(_BASE_DIR, "sillon_lorrain.db"))
HEALTHCHECK_INTERVAL_SEC = int(os.getenv("BAZO_HEALTHCHECK_INTERVAL", "900"))
API_SECRET = os.getenv("BAZO_API_SECRET") or hashlib.sha256(
    f"bazo-api-{TOKEN_TELEGRAM}".encode()
).hexdigest()
PDF_TOKEN_TTL_SEC = int(os.getenv("BAZO_PDF_TOKEN_TTL", "3600"))
API_BASE_URL = os.getenv("BAZO_API_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
BACKUP_DIR = os.getenv("BAZO_BACKUP_DIR", os.path.join(_BASE_DIR, "backups_bazo"))
BACKUP_RETENTION_DAYS = int(os.getenv("BAZO_BACKUP_RETENTION_DAYS", "14"))
BACKUP_GZIP = os.getenv("BAZO_BACKUP_GZIP", "1") == "1"
DATABASE_URL = os.getenv("BAZO_DATABASE_URL", "").strip()
DB_BACKEND = "postgres" if DATABASE_URL else "sqlite"
ADMIN_API_KEY = os.getenv("BAZO_ADMIN_API_KEY", "").strip()
ADMIN_LOCALHOST_ONLY = os.getenv("BAZO_ADMIN_LOCALHOST", "0") == "1"
API_REQUIRE_AUTH = os.getenv("BAZO_API_REQUIRE_AUTH", "1") == "1"
API_DOCS_ENABLED = os.getenv("BAZO_API_DOCS", "0") == "1"
ALLOWED_HOSTS = os.getenv("BAZO_API_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
RADAR_REGIONS = [r.strip() for r in os.getenv("BAZO_RADAR_REGIONS", "grand_est").split(",") if r.strip()]

if not TOKEN_TELEGRAM or not TOKEN_SNCF:
    raise RuntimeError(
        "Secrets manquants. Copie .env.example vers .env et renseigne tes jetons."
    )


def make_session(pool=20):
    """Session HTTP partagée : pooling + retries sur erreurs transitoires."""
    import requests
    from requests.adapters import HTTPAdapter
    session = requests.Session()
    try:
        from urllib3.util.retry import Retry
        retry = Retry(
            total=1, connect=1, read=1, backoff_factor=0.6,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
    except Exception:
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def log_quota_sncf(response, source="sncf"):
    """Journalise les en-têtes quota/rate-limit de l'API SNCF Navitia si présents."""
    if response is None:
        return
    if getattr(response, "status_code", None) == 429:
        logger_quota.warning("[%s] HTTP 429 — quota API atteint", source)
        return
    headers = getattr(response, "headers", None)
    if not headers:
        return
    cles = [
        k for k in headers
        if any(x in k.lower() for x in ("rate", "quota", "remaining", "limit", "usage"))
    ]
    if cles:
        parts = ", ".join(f"{k}={headers[k]}" for k in sorted(cles))
        logger_quota.info("[%s] %s", source, parts)


def db_connect(timeout=15):
    """Connexion SQLite robuste : WAL + busy_timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=8000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn
