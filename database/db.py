"""طبقة الاتصال بقاعدة البيانات + الترقية الآمنة + النسخ الاحتياطي.

- مسارات القاعدة والصور والنسخ تُقرأ من متغيرات البيئة (utils/config.py).
- كل عملية كتابة تُنفَّذ داخل tx(): معاملة BEGIN IMMEDIATE تُسلسل الكتّاب فلا تحدث سباقات
  على المخزون أو الأرصدة، وإما أن تُحفظ كاملة أو تُلغى كاملة.
- migrate() تضيف فقط ما ينقص (جداول/أعمدة) ولا تحذف أو تعدّل بيانات موجودة.
"""
import logging
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
log = logging.getLogger(__name__)

# رقم إصدار البنية لضمان التوافق مع app.py
def schema_version() -> int:
    return 5

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
-- ===== جلسات الدخول =====
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
    is_operations INTEGER NOT NULL DEFAULT 0,
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
    factor REAL NOT NULL CHECK (factor > 0),
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

# أنواع حركة المخزون المسموح بها (يجب أن تطابق كل ما يمرره الكود إلى inventory.move/adj_move)
MOVE_TYPES = ("purchase", "production_in", "production_out", "sale", "waste", "adjustment",
              "transfer_in", "transfer_out", "purchase_return", "reversal", "opening", "purchase_adjust")

