"""ترقيم المستندات: تسلسل مستقل لكل نوع مستند ولكل مطعم (فواتير الشراء، المرتجعات، التعديلات، الدفعات، ...).

- الرقم يُحجز داخل نفس معاملة إنشاء المستند (BEGIN IMMEDIATE تُسلسل الكتّاب)، فلا تكرار ولا فجوات بسبب التزامن،
  وإن فشل حفظ المستند يُلغى حجز الرقم معه.
- الصيغة: البادئة + (السنة- أو السنةالشهر- حسب نمط إعادة البدء) + رقم مُبطَّن بالأصفار، مثل PI-2026-00012.
- الفترة تُحسب من تاريخ المستند نفسه (فاتورة بتاريخ 2025 تأخذ رقمًا من تسلسل 2025 حتى لو أُدخلت لاحقًا).
- المدير العام يضبط البادئة والطول ونمط إعادة البدء ورقم البداية من «الضوابط المالية».
"""
from datetime import datetime, date

import pandas as pd

# النوع: (الاسم، الجدول، عمود التاريخ، البادئة الافتراضية)
DOC_SEQ = {
    "purchase_invoice": ("فاتورة شراء", "purchase_invoices", "invoice_date", "PI-"),
    "purchase_return": ("مرتجع مشتريات", "purchase_returns", "return_date", "PR-"),
    "purchase_adjustment": ("تعديل مباشر لفاتورة شراء", "purchase_adjustments", "adj_date", "PA-"),
    "supplier_payment": ("دفعة مورد", "supplier_payments", "pay_date", "SP-"),
    "transfer_voucher": ("إذن تحويل", "transfer_vouchers", "voucher_date", "TR-"),
    "production_batch": ("تشغيلة إنتاج", "production_batches", "batch_date", "PB-"),
    "expense": ("مصروف", "expenses", "exp_date", "EX-"),
    "waste": ("تسجيل هدر", "waste_log", "waste_date", "WS-"),
    "stocktake": ("جرد", "stocktakes", "count_date", "ST-"),
}
RESET_AR = {"yearly": "سنويًا", "monthly": "شهريًا", "never": "بدون إعادة (تسلسل مستمر)"}
DEFAULT_PADDING = 5
DEFAULT_RESET = "yearly"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _period(reset: str, day) -> str:
    d = str(day or date.today())[:10]
    if reset == "monthly":
        return d[:7]
    if reset == "yearly":
        return d[:4]
    return ""


def _render(prefix: str, reset: str, period: str, n: int, padding: int) -> str:
    mid = {"yearly": f"{period}-", "monthly": f"{period.replace('-', '')}-"}.get(reset, "")
    return f"{prefix}{mid}{n:0{int(padding)}d}"


def config(c, cid, doc_type) -> dict:
    """إعداد التسلسل (أو الافتراضي إن لم يضبطه المدير العام)."""
    if doc_type not in DOC_SEQ:
        raise ValueError("نوع مستند غير مدعوم للترقيم.")
    r = c.execute("SELECT prefix, padding, reset FROM doc_sequences WHERE client_id=? AND doc_type=?", (cid, doc_type)).fetchone()
    if r:
        return {"prefix": r[0] or "", "padding": int(r[1] or DEFAULT_PADDING), "reset": r[2] or DEFAULT_RESET}
    return {"prefix": DOC_SEQ[doc_type][3], "padding": DEFAULT_PADDING, "reset": DEFAULT_RESET}


def _taken(c, cid, doc_type, doc_no) -> bool:
    table = DOC_SEQ[doc_type][1]
    return c.execute(f"SELECT 1 FROM {table} WHERE client_id=? AND doc_no=?", (cid, doc_no)).fetchone() is not None


def next_no(c, cid, doc_type, day=None) -> str:
    """يحجز الرقم التالي داخل المعاملة الجارية c ويرجعه. يتخطى أي رقم مستخدم (بعد تغيير البادئة أو رقم البداية)."""
    cfg = config(c, cid, doc_type)
    period = _period(cfg["reset"], day)
    r = c.execute("SELECT last_no FROM doc_counters WHERE client_id=? AND doc_type=? AND period=?", (cid, doc_type, period)).fetchone()
    n = (int(r[0]) if r else 0) + 1
    while True:
        no = _render(cfg["prefix"], cfg["reset"], period, n, cfg["padding"])
        if not _taken(c, cid, doc_type, no):
            break
        n += 1
    c.execute("""INSERT INTO doc_counters(client_id,doc_type,period,last_no) VALUES(?,?,?,?)
                 ON CONFLICT(client_id,doc_type,period) DO UPDATE SET last_no=excluded.last_no""", (cid, doc_type, period, n))
    return no


def assign(c, cid, doc_type, doc_id, day=None) -> str:
    """يحجز رقمًا ويكتبه في عمود doc_no للمستند (بعد إدخاله)."""
    no = next_no(c, cid, doc_type, day)
    c.execute(f"UPDATE {DOC_SEQ[doc_type][1]} SET doc_no=? WHERE id=?", (no, doc_id))
    return no


def preview(c, cid, doc_type, day=None) -> str:
    """الرقم الذي سيأخذه المستند التالي (بدون حجز)."""
    cfg = config(c, cid, doc_type)
    period = _period(cfg["reset"], day)
    r = c.execute("SELECT last_no FROM doc_counters WHERE client_id=? AND doc_type=? AND period=?", (cid, doc_type, period)).fetchone()
    n = (int(r[0]) if r else 0) + 1
    while _taken(c, cid, doc_type, _render(cfg["prefix"], cfg["reset"], period, n, cfg["padding"])):
        n += 1
    return _render(cfg["prefix"], cfg["reset"], period, n, cfg["padding"])


def backfill(conn) -> int:
    """ترقيم المستندات القديمة (doc_no فارغ) بالترتيب الزمني. آمن للتكرار: يلمس الصفوف الفارغة فقط."""
    done = 0
    for doc_type, (_, table, dcol, _) in DOC_SEQ.items():
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "doc_no" not in cols or dcol not in cols:
            continue
        rows = conn.execute(f"SELECT id, client_id, {dcol} FROM {table} WHERE doc_no IS NULL ORDER BY {dcol}, id").fetchall()
        for rid, cid, day in rows:
            assign(conn, cid, doc_type, rid, day)
            done += 1
    return done


# ============================================================ إدارة الإعدادات (المدير العام)
def settings_df(cid) -> pd.DataFrame:
    from database.db import get_conn
    c = get_conn()
    try:
        out = []
        today = str(date.today())
        for t, (label, table, _, _) in DOC_SEQ.items():
            cfg = config(c, cid, t)
            period = _period(cfg["reset"], today)
            r = c.execute("SELECT last_no FROM doc_counters WHERE client_id=? AND doc_type=? AND period=?", (cid, t, period)).fetchone()
            count = c.execute(f"SELECT COUNT(*) FROM {table} WHERE client_id=?", (cid,)).fetchone()[0]
            out.append({"doc_type": t, "المستند": label, "البادئة": cfg["prefix"], "عدد الأرقام": cfg["padding"],
                        "إعادة البدء": cfg["reset"], "آخر رقم في الفترة الحالية": int(r[0]) if r else 0,
                        "الرقم التالي": preview(c, cid, t, today), "عدد المستندات": int(count)})
        return pd.DataFrame(out)
    finally:
        c.close()


def save_settings(cid, doc_type, prefix, padding, reset, user, start_at=None) -> None:
    """يحفظ إعداد تسلسل نوع مستند. start_at (اختياري) = رقم المستند التالي في الفترة الحالية (لا يُسمح بالرجوع لرقم مستخدم)."""
    from database.db import tx
    from utils.audit import log
    from utils.security import _require_admin
    _require_admin(user)
    if doc_type not in DOC_SEQ:
        raise ValueError("نوع مستند غير مدعوم.")
    prefix = (prefix or "").strip()
    if len(prefix) > 12 or any(ch.isspace() for ch in prefix):
        raise ValueError("البادئة حتى 12 حرفًا وبدون مسافات.")
    padding = int(padding)
    if not 1 <= padding <= 10:
        raise ValueError("عدد الأرقام بين 1 و10.")
    if reset not in RESET_AR:
        raise ValueError("نمط إعادة البدء غير صحيح.")
    with tx() as c:
        c.execute("""INSERT INTO doc_sequences(client_id,doc_type,prefix,padding,reset,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(client_id,doc_type) DO UPDATE SET prefix=excluded.prefix, padding=excluded.padding,
                     reset=excluded.reset, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                  (cid, doc_type, prefix, padding, reset, user, _now()))
        detail = f"{DOC_SEQ[doc_type][0]}: بادئة «{prefix}» - {padding} أرقام - إعادة البدء {RESET_AR[reset]}"
        if start_at:
            start_at = int(start_at)
            period = _period(reset, date.today())
            r = c.execute("SELECT last_no FROM doc_counters WHERE client_id=? AND doc_type=? AND period=?", (cid, doc_type, period)).fetchone()
            last = int(r[0]) if r else 0
            if start_at <= last:
                raise ValueError(f"رقم البداية يجب أن يكون أكبر من آخر رقم صادر في الفترة الحالية ({last}).")
            c.execute("""INSERT INTO doc_counters(client_id,doc_type,period,last_no) VALUES(?,?,?,?)
                         ON CONFLICT(client_id,doc_type,period) DO UPDATE SET last_no=excluded.last_no""",
                      (cid, doc_type, period, start_at - 1))
            detail += f" - يبدأ الترقيم من {start_at}"
        log(c, user, "update", "settings", None, f"ترقيم المستندات - {detail}", None, cid)


def label(doc_no, doc_id) -> str:
    """نص عرض موحّد: رقم المستند إن وُجد وإلا الرقم الداخلي."""
    if isinstance(doc_no, str) and doc_no:
        return doc_no
    return f"#{int(doc_id)}" if doc_id is not None and not pd.isna(doc_id) else "—"
