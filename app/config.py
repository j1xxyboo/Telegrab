"""Runtime configuration, read from the environment (or a local .env file)."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the app has no extra dependency."""
    for candidate in (BASE_DIR / ".env", Path.cwd() / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        break


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Telegram application credentials (https://my.telegram.org) -------------
API_ID: int = _int("TELEGRAM_API_ID", 0)
API_HASH: str = os.environ.get("TELEGRAM_API_HASH", "").strip()

# --- Server -----------------------------------------------------------------
HOST: str = os.environ.get("TELE_HOST", "127.0.0.1")
PORT: int = _int("TELE_PORT", 8000)

# --- Storage ----------------------------------------------------------------
SESSIONS_DIR = Path(os.environ.get("TELE_SESSIONS_DIR", BASE_DIR / "sessions"))
CACHE_DIR = Path(os.environ.get("TELE_CACHE_DIR", BASE_DIR / "cache"))
LOG_FILE = Path(os.environ.get("TELE_LOG_FILE", BASE_DIR / "logs" / "tegrab.log"))
for _directory in (SESSIONS_DIR, CACHE_DIR, LOG_FILE.parent):
    _directory.mkdir(parents=True, exist_ok=True)

# --- Behaviour --------------------------------------------------------------
COOKIE_NAME = "tegrab_sid"
COOKIE_SECURE = os.environ.get("TELE_COOKIE_SECURE", "0") == "1"
COOKIE_MAX_AGE = _int("TELE_COOKIE_MAX_AGE", 30 * 86400)
SESSION_TTL = _int("TELE_SESSION_TTL", 12 * 3600)       # idle seconds before a client is dropped
DIALOG_LIMIT = _int("TELE_DIALOG_LIMIT", 500)
CHUNK_SIZE = _int("TELE_CHUNK_SIZE", 512 * 1024)        # download request size
THUMB_CACHE_MAX = _int("TELE_THUMB_CACHE_MAX", 512)     # max cached thumbnails on disk


PLACEHOLDER_API_ID = 1234567
PLACEHOLDER_API_HASH = "0123456789abcdef0123456789abcdef"


def credentials_ok() -> bool:
    if not API_ID or not API_HASH:
        return False
    if API_ID == PLACEHOLDER_API_ID or API_HASH == PLACEHOLDER_API_HASH:
        return False
    return True


def missing_credentials_message() -> str:
    if API_ID == PLACEHOLDER_API_ID or API_HASH == PLACEHOLDER_API_HASH:
        return (
            "The app is still using the placeholder values from .env.example. Put your real "
            f"api_id / api_hash in {BASE_DIR / '.env'} and restart the server."
        )
    return (
        "TELEGRAM_API_ID / TELEGRAM_API_HASH are not set. Create an application at "
        "https://my.telegram.org → API development tools, then put both values in "
        f"{BASE_DIR / '.env'} (see .env.example) and restart."
    )
