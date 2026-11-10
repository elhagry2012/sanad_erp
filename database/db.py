"""طبقة الاتصال بقاعدة البيانات + الترقية الآمنة + النسخ الاحتياطي.

- مسارات القاعدة والصور والنسخ تُقرأ من متغيرات البيئة (utils/config.py).
- كل عملية كتابة تُنفَّذ داخل tx(): معاملة BEGIN IMMEDIATE تُسلسل الكتّاب فلا تحدث سباقات
  على المخزون أو الأرصدة، وإما أن تُحفظ كاملة أو تُلغى كاملة.
- migrate() تضيف فقط ما ينقص (جداول/أعمدة) ولا تحذف أو تعدّل بيانات موجودة.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import pandas as pd

from utils import config

BASE = Path(__file__).resolve().parent
DB_PATH = config.DB_PATH
SCHEMA_PATH = BASE / "schema.sql"
BACKUP_DIR = config.BACKUP_DIR
KEEP_BACKUPS = config.KEEP_BACKUPS
IMAGES_DIR = config.IMAGES_DIR

# جداول تُضاف لها أعمدة التدقيق
AUDITED_TABLES = [
    "clients", "suppliers", "items", "purchase_invoices", "supplier_payments",
    "stock_movements", "recipes", "production_batches", "dishes", "pos_sales",
    "waste_log", "expense_categories", "expenses", "stocktakes",
]

EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','partner')),
    is_active INTEGER DEFAULT 1,
    failed_attempts INTEGER DEFAULT 0,
    locked_until TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_single_admin ON users(role) WHERE role = 'admin';
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id INTEGER,
    client_id INTEGER,
    details TEXT,
    amount REAL
);
CREATE INDEX IF NOT EXISTS idx_audit_user_ts ON audit_log(username, ts);

-- ===== صلاحيات المستخدمين =====
CREATE TABLE IF NOT EXISTS user_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module TEXT NOT NULL,
    can_view INTEGER NOT NULL DEFAULT 1,
    can_edit INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, module)
);
CREATE TABLE IF NOT EXISTS user_clients (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, client_id)
);
-- ===== جلسات الدخول (تُحفظ مجزّأة SHA-256، ويمكن إلغاؤها من لوحة المدير) =====
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- ===== الخزينة والبنك =====
CREATE TABLE IF NOT EXISTS treasury_settings (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),
    opening_balance REAL DEFAULT 0,
    updated_by TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS treasury_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    tx_date TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
    amount REAL NOT NULL CHECK (amount > 0),
    category TEXT NOT NULL,
    description TEXT,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_id INTEGER,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_treasury_date ON treasury_transactions(client_id, tx_date);
CREATE INDEX IF NOT EXISTS idx_treasury_src  ON treasury_transactions(source_type, source_id);

-- ===== المخازن والوحدات وأذون التحويل =====
CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ('main','production','other')),
    is_operations INTEGER NOT NULL DEFAULT 0,   -- مخزن التشغيل: تُخصم منه المبيعات والتصنيع
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (client_id, name)
);
CREATE TABLE IF NOT EXISTS warehouse_stock (
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (warehouse_id, item_id)
);
CREATE TABLE IF NOT EXISTS item_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    unit_name TEXT NOT NULL,
    factor REAL NOT NULL CHECK (factor > 0),     -- عدد الوحدات الأساسية في هذه الوحدة
    is_purchase_default INTEGER NOT NULL DEFAULT 0,
    UNIQUE (item_id, unit_name)
);
CREATE TABLE IF NOT EXISTS transfer_vouchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    voucher_date TEXT NOT NULL,
    from_wh INTEGER NOT NULL REFERENCES warehouses(id),
    to_wh INTEGER NOT NULL REFERENCES warehouses(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','cancelled')),
    notes TEXT,
    created_by TEXT,
    created_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    CHECK (from_wh <> to_wh)
);
CREATE TABLE IF NOT EXISTS transfer_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id INTEGER NOT NULL REFERENCES transfer_vouchers(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    entered_qty REAL NOT NULL,
    unit_name TEXT,
    unit_factor REAL NOT NULL DEFAULT 1,
    base_qty REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transfer_client ON transfer_vouchers(client_id, voucher_date);
"""