# جداول بنيتها مُدارة بالكامل هنا: تُنشأ إن لم توجد، وإن وُجدت بنية قديمة/يدوية ينقصها عمود أو مفتاح أجنبي
# أو قيد CHECK تُعاد بناؤها مع الحفاظ على البيانات (انظر _rebuild_if_needed).
MANAGED_TABLES = {
    "stock_movements": f"""
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    move_date TEXT NOT NULL,
    move_type TEXT NOT NULL CHECK (move_type IN ({",".join(f"'{t}'" for t in MOVE_TYPES)})),
    qty REAL NOT NULL,
    unit_cost REAL DEFAULT 0,
    ref_id INTEGER,
    created_by TEXT,
    updated_by TEXT,
    updated_at TEXT,
    warehouse_id INTEGER REFERENCES warehouses(id),
    lots TEXT
)""",
    # ===== دفعات المخزون (الصلاحية) =====
    "stock_lots": """
CREATE TABLE IF NOT EXISTS stock_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    qty_initial REAL,
    qty_remaining REAL NOT NULL DEFAULT 0,
    unit_cost REAL DEFAULT 0,
    expiry_date TEXT,
    received_date TEXT,
    source_type TEXT,
    source_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""",
    # ===== مرتجعات المشتريات =====
    "purchase_returns": """
CREATE TABLE IF NOT EXISTS purchase_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_id INTEGER REFERENCES purchase_invoices(id),
    warehouse_id INTEGER REFERENCES warehouses(id),
    return_no TEXT,
    return_date TEXT NOT NULL,
    reason TEXT,
    subtotal REAL NOT NULL DEFAULT 0,
    vat_rate REAL DEFAULT 0,
    vat_amount REAL NOT NULL DEFAULT 0,
    wht_rate REAL DEFAULT 0,
    wht_amount REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    net_credit REAL NOT NULL DEFAULT 0,
    refund_method TEXT NOT NULL DEFAULT 'none',
    refund_amount REAL NOT NULL DEFAULT 0,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','voided')),
    voided_by TEXT,
    voided_at TEXT,
    void_reason TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""",
    "purchase_return_lines": """
CREATE TABLE IF NOT EXISTS purchase_return_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id INTEGER NOT NULL REFERENCES purchase_returns(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    entered_qty REAL NOT NULL,
    unit_name TEXT,
    unit_factor REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL,
    base_qty REAL NOT NULL,
    lot_id INTEGER REFERENCES stock_lots(id)
)""",
    # ===== الضوابط: الإعدادات، إقفال الفترات، الإشعارات، طلبات التعديل =====
    "client_settings": """
CREATE TABLE IF NOT EXISTS client_settings (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (client_id, key)
)""",
    "period_locks": """
CREATE TABLE IF NOT EXISTS period_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    locked_by TEXT,
    locked_at TEXT,
    note TEXT,
    UNIQUE (client_id, period)
)""",
    "notifications": """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    kind TEXT,
    title TEXT NOT NULL,
    body TEXT,
    ref_type TEXT,
    ref_id INTEGER,
    is_read INTEGER NOT NULL DEFAULT 0,
    read_at TEXT
)""",
    "edit_requests": """
CREATE TABLE IF NOT EXISTS edit_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    doc_type TEXT NOT NULL,
    doc_id INTEGER NOT NULL,
    requested_by TEXT NOT NULL,
    reason TEXT,
    proposed TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT,
    decided_by TEXT,
    decision_note TEXT,
    decided_at TEXT,
    request_kind TEXT NOT NULL DEFAULT 'general',
    payload TEXT,
    result_ref INTEGER
)""",
    # ===== ترقيم المستندات: إعداد التسلسل لكل نوع مستند + العدّاد لكل فترة =====
    "doc_sequences": """
CREATE TABLE IF NOT EXISTS doc_sequences (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,
    prefix TEXT NOT NULL DEFAULT '',
    padding INTEGER NOT NULL DEFAULT 5,
    reset TEXT NOT NULL DEFAULT 'yearly' CHECK (reset IN ('never','yearly','monthly')),
    updated_by TEXT,
    updated_at TEXT,
    PRIMARY KEY (client_id, doc_type)
)""",
    "doc_counters": """
CREATE TABLE IF NOT EXISTS doc_counters (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '',
    last_no INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, doc_type, period)
)""",
    # ===== التعديل المباشر لفاتورة الشراء (تصحيح كميات/أسعار بدلًا من المرتجع) =====
    "purchase_adjustments": """
CREATE TABLE IF NOT EXISTS purchase_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    invoice_id INTEGER NOT NULL REFERENCES purchase_invoices(id),
    doc_no TEXT,
    adj_date TEXT NOT NULL,
    reason TEXT,
    request_id INTEGER REFERENCES edit_requests(id),
    requested_by TEXT,
    subtotal_before REAL NOT NULL DEFAULT 0,
    total_before REAL NOT NULL DEFAULT 0,
    net_before REAL NOT NULL DEFAULT 0,
    subtotal_after REAL NOT NULL DEFAULT 0,
    total_after REAL NOT NULL DEFAULT 0,
    net_after REAL NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT
)""",
    "purchase_adjustment_lines": """
CREATE TABLE IF NOT EXISTS purchase_adjustment_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adjustment_id INTEGER NOT NULL REFERENCES purchase_adjustments(id) ON DELETE CASCADE,
    line_id INTEGER NOT NULL REFERENCES purchase_lines(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    unit_name TEXT,
    unit_factor REAL NOT NULL DEFAULT 1,
    old_qty REAL NOT NULL,
    new_qty REAL NOT NULL,
    old_price REAL NOT NULL,
    new_price REAL NOT NULL,
    base_delta REAL NOT NULL DEFAULT 0,
    value_delta REAL NOT NULL DEFAULT 0
)""",
    "import_batches": """
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    kind TEXT NOT NULL,
    filename TEXT,
    file_sha256 TEXT,
    rows_total INTEGER,
    created_count INTEGER,
    skipped_count INTEGER,
    summary TEXT,
    created_by TEXT,
    created_at TEXT
)""",
}

# أسماء أعمدة قديمة (من جداول أُنشئت يدويًا) تُنقل بياناتها إلى الاسم الجديد عند إعادة البناء
REBUILD_RENAMES = {
    "purchase_returns": {"client_id": "restaurant_id"},
    "notifications": {"client_id": "restaurant_id", "body": "message"},
    "edit_requests": {"doc_type": "ref_type", "doc_id": "ref_id", "requested_by": "user_id",
                      "decided_by": "handled_by", "decided_at": "handled_at"},
}

