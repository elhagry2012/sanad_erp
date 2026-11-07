-- ============================================================
-- Sanad Business Advisory - ERP Schema (SQLite)
-- آمن للتشغيل المتكرر: كل الجداول IF NOT EXISTS ولا يُحذف شيء أبدًا.
-- أعمدة التدقيق (created_by / updated_by / updated_at) تُضاف تلقائيًا
-- للجداول القديمة عبر database/db.py -> migrate()
-- ============================================================
PRAGMA foreign_keys = ON;

-- ---------- المستخدمون والتدقيق ----------
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT,
    password_hash TEXT NOT NULL,                       -- salt$pbkdf2 (لا تُخزَّن كلمات المرور نصًا)
    role TEXT NOT NULL CHECK (role IN ('admin','partner')),
    is_active INTEGER DEFAULT 1,
    failed_attempts INTEGER DEFAULT 0,
    locked_until TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT
);
-- حساب admin واحد فقط مسموح في النظام
CREATE UNIQUE INDEX IF NOT EXISTS ux_single_admin ON users(role) WHERE role = 'admin';

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,          -- create / update / delete / login / login_failed
    entity TEXT,                   -- نوع السجل (purchase_invoice, sale, ...)
    entity_id INTEGER,
    client_id INTEGER,
    details TEXT,
    amount REAL                    -- قيمة العملية المالية لحساب حجم الشغل
);
CREATE INDEX IF NOT EXISTS idx_audit_user_ts ON audit_log(username, ts);

-- ---------- العملاء (المطاعم) ----------
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cuisine TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ---------- 1) الموردون والمشتريات ----------
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    phone TEXT,
    opening_balance REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    is_semi INTEGER DEFAULT 0,
    qty_on_hand REAL DEFAULT 0,
    avg_cost REAL DEFAULT 0,
    last_price REAL DEFAULT 0,
    min_qty REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchase_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_no TEXT,
    invoice_date TEXT NOT NULL,
    total REAL DEFAULT 0,
    paid REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchase_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL,
    unit_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    pay_date TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT
);

-- ---------- 2) المخزن وتتبع الأسعار ----------
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    move_date TEXT NOT NULL,
    move_type TEXT NOT NULL CHECK (move_type IN
        ('purchase','production_in','production_out','sale','waste','adjustment','transfer_in','transfer_out')),
    qty REAL NOT NULL,
    unit_cost REAL DEFAULT 0,
    ref_id INTEGER
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    price_date TEXT NOT NULL,
    unit_price REAL NOT NULL
);

-- ---------- 3 + 4) الوصفات ----------
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('semi','dish')),
    output_item_id INTEGER REFERENCES items(id),
    yield_qty REAL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recipe_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS production_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    batch_date TEXT NOT NULL,
    output_qty REAL NOT NULL,
    total_cost REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    category TEXT,
    price REAL NOT NULL,
    recipe_id INTEGER REFERENCES recipes(id)
);

-- ---------- 5) مبيعات الكاشير ----------
CREATE TABLE IF NOT EXISTS pos_sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    dish_id INTEGER NOT NULL REFERENCES dishes(id),
    sale_date TEXT NOT NULL,
    qty REAL NOT NULL,
    unit_price REAL NOT NULL
);

-- ---------- 6) الهالك ----------
CREATE TABLE IF NOT EXISTS waste_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    waste_date TEXT NOT NULL,
    item_id INTEGER REFERENCES items(id),
    dish_id INTEGER REFERENCES dishes(id),
    qty REAL NOT NULL,
    reason TEXT NOT NULL,
    cost REAL NOT NULL
);

-- ---------- 7) المصروفات ----------
CREATE TABLE IF NOT EXISTS expense_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    nature TEXT NOT NULL CHECK (nature IN ('fixed','variable'))
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    category_id INTEGER NOT NULL REFERENCES expense_categories(id),
    exp_date TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT
);

-- ---------- 8) الجرد ----------
CREATE TABLE IF NOT EXISTS stocktakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    count_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stocktake_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    system_qty REAL NOT NULL,
    actual_qty REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_date  ON pos_sales(client_id, sale_date);
CREATE INDEX IF NOT EXISTS idx_exp_date    ON expenses(client_id, exp_date);
CREATE INDEX IF NOT EXISTS idx_moves       ON stock_movements(client_id, item_id, move_date);
CREATE INDEX IF NOT EXISTS idx_price_hist  ON price_history(item_id, price_date);