# أعمدة إضافية تُضاف تلقائيًا للجداول القديمة (كلها اختيارية/لها قيم افتراضية = توافق كامل مع البيانات الحالية)
EXTRA_COLUMNS = {
    "users": [("all_modules", "INTEGER DEFAULT 1"), ("all_clients", "INTEGER DEFAULT 1"),
              ("must_change_password", "INTEGER DEFAULT 0"), ("password_changed_at", "TEXT"),
              ("last_login_at", "TEXT")],
    "purchase_invoices": [("subtotal", "REAL"), ("vat_rate", "REAL"), ("vat_amount", "REAL"),
                          ("wht_rate", "REAL"), ("wht_amount", "REAL"), ("image_path", "TEXT"),
                          ("warehouse_id", "INTEGER"), ("pay_method", "TEXT")],
    "purchase_lines": [("unit_name", "TEXT"), ("unit_factor", "REAL")],
    "recipe_lines": [("entered_qty", "REAL"), ("entered_unit", "TEXT")],
    "pos_sales": [("treasury_tx_id", "INTEGER"), ("bank_tx_id", "INTEGER"), ("cash_amount", "REAL"),
                  ("bank_amount", "REAL"), ("channel", "TEXT"), ("channel_detail", "TEXT")],
    "stocktakes": [("warehouse_id", "INTEGER")],
    "waste_log": [("warehouse_id", "INTEGER")],
    "stock_movements": [("warehouse_id", "INTEGER")],
    "treasury_transactions": [("account", "TEXT DEFAULT 'cash'")],
    "treasury_settings": [("bank_opening_balance", "REAL DEFAULT 0")],
    "supplier_payments": [("pay_method", "TEXT")],
    "expenses": [("pay_method", "TEXT")],
}

_STOCK_MOVEMENTS_DDL = """
CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    move_date TEXT NOT NULL,
    move_type TEXT NOT NULL CHECK (move_type IN
        ('purchase','production_in','production_out','sale','waste','adjustment','transfer_in','transfer_out')),
    qty REAL NOT NULL,
    unit_cost REAL DEFAULT 0,
    ref_id INTEGER,
    created_by TEXT,
    updated_by TEXT,
    updated_at TEXT,
    warehouse_id INTEGER
)"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx():
    """معاملة كتابة واحدة. BEGIN IMMEDIATE تحجز القاعدة للكتابة من البداية
    فتُقرأ الأرصدة وتُعدَّل داخل نفس القفل (لا سباقات ولا كتابة جزئية)."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- الترقية