MANAGED_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_moves ON stock_movements(client_id, item_id, move_date);
CREATE INDEX IF NOT EXISTS idx_moves_ref ON stock_movements(move_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_lots_item_wh ON stock_lots(item_id, warehouse_id);
CREATE INDEX IF NOT EXISTS idx_lots_expiry ON stock_lots(client_id, expiry_date);
CREATE INDEX IF NOT EXISTS idx_purchase_returns_client ON purchase_returns(client_id, return_date);
CREATE INDEX IF NOT EXISTS idx_purchase_returns_invoice ON purchase_returns(invoice_id);
CREATE INDEX IF NOT EXISTS idx_purchase_return_lines_ret ON purchase_return_lines(return_id);
CREATE INDEX IF NOT EXISTS idx_notifications_target ON notifications(target, is_read);
CREATE INDEX IF NOT EXISTS idx_edit_requests_doc ON edit_requests(doc_type, doc_id, status);
CREATE INDEX IF NOT EXISTS idx_import_batches_client ON import_batches(client_id, kind);
CREATE INDEX IF NOT EXISTS idx_purchase_adj_invoice ON purchase_adjustments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_purchase_adj_lines ON purchase_adjustment_lines(adjustment_id);
"""

# جداول المستندات المرقّمة (عمود doc_no + فهرس فريد لكل مطعم). المصدر: utils/sequences.DOC_SEQ
DOC_NO_TABLES = ("purchase_invoices", "purchase_returns", "purchase_adjustments", "supplier_payments", "transfer_vouchers",
                 "production_batches", "expenses", "waste_log", "stocktakes")


# أعمدة إضافية تُضاف تلقائيًا للجداول القديمة
EXTRA_COLUMNS = {
    "users": [("all_modules", "INTEGER DEFAULT 1"), ("all_clients", "INTEGER DEFAULT 1"),
              ("must_change_password", "INTEGER DEFAULT 0"), ("password_changed_at", "TEXT"),
              ("last_login_at", "TEXT"), ("job_role", "TEXT")],
    "items": [("track_expiry", "INTEGER DEFAULT 0"), ("shelf_life_days", "INTEGER DEFAULT 0"), ("barcode", "TEXT")],
    "dishes": [("barcode", "TEXT")],
    # ملاحظة: ADD COLUMN يقبل REFERENCES (القيمة الافتراضية NULL)، فتحصل القواعد الجديدة على المفتاح الأجنبي.
    "purchase_invoices": [("subtotal", "REAL"), ("vat_rate", "REAL"), ("vat_amount", "REAL"),
                          ("wht_rate", "REAL"), ("wht_amount", "REAL"), ("image_path", "TEXT"),
                          ("warehouse_id", "INTEGER REFERENCES warehouses(id)"), ("pay_method", "TEXT"),
                          ("replaces_id", "INTEGER REFERENCES purchase_invoices(id)"),
                          ("status", "TEXT DEFAULT 'active'"), ("voided_by", "TEXT"), ("voided_at", "TEXT"),
                          ("void_reason", "TEXT")],
    "purchase_lines": [("unit_name", "TEXT"), ("unit_factor", "REAL"), ("expiry_date", "TEXT")],
    "recipe_lines": [("entered_qty", "REAL"), ("entered_unit", "TEXT")],
    "pos_sales": [("treasury_tx_id", "INTEGER"), ("bank_tx_id", "INTEGER"), ("cash_amount", "REAL"),
                  ("bank_amount", "REAL"), ("channel", "TEXT"), ("channel_detail", "TEXT")],
    "stocktakes": [("warehouse_id", "INTEGER REFERENCES warehouses(id)")],
    "waste_log": [("warehouse_id", "INTEGER REFERENCES warehouses(id)")],
    "treasury_transactions": [("account", "TEXT DEFAULT 'cash'")],
    "treasury_settings": [("bank_opening_balance", "REAL DEFAULT 0")],
    "supplier_payments": [("pay_method", "TEXT")],
    "expenses": [("pay_method", "TEXT")],
    "production_batches": [("is_voided", "INTEGER DEFAULT 0"), ("voided_by", "TEXT"), ("voided_at", "TEXT")],
    "transfer_vouchers": [("is_voided", "INTEGER DEFAULT 0"), ("voided_by", "TEXT"), ("voided_at", "TEXT"),
                          ("void_reason", "TEXT")],
    "price_history": [("ref_id", "INTEGER")],
}
for _t in DOC_NO_TABLES:
    EXTRA_COLUMNS.setdefault(_t, []).append(("doc_no", "TEXT"))


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx():
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


def _columns(conn, table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.OperationalError:
        return []


def _shape(conn, table):
    """(أعمدة {الاسم: (النوع، NOT NULL، افتراضي)}، مجموعة المفاتيح الأجنبية (عمود، جدول، عمود الهدف، ON DELETE))."""
    cols = {r[1]: (r[2].upper(), r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")}
    fks = {(r[3], r[2], r[4] or "id", r[6]) for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
    return cols, fks


def _needs_rebuild(conn, table, ddl) -> bool:
    have_cols, have_fks = _shape(conn, table)
    if not have_cols:
        return False                                          # الجدول غير موجود؛ سيُنشأ من DDL مباشرة
    ref = sqlite3.connect(":memory:")
    try:
        ref.execute(ddl)
        want_cols, want_fks = _shape(ref, table)
    finally:
        ref.close()
    if want_fks - have_fks:
        return True                                           # مفتاح أجنبي ناقص
    for name, (typ, notnull, _dflt) in want_cols.items():
        if name not in have_cols or have_cols[name][0] != typ:
            return True                                       # عمود ناقص أو نوعه مختلف (مثل client_id TEXT)
    for name, (_typ, notnull, dflt) in have_cols.items():
        if name not in want_cols and notnull and dflt is None:
            return True                                       # عمود قديم إجباري لا يملؤه الكود فيُفشل كل إدخال
    if table == "stock_movements":
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        if any(f"'{t}'" not in sql for t in MOVE_TYPES):
            return True                                       # قيد CHECK لا يسمح بكل أنواع الحركة
    return False


def _rebuild_table(conn, table, ddl) -> None:
    """إعادة بناء جدول بالبنية الصحيحة مع نقل كل الصفوف (الطريقة الموصى بها في توثيق SQLite لتعديل الجداول).

    تُطفأ المفاتيح الأجنبية أثناء النقل ثم يُتحقق منها؛ إن وُجدت صفوف يتيمة تُلغى العملية ويبقى الجدول القديم كما هو.
    """
    tmp = f"{table}__new"
    old_cols = _columns(conn, table)
    new_ddl = ddl.replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {tmp} (", 1)
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")                 # لا يعمل داخل معاملة
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(new_ddl)
            new_cols = _columns(conn, tmp)
            renames = REBUILD_RENAMES.get(table, {})
            dst, src = [], []
            for col in new_cols:
                if col in old_cols and col in renames and renames[col] in old_cols:
                    dst.append(col); src.append(f"COALESCE({col}, {renames[col]})")
                elif col in old_cols:
                    dst.append(col); src.append(col)
                elif col in renames and renames[col] in old_cols:
                    dst.append(col); src.append(renames[col])
            if dst:
                conn.execute(f"INSERT INTO {tmp}({','.join(dst)}) SELECT {','.join(src)} FROM {table}")
            bad = conn.execute(f"PRAGMA foreign_key_check({tmp})").fetchall()
            if bad:
                raise sqlite3.IntegrityError(f"{table}: {len(bad)} صف يشير إلى سجلات غير موجودة")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _rebuild_managed_tables(conn) -> None:
    for table, ddl in MANAGED_TABLES.items():
        try:
            if _needs_rebuild(conn, table, ddl):
                _rebuild_table(conn, table, ddl)
                log.info("أُعيد بناء الجدول %s بالبنية الصحيحة", table)
        except Exception:
            log.exception("تعذّرت إعادة بناء الجدول %s؛ بقي كما هو", table)
    conn.executescript(MANAGED_INDEXES)                       # DROP TABLE يحذف الفهارس؛ نعيد إنشاءها


def ensure_defaults(conn) -> None:
    for (cid,) in conn.execute("SELECT id FROM clients").fetchall():
        if not conn.execute("SELECT 1 FROM warehouses WHERE client_id=?", (cid,)).fetchone():
            conn.execute("INSERT INTO warehouses(client_id,name,kind,is_operations,created_by) "
                         "VALUES(?,?,?,1,'system')", (cid, "المخزن الرئيسي", "main"))
            conn.execute("INSERT INTO warehouses(client_id,name,kind,is_operations,created_by) "
                         "VALUES(?,?,?,0,'system')", (cid, "مخزن التشغيل / الإنتاج", "production"))
    main = ("(SELECT id FROM warehouses w WHERE w.client_id={t}.client_id AND w.kind='main' ORDER BY id LIMIT 1)")
    conn.execute("""INSERT INTO warehouse_stock(warehouse_id,item_id,qty)
        SELECT (SELECT id FROM warehouses w WHERE w.client_id=i.client_id AND w.kind='main' ORDER BY id LIMIT 1),
               i.id, i.qty_on_hand FROM items i
        WHERE NOT EXISTS (SELECT 1 FROM warehouse_stock s WHERE s.item_id=i.id)
          AND EXISTS (SELECT 1 FROM warehouses w WHERE w.client_id=i.client_id)""")
    for t in ("stock_movements", "stocktakes", "waste_log", "purchase_invoices"):
        try:
            conn.execute(f"UPDATE {t} SET warehouse_id={main.format(t=t)} WHERE warehouse_id IS NULL")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("UPDATE treasury_transactions SET account='cash' WHERE account IS NULL")
    except sqlite3.OperationalError:
        pass


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(EXTRA_DDL)
    conn.executescript(";\n".join(MANAGED_TABLES.values()) + ";")
    _rebuild_managed_tables(conn)
    for table in AUDITED_TABLES:
        have = set(_columns(conn, table))
        if not have:
            continue
        for col in ("created_by", "updated_by", "updated_at"):
            if col not in have:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
    for table, defs in EXTRA_COLUMNS.items():
        have = set(_columns(conn, table))
        if not have:
            continue
        for col, typ in defs:
            if col not in have:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
    conn.commit()
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_items_name ON items(client_id, name)")
    except sqlite3.IntegrityError:
        pass
    ensure_defaults(conn)
    conn.commit()
    for t in ("items", "dishes"):                             # الباركود فريد لكل مطعم (الفارغ مسموح بلا حدود)
        try:
            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{t}_barcode ON {t}(client_id, barcode) "
                         "WHERE barcode IS NOT NULL AND barcode <> ''")
        except sqlite3.IntegrityError:
            log.exception("باركود مكرر في %s؛ لم يُنشأ الفهرس الفريد", t)
    conn.commit()
    _ensure_lots(conn)
    _ensure_doc_numbers(conn)


def _ensure_lots(conn) -> None:
    """كل المخزون بالدفعات: الأرصدة القديمة خارج الدفعات تتحول إلى دفعات افتتاحية بلا تاريخ صلاحية (مرة واحدة)."""
    from utils import inventory
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = inventory.backfill_lots(conn)
        conn.commit()
        if n:
            log.info("أُنشئت %s دفعة افتتاحية للأرصدة خارج الدفعات", n)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        log.exception("تعذّر تحويل الأرصدة القديمة إلى دفعات")


def _ensure_doc_numbers(conn) -> None:
    """ترقيم المستندات القديمة ثم فهرس فريد (المطعم، رقم المستند) لكل جدول مستندات."""
    from utils import sequences
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = sequences.backfill(conn)
        conn.commit()
        if n:
            log.info("رُقّم %s مستند قديم", n)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        log.exception("تعذّر ترقيم المستندات القديمة")
    for t in DOC_NO_TABLES:
        if "doc_no" in _columns(conn, t):
            try:
                conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{t}_doc_no ON {t}(client_id, doc_no) WHERE doc_no IS NOT NULL")
            except sqlite3.IntegrityError:
                log.exception("أرقام مستندات مكررة في %s؛ لم يُنشأ الفهرس الفريد", t)
    conn.commit()


def init_db() -> None:
    conn = get_conn()
    try:
        if SCHEMA_PATH.exists():
            try:
                conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        migrate(conn)
    finally:
        conn.close()


def backup_db(force: bool = False) -> Path | None:
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