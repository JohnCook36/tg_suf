"""Local-only configuration. Never log user Telegram credentials."""
import os
from pathlib import Path


def load_dotenv(path: Path | None = None) -> None:
    path = path or (Path.cwd() / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"TG_API_ID", "TG_API_HASH", "TG_SUF_DATA_DIR", "ALICE_WEBHOOK_SECRET", "ALICE_ALLOWED_USER_ID", "ALICE_ALLOWED_APPLICATION_ID"}:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def data_dir() -> Path:
    load_dotenv()
    specified = os.getenv("TG_SUF_DATA_DIR", "").strip()
    target = Path(specified).expanduser() if specified else Path.cwd() / ".local"
    target.mkdir(parents=True, exist_ok=True)
    try:
        target.chmod(0o700)
    except OSError:
        pass
    return target


def api_credentials() -> tuple[int, str]:
    load_dotenv()
    raw_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if not raw_id.isdecimal() or not api_hash:
        raise ValueError("Укажи TG_API_ID и TG_API_HASH в файле .env.")
    return int(raw_id), api_hash
