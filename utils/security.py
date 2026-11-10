"""الأمان: التشفير، الجلسات، المستخدمون، والصلاحيات (RBAC). منطق بدون واجهة، قابل للاختبار.

- كلمات المرور: argon2id إن كانت مكتبة argon2-cffi مثبتة، وإلا PBKDF2-SHA256 بـ 600 ألف دورة.
  الهاشات القديمة تُقبل وتُرقّى تلقائيًا عند أول دخول ناجح.
- الجلسات: رموز عشوائية تُخزَّن مجزّأة في قاعدة البيانات، لها خمول وعمر أقصى، ويمكن إلغاؤها فورًا.
- الصلاحيات: تُفرض داخل طبقة الخدمات (guard) وليس فقط بإخفاء الصفحات، وكل رفض يُسجَّل.
"""
import functools
import hashlib
import hmac
import inspect
import re
import secrets
import sys
from datetime import datetime, timedelta

import pandas as pd

from database.db import tx, query
from utils import config
from utils.audit import log

ADMIN_USERNAME = "momar"
BAD_LOGIN = "اسم المستخدم أو كلمة المرور غير صحيحة."

# وحدات النظام التي يمكن للمدير تقييد الوصول إليها لكل مستخدم
MODULES = {
    "dashboard": "لوحة التحكم",
    "purchases": "المشتريات والموردون",
    "inventory": "المخزن والأسعار والجرد",
    "warehouses": "المخازن وأذون التحويل",
    "treasury": "الخزينة والبنك",
    "production": "التصنيع والوصفات",
    "sales": "مبيعات الكاشير",
    "expenses": "المصروفات",
    "waste": "الهدر والتالف",
    "menu": "هندسة القائمة",
    "clients": "العملاء (المطاعم)",
    "import": "الاستيراد الجماعي للبيانات",
    "reports": "مركز التقارير (الدخول للصفحة)",
    "rep_financial": "تقارير: مالية",
    "rep_tax": "تقارير: ضريبية وإقفال",
    "rep_inventory": "تقارير: مخزون وصلاحية وركود",
    "rep_management": "تقارير: إدارية وتحليلية",
}
_T, _F = True, False
# أدوار جاهزة: تُطبَّق كنقطة بداية ويمكن للمدير العام تعديلها لكل مستخدم. (عرض، تعديل)
ROLE_PRESETS = {
    "cashier": {"label": "كاشير", "modules": {"sales": (_T, _T), "waste": (_T, _T)}},
    "restaurant_manager": {"label": "مدير مطعم", "modules": {
        "dashboard": (_T, _F), "purchases": (_T, _T), "inventory": (_T, _T), "warehouses": (_T, _T), "production": (_T, _T),
        "sales": (_T, _T), "expenses": (_T, _T), "waste": (_T, _T), "menu": (_T, _F), "reports": (_T, _F),
        "rep_inventory": (_T, _F), "rep_management": (_T, _F)}},
    "finance_manager": {"label": "مدير مالي", "modules": {
        "dashboard": (_T, _F), "treasury": (_T, _T), "expenses": (_T, _T), "purchases": (_T, _T), "sales": (_T, _F),
        "inventory": (_T, _F), "menu": (_T, _F), "reports": (_T, _T), "rep_financial": (_T, _T), "rep_tax": (_T, _T),
        "rep_inventory": (_T, _F)}},
    "general_manager": {"label": "مدير عام (غير Admin)", "all": True, "modules": {}},
}
# وحدات حساسة: لا تشملها خاصية «كل الوحدات»؛ تُمنح بالاسم فقط (المدير العام يملكها دائمًا)
SENSITIVE_MODULES = {"import"}


class PermissionDenied(ValueError):
    """ترث من ValueError حتى تعرضها الشاشات كرسالة خطأ عادية."""


# ================================================================= كلمات المرور
try:  # اختياري: تحسين تلقائي إن توفرت المكتبة
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError
    _ph = PasswordHasher()
except Exception:  # noqa: BLE001
    _ph = None
    InvalidHashError = VerificationError = Exception

_WEAK = {"123456", "12345678", "123456789", "1234567890", "password", "qwerty", "qwerty123", "111111",
         "000000", "231291", "admin", "admin123", "letmein", "iloveyou", "welcome"}


def policy_error(password: str, username: str = "") -> str | None:
    pw = password or ""
    if len(pw) < 10:
        return "كلمة المرور يجب ألا تقل عن 10 أحرف."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "كلمة المرور يجب أن تحتوي على حروف وأرقام."
    low = pw.lower()
    if low in _WEAK or "password" in low or "123456" in low or (username and username.lower() in low):
        return "كلمة المرور ضعيفة أو متوقعة. اختر كلمة أصعب."
    return None


def hash_password(password: str) -> str:
    if _ph is not None:
        return _ph.hash(password)
    salt = secrets.token_hex(16)
    it = config.PBKDF2_ITERATIONS
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), it).hex()
    return f"pbkdf2_sha256${it}${salt}${dk}"


def _pbkdf2(password, salt_hex, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iterations).hex()


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        if stored.startswith("$argon2"):
            return _ph is not None and bool(_ph.verify(stored, password))
        if stored.startswith("pbkdf2_sha256$"):
            _, it, salt, dk = stored.split("$")
            return hmac.compare_digest(_pbkdf2(password, salt, int(it)), dk)
        salt, dk = stored.split("$", 1)           # الصيغة القديمة: salt$hash (200 ألف دورة)
        return hmac.compare_digest(_pbkdf2(password, salt, 200_000), dk)
    except (InvalidHashError, VerificationError, ValueError):
        return False


def needs_rehash(stored: str) -> bool:
    if _ph is not None:
        return (not stored.startswith("$argon2")) or _ph.check_needs_rehash(stored)
    if stored.startswith("pbkdf2_sha256$"):
        return int(stored.split("$")[1]) < config.PBKDF2_ITERATIONS
    return not stored.startswith("$argon2")


def generate_password() -> str:
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if policy_error(pw) is None:
            return pw


# ================================================================= الإدارة والإنشاء الأولي
def ensure_admin() -> None:
    """ينشئ حساب المدير العام لأول مرة فقط، ولا يلمس كلمة مروره بعد ذلك.

    كلمة المرور الابتدائية من SANAD_ADMIN_PASSWORD؛ إن لم تُحدَّد تُولَّد كلمة عشوائية وتُطبع في سجل الخادم مرة واحدة.
    في الحالتين يُطلب تغييرها عند أول دخول.
    """
    with tx() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone():
            return
        pw = config.ADMIN_INITIAL_PASSWORD
        generated = not pw
        if generated:
            pw = generate_password()
            print(f"[SANAD] Initial admin password for '{ADMIN_USERNAME}': {pw}  (change it at first login)",
                  file=sys.stderr, flush=True)
        c.execute("INSERT INTO users(username,full_name,password_hash,role,is_active,created_by,"
                  "must_change_password,password_changed_at) VALUES(?,?,?,?,1,'system',1,?)",
                  (ADMIN_USERNAME, "المدير العام", hash_password(pw), "admin", _now()))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse(ts):
    return datetime.fromisoformat(ts) if ts else None


# ================================================================= الدخول والجلسات
def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(username: str, password: str):
    """يرجع (بيانات المستخدم أو None، رسالة). لا ينشئ جلسة (انظر login)."""
    uname = (username or "").strip().lower()
    now = datetime.now()
    with tx() as c:
        row = c.execute(
            "SELECT id,username,full_name,password_hash,role,is_active,failed_attempts,locked_until,"
            "COALESCE(must_change_password,0) FROM users WHERE username=?", (uname,)).fetchone()
        if row is None:
            hash_password("timing-equalizer")            # يقارب زمن التحقق لمنع تخمين أسماء المستخدمين
            log(c, uname[:40] or "?", "login_failed", "session", details="مستخدم غير موجود")
            return None, BAD_LOGIN
        uid, uname, full, pw_hash, role, active, fails, locked, must = row
        if locked and now < datetime.fromisoformat(locked):
            log(c, uname, "login_failed", "session", details="حساب مقفل مؤقتًا")
            return None, f"الحساب مقفل مؤقتًا بسبب محاولات خاطئة. حاول بعد {config.LOCKOUT_MINUTES} دقائق."
        if not verify_password(password or "", pw_hash):
            fails = (fails or 0) + 1
            lock = None
            if fails >= config.MAX_FAILED_LOGINS:
                lock = (now + timedelta(minutes=config.LOCKOUT_MINUTES)).isoformat(timespec="seconds")
                fails = 0
            c.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?", (fails, lock, uid))
            log(c, uname, "login_failed", "session", details="كلمة مرور خاطئة")
            return None, BAD_LOGIN
        if not active:
            log(c, uname, "login_failed", "session", details="حساب معطّل")
            return None, "هذا الحساب معطّل. تواصل مع المدير العام."
        # دخول ناجح: ترقية الهاش القديم + إلزام تغيير كلمة المرور الضعيفة
        updates, params = ["failed_attempts=0", "locked_until=NULL", "last_login_at=?"], [_now()]
        if needs_rehash(pw_hash):
            updates.append("password_hash=?")
            params.append(hash_password(password))
        if policy_error(password, uname):
            updates.append("must_change_password=1")
            must = 1
        c.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", (*params, uid))
        log(c, uname, "login", "session")
    return {"id": uid, "username": uname, "full_name": full or uname, "role": role,
            "must_change": bool(must)}, "ok"


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    with tx() as c:
        c.execute("INSERT INTO sessions(token_hash,user_id,created_at,last_seen,expires_at) VALUES(?,?,?,?,?)",
                  (_sha(token), user_id, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                   (now + timedelta(hours=config.SESSION_MAX_HOURS)).isoformat(timespec="seconds")))
        c.execute("DELETE FROM sessions WHERE expires_at < ?", ((now - timedelta(days=2)).isoformat(timespec="seconds"),))
    return token


def validate_session(token: str | None):
    """يتحقق من الجلسة في كل إعادة تشغيل للصفحة: صلاحية، خمول، عمر أقصى، تعطيل المستخدم."""
    if not token:
        return None
    df = query("""SELECT s.last_seen, s.expires_at, s.revoked, u.id, u.username, u.full_name, u.role, u.is_active,
                  COALESCE(u.must_change_password,0) AS must FROM sessions s JOIN users u ON u.id=s.user_id
                  WHERE s.token_hash=?""", (_sha(token),))
    if df.empty:
        return None
    r = df.iloc[0]
    now = datetime.now()
    if r["revoked"] or not r["is_active"]:
        return None
    if now > _parse(r["expires_at"]) or now - _parse(r["last_seen"]) > timedelta(minutes=config.SESSION_IDLE_MINUTES):
        revoke_token(token)
        return None
    if now - _parse(r["last_seen"]) > timedelta(seconds=60):
        with tx() as c:
            c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now.isoformat(timespec="seconds"), _sha(token)))
    return {"id": int(r["id"]), "username": r["username"], "full_name": r["full_name"] or r["username"],
            "role": r["role"], "must_change": bool(r["must"])}


def revoke_token(token: str) -> None:
    with tx() as c:
        c.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (_sha(token),))


def log_logout(username: str) -> None:
    with tx() as c:
        log(c, username, "logout", "session")


def _revoke_user(c, user_id, keep_hash=None):
    if keep_hash:
        c.execute("UPDATE sessions SET revoked=1 WHERE user_id=? AND token_hash<>?", (user_id, keep_hash))
    else:
        c.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))


# ================================================================= الصلاحيات
def _denied(user, module, cid, why):
    with tx() as c:      # معاملة مستقلة: يُحفظ سجل المحاولة حتى لو أُلغيت العملية المرفوضة
        log(c, user or "?", "access_denied", "permission", None, f"{why} [{module}] عميل={cid}", None, cid)
    raise PermissionDenied(why)


def log_event(username: str, action: str, entity: str, details: str = "", client_id=None) -> None:
    """تسجيل حدث أمني من الواجهة (مثل رفض دخول مطعم غير مصرح به)."""
    with tx() as c:
        log(c, username or "?", action, entity, None, details, None, client_id)