def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _rebuild_stock_movements(conn) -> None:
    """يوسّع قيد CHECK لأنواع الحركة (تحويل وارد/صادر). يحافظ على كل الصفوف الحالية."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='stock_movements'").fetchone()
    if not row or "transfer_in" in row[0]:
        return
    old_cols = _columns(conn, "stock_movements")
    new_cols = {"id", "client_id", "item_id", "move_date", "move_type", "qty", "unit_cost", "ref_id",
                "created_by", "updated_by", "updated_at", "warehouse_id"}
    common = [c for c in old_cols if c in new_cols]
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE stock_movements RENAME TO stock_movements_old")
        conn.execute(_STOCK_MOVEMENTS_DDL)
        cols = ",".join(common)
        conn.execute(f"INSERT INTO stock_movements({cols}) SELECT {cols} FROM stock_movements_old")
        conn.execute("DROP TABLE stock_movements_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_moves ON stock_movements(client_id, item_id, move_date)")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def ensure_defaults(conn) -> None:
    """يُنشئ مخازن افتراضية للعملاء الحاليين ويسند الأرصدة القديمة للمخزن الرئيسي (آمن للتكرار).

    العملاء القدامى: المخزن الرئيسي هو مخزن التشغيل، فيبقى سلوك النظام كما كان تمامًا.
    """
    for (cid,) in conn.execute("SELECT id FROM clients").fetchall():
        if not conn.execute("SELECT 1 FROM warehouses WHERE client_id=?", (cid,)).fetchone():
            conn.execute("INSERT INTO warehouses(client_id,name,kind,is_operations,created_by) "
                         "VALUES(?,?,?,1,'system')", (cid, "المخزن الرئيسي", "main"))
            conn.execute("INSERT INTO warehouses(client_id,name,kind,is_operations,created_by) "
                         "VALUES(?,?,?,0,'system')", (cid, "مخزن التشغيل / الإنتاج", "production"))
    main = ("(SELECT id FROM warehouses w WHERE w.client_id={t}.client_id AND w.kind='main' ORDER BY id LIMIT 1)")
    # أصناف بلا أي رصيد موزّع: كل رصيدها القديم يذهب للمخزن الرئيسي
    conn.execute("""INSERT INTO warehouse_stock(warehouse_id,item_id,qty)
        SELECT (SELECT id FROM warehouses w WHERE w.client_id=i.client_id AND w.kind='main' ORDER BY id LIMIT 1),
               i.id, i.qty_on_hand FROM items i
        WHERE NOT EXISTS (SELECT 1 FROM warehouse_stock s WHERE s.item_id=i.id)
          AND EXISTS (SELECT 1 FROM warehouses w WHERE w.client_id=i.client_id)""")
    for t in ("stock_movements", "stocktakes", "waste_log", "purchase_invoices"):
        conn.execute(f"UPDATE {t} SET warehouse_id={main.format(t=t)} WHERE warehouse_id IS NULL")
    conn.execute("UPDATE treasury_transactions SET account='cash' WHERE account IS NULL")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(EXTRA_DDL)
    for table in AUDITED_TABLES:
        have = set(_columns(conn, table))
        for col in ("created_by", "updated_by", "updated_at"):
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    for table, defs in EXTRA_COLUMNS.items():
        have = set(_columns(conn, table))
        for col, typ in defs:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    _rebuild_stock_movements(conn)
    try:  # منع تكرار اسم الصنف لنفس العميل (يُتجاهل إن وُجدت بيانات قديمة مكررة)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_items_name ON items(client_id, name)")
    except sqlite3.IntegrityError:
        pass
    ensure_defaults(conn)
    conn.commit()


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        migrate(conn)
    finally:
        conn.close()


def backup_db(force: bool = False) -> Path | None:
    """نسخة احتياطية آمنة (sqlite3.backup) باسم يحمل التاريخ والوقت.
    تلقائيًا: نسخة واحدة يوميًا. force=True: نسخة جديدة دائمًا. يُحتفظ بآخر KEEP_BACKUPS."""
    if not DB_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    existing = sorted(BACKUP_DIR.glob(f"sanad_{today}_*.db"))
    if existing and not force:
        return existing[-1]
    target = BACKUP_DIR / f"sanad_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    src = get_conn()
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(BACKUP_DIR.glob("sanad_*.db"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_conn()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()
def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # فحص وإضافة أي أعمدة ناقصة تلقائياً لتفادي أي أخطاء OperationalError
    def add_column_if_not_exists(table_name, column_name, column_definition):
        try:
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [col[1] for col in cursor.fetchall()]
            if columns and column_name not in columns:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
                conn.commit()
        except Exception as e:
            print(f"Note on {table_name}.{column_name}: {e}")

    # تفعيل الفحص على جدول فواتير المشتريات والحركات
    add_column_if_not_exists("purchase_invoices", "total", "REAL")
    add_column_if_not_exists("purchase_invoices", "subtotal", "REAL")
    add_column_if_not_exists("purchase_invoices", "vat_rate", "REAL")
    add_column_if_not_exists("purchase_invoices", "vat", "REAL")
    add_column_if_not_exists("purchase_invoices", "wht_rate", "REAL")
    add_column_if_not_exists("purchase_invoices", "saved_image", "TEXT")
    add_column_if_not_exists("purchase_invoices", "wh", "TEXT")
    add_column_if_not_exists("purchase_invoices", "pay_method", "TEXT")
    add_column_if_not_exists("purchase_invoices", "created_by", "TEXT")
    add_column_if_not_exists("purchase_invoices", "replaces_id", "INTEGER")
    add_column_if_not_exists("stock_movements", "lots", "TEXT")
    add_column_if_not_exists("production_batches", "is_voided", "INTEGER DEFAULT 0")
    add_column_if_not_exists("transfer_vouchers", "is_voided", "INTEGER DEFAULT 0")
    add_column_if_not_exists("transfer_vouchers", "void_reason", "TEXT")
    
    conn.close()