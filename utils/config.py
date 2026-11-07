"""الإعدادات من متغيرات البيئة (وملف .env اختياري). لا توجد أسرار مكتوبة في الكود.

الأولوية: متغيرات البيئة الفعلية > ملف .env > القيم الافتراضية.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """قارئ .env بسيط (KEY=VALUE) بدون مكتبات خارجية، ولا يستبدل متغيرات البيئة الموجودة."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _path(name: str, default: Path) -> Path:
    v = _get(name)
    return Path(v).expanduser() if v else default


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


ENV = _get("SANAD_ENV", "development").lower()          # development | production
IS_PROD = ENV == "production"

DB_PATH = _path("SANAD_DB_PATH", ROOT / "database" / "sanad.db")
BACKUP_DIR = _path("SANAD_BACKUP_DIR", ROOT / "backups")
IMAGES_DIR = _path("SANAD_IMAGES_DIR", ROOT / "invoice_images")

# كلمة مرور المدير العام الابتدائية (تُستخدم فقط عند إنشاء الحساب لأول مرة)
ADMIN_INITIAL_PASSWORD = _get("SANAD_ADMIN_PASSWORD")

# الجلسات
SESSION_IDLE_MINUTES = _int("SANAD_SESSION_IDLE_MINUTES", 30)        # خروج تلقائي بعد خمول
SESSION_MAX_HOURS = _int("SANAD_SESSION_MAX_HOURS", 12)              # أقصى عمر للجلسة
MAX_FAILED_LOGINS = _int("SANAD_MAX_FAILED_LOGINS", 5)
LOCKOUT_MINUTES = _int("SANAD_LOCKOUT_MINUTES", 10)

# تشفير كلمات المرور
PBKDF2_ITERATIONS = _int("SANAD_PBKDF2_ITERATIONS", 600_000)

KEEP_BACKUPS = _int("SANAD_KEEP_BACKUPS", 30)