def get_access(username: str) -> dict | None:
    df = query("SELECT id, role, is_active, all_modules, all_clients FROM users WHERE username=?", (username,))
    if df.empty:
        return None
    r = df.iloc[0]
    uid = int(r["id"])
    if r["role"] == "admin" and username == ADMIN_USERNAME:
        return {"admin": True, "active": bool(r["is_active"]), "all_modules": True, "modules": {}, "explicit": {},
                "all_clients": True, "clients": set()}
    all_m = True if pd.isna(r["all_modules"]) else bool(r["all_modules"])
    all_c = True if pd.isna(r["all_clients"]) else bool(r["all_clients"])
    explicit, clients = {}, set()
    for _, p in query("SELECT module, can_view, can_edit FROM user_permissions WHERE user_id=?", (uid,)).iterrows():
        explicit[p["module"]] = (bool(p["can_view"]) or bool(p["can_edit"]), bool(p["can_edit"]))
    if not all_c:
        clients = {int(x) for x in query("SELECT client_id FROM user_clients WHERE user_id=?", (uid,))["client_id"]}
    return {"admin": False, "active": bool(r["is_active"]), "all_modules": all_m, "modules": {} if all_m else explicit,
            "explicit": explicit, "all_clients": all_c, "clients": clients}


def module_access(a: dict, module: str) -> tuple[bool, bool]:
    """(عرض، تعديل) لوحدة معيّنة مع مراعاة الوحدات الحساسة."""
    if a["admin"]:
        return True, True
    if module in SENSITIVE_MODULES:
        return a["explicit"].get(module, (False, False))
    if a["all_modules"]:
        return True, True
    return a["modules"].get(module, (False, False))


def can_view(username: str, module: str) -> bool:
    a = get_access(username)
    if not a or not a["active"]:
        return False
    return module_access(a, module)[0]


def can_edit(username: str, module: str) -> bool:
    a = get_access(username)
    if not a or not a["active"]:
        return False
    return module_access(a, module)[1]


def allowed_client_ids(username: str):
    """None = كل المطاعم، وإلا مجموعة أرقام مسموحة."""
    a = get_access(username)
    if not a or not a["active"]:
        return set()
    return None if (a["admin"] or a["all_clients"]) else a["clients"]


def client_allowed(username: str, cid) -> bool:
    ids = allowed_client_ids(username)
    return ids is None or int(cid) in ids


def require(user: str, module: str, cid=None, edit: bool = True) -> None:
    """يفرض الصلاحية أو يسجّل محاولة غير مصرح بها ويرفع PermissionDenied."""
    if user == "system":
        return
    a = get_access(user)
    if a is None or not a["active"]:
        _denied(user, module, cid, "المستخدم غير معروف أو معطّل.")
    if a["admin"]:
        return
    v, e = module_access(a, module)
    if (edit and not e) or (not edit and not v):
        _denied(user, module, cid, "ليس لديك صلاحية " + ("التعديل" if edit else "الدخول") +
                f" في وحدة «{MODULES.get(module, module)}».")
    if cid is not None and not a["all_clients"] and int(cid) not in a["clients"]:
        _denied(user, module, cid, "ليس لديك صلاحية على هذا المطعم.")


def guard(module: str, edit: bool = True):
    """ديكوريتر لدوال الخدمات: يقرأ user وcid من وسائط الدالة ويفرض الصلاحية قبل التنفيذ."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*a, **k):
            b = sig.bind_partial(*a, **k).arguments
            require(b.get("user") or b.get("by"), module, b.get("cid", b.get("client_id")), edit)
            return fn(*a, **k)
        return wrapper
    return deco


def client_of(table: str, record_id) -> int | None:
    """عميل السجل (للتحقق من الصلاحية قبل تعديل سجل معروف برقمه)."""
    allowed = {"pos_sales", "expenses", "waste_log", "transfer_vouchers", "treasury_transactions"}
    if table not in allowed:
        raise ValueError("جدول غير مسموح.")
    df = query(f"SELECT client_id FROM {table} WHERE id=?", (record_id,))
    return int(df["client_id"].iloc[0]) if not df.empty else None


def _require_admin(by: str) -> None:
    if by != ADMIN_USERNAME:
        _denied(by, "admin", None, "هذه العملية للمدير العام فقط.")
    a = get_access(by)
    if not a or not a["active"]:
        _denied(by, "admin", None, "حساب المدير غير فعال.")


# ================================================================= إدارة الحسابات (المدير العام فقط)
def _apply_modules(c, uid, all_modules, modules):
    c.execute("UPDATE users SET all_modules=? WHERE id=?", (1 if all_modules else 0, uid))
    c.execute("DELETE FROM user_permissions WHERE user_id=?", (uid,))
    for m, (v, e) in (modules or {}).items():
        if m in MODULES and (not all_modules or m in SENSITIVE_MODULES):
            c.execute("INSERT INTO user_permissions(user_id,module,can_view,can_edit) VALUES(?,?,?,?)",
                      (uid, m, 1 if (v or e) else 0, 1 if e else 0))


def _apply_access(c, uid, all_modules, modules, all_clients, client_ids):
    _apply_modules(c, uid, all_modules, modules)
    c.execute("UPDATE users SET all_clients=? WHERE id=?", (1 if all_clients else 0, uid))
    c.execute("DELETE FROM user_clients WHERE user_id=?", (uid,))
    if not all_clients:
        for cid in (client_ids or []):
            c.execute("INSERT OR IGNORE INTO user_clients(user_id,client_id) VALUES(?,?)", (uid, int(cid)))


def apply_role(username: str, role_key: str, by: str) -> None:
    """يطبّق دورًا جاهزًا (كاشير/مدير مطعم/مدير مالي/مدير عام) على صلاحيات الوحدات. المطاعم المسموحة لا تتغير."""
    _require_admin(by)
    if username == ADMIN_USERNAME:
        raise ValueError("المدير العام لا يُغيَّر دوره.")
    if role_key not in ROLE_PRESETS:
        raise ValueError("دور غير معروف.")
    preset = ROLE_PRESETS[role_key]
    with tx() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise ValueError("المستخدم غير موجود.")
        _apply_modules(c, row[0], bool(preset.get("all")), preset["modules"])
        c.execute("UPDATE users SET job_role=? WHERE id=?", (role_key, row[0]))
        log(c, by, "update", "permission", row[0], f"{username}: تطبيق دور «{preset['label']}»")


def create_user(username: str, full_name: str, password: str, by: str, all_modules=True, modules=None,
                all_clients=True, client_ids=None, role: str | None = None) -> None:
    _require_admin(by)
    u = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.]{3,30}", u):
        raise ValueError("اسم المستخدم: 3 إلى 30 حرفًا إنجليزيًا صغيرًا أو أرقامًا أو _ أو .")
    if u == ADMIN_USERNAME:
        raise ValueError("اسم المستخدم محجوز.")
    err = policy_error(password, u)
    if err:
        raise ValueError(err)
    with tx() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
            raise ValueError("اسم المستخدم مستخدم بالفعل.")
        uid = c.execute("INSERT INTO users(username,full_name,password_hash,role,is_active,created_by,must_change_password,"
                        "password_changed_at) VALUES(?,?,?,'partner',1,?,1,?)",
                        (u, (full_name or "").strip(), hash_password(password), by, _now())).lastrowid
        if role:
            if role not in ROLE_PRESETS:
                raise ValueError("دور غير معروف.")
            all_modules, modules = bool(ROLE_PRESETS[role].get("all")), ROLE_PRESETS[role]["modules"]
            c.execute("UPDATE users SET job_role=? WHERE id=?", (role, uid))
        _apply_access(c, uid, all_modules, modules, all_clients, client_ids)
        log(c, by, "create", "user", uid, f"إنشاء حساب {u}" + (f" بدور {ROLE_PRESETS[role]['label']}" if role else ""))


def set_permissions(username: str, by: str, all_modules: bool, modules: dict, all_clients: bool, client_ids) -> None:
    _require_admin(by)
    if username == ADMIN_USERNAME:
        raise ValueError("صلاحيات المدير العام مطلقة ولا تُعدَّل.")
    with tx() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise ValueError("المستخدم غير موجود.")
        _apply_access(c, row[0], all_modules, modules, all_clients, client_ids)
        mods = "كل الوحدات" if all_modules else "؛ ".join(
            f"{MODULES.get(m, m)}={'تعديل' if e else 'عرض'}" for m, (v, e) in modules.items() if v or e) or "لا شيء"
        cl = "كل المطاعم" if all_clients else f"مطاعم: {sorted(int(x) for x in client_ids)}"
        log(c, by, "update", "permission", row[0], f"{username}: {mods} | {cl}")


def list_users() -> pd.DataFrame:
    return query("""SELECT id, username, full_name, role, job_role, is_active, COALESCE(all_modules,1) all_modules,
                    COALESCE(all_clients,1) all_clients, COALESCE(must_change_password,0) must_change_password,
                    password_changed_at, last_login_at, created_at, created_by FROM users ORDER BY id""")


def set_active(username: str, active: bool, by: str) -> None:
    _require_admin(by)
    if username == ADMIN_USERNAME:
        raise ValueError("لا يمكن تعطيل حساب المدير العام.")
    with tx() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise ValueError("المستخدم غير موجود.")
        c.execute("UPDATE users SET is_active=?, failed_attempts=0, locked_until=NULL WHERE id=?", (1 if active else 0, row[0]))
        if not active:
            _revoke_user(c, row[0])
        log(c, by, "update", "user", row[0], f"{'تفعيل' if active else 'تعطيل'} الحساب {username}")


def reset_password(username: str, by: str, new_password: str | None = None) -> str:
    """يعيّن كلمة مرور (مؤقتة تُولَّد إن لم تُحدَّد)، ويُلزم المستخدم بتغييرها، ويُنهي جلساته. يرجع الكلمة لتسليمها."""
    _require_admin(by)
    if username == ADMIN_USERNAME:
        raise ValueError("غيّر كلمة مرورك من صفحة حسابي.")
    pw = new_password or generate_password()
    err = policy_error(pw, username)
    if err:
        raise ValueError(err)
    with tx() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise ValueError("المستخدم غير موجود.")
        c.execute("UPDATE users SET password_hash=?, must_change_password=1, password_changed_at=?, failed_attempts=0, "
                  "locked_until=NULL WHERE id=?", (hash_password(pw), _now(), row[0]))
        _revoke_user(c, row[0])
        log(c, by, "update", "user", row[0], f"إعادة تعيين كلمة مرور {username} (مؤقتة، تغيير إجباري)")
    return pw


def revoke_sessions(username: str, by: str) -> None:
    _require_admin(by)
    with tx() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            _revoke_user(c, row[0])
            log(c, by, "update", "user", row[0], f"إنهاء كل جلسات {username}")


def active_sessions() -> pd.DataFrame:
    return query("""SELECT u.username, s.created_at, s.last_seen, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id
                    WHERE s.revoked=0 AND s.expires_at > ? ORDER BY s.last_seen DESC""", (_now(),))


def change_own_password(username: str, old: str, new: str, keep_token: str | None = None) -> None:
    err = policy_error(new, username)
    if err:
        raise ValueError(err)
    with tx() as c:
        row = c.execute("SELECT id, password_hash FROM users WHERE username=?", (username,)).fetchone()
        if not row or not verify_password(old or "", row[1]):
            log(c, username, "login_failed", "user", None, "تغيير كلمة المرور بكلمة حالية خاطئة")
            raise ValueError("كلمة المرور الحالية غير صحيحة.")
        if verify_password(new, row[1]):
            raise ValueError("كلمة المرور الجديدة مطابقة للحالية.")
        c.execute("UPDATE users SET password_hash=?, must_change_password=0, password_changed_at=? WHERE id=?",
                  (hash_password(new), _now(), row[0]))
        _revoke_user(c, row[0], _sha(keep_token) if keep_token else None)
        log(c, username, "update", "user", row[0], "تغيير كلمة المرور")
