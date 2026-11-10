"""الضوابط: إقفال الفترات، الإعدادات، الإشعارات، تنبيهات الحركات الكبيرة، وطلبات التعديل بموافقة المدير العام."""
import re
from datetime import datetime

import pandas as pd

from database.db import tx, query
from utils.audit import log
from utils.security import require, _require_admin, ADMIN_USERNAME

THRESHOLDS = {"thr_expense": "مصروف", "thr_purchase": "فاتورة شراء", "thr_payment": "دفعة لمورد",
              "thr_treasury_out": "سحب يدوي من الخزينة/البنك"}
DOC_TYPES = {  # النوع: (الاسم، الجدول، الوحدة)
    "purchase_invoice": ("فاتورة شراء", "purchase_invoices", "purchases"),
    "purchase_return": ("مرتجع مشتريات", "purchase_returns", "purchases"),
    "transfer_voucher": ("إذن تحويل بين المخازن", "transfer_vouchers", "warehouses"),
    "production_batch": ("تشغيلة إنتاج", "production_batches", "production"),
    "sale": ("سطر مبيعات", "pos_sales", "sales"),
    "expense": ("مصروف", "expenses", "expenses"),
    "waste": ("تسجيل هدر", "waste_log", "waste"),
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================ إقفال الفترات
def period_of(day) -> str:
    return str(day)[:7]


def check_open(c, cid, day) -> None:
    """يرفض أي إضافة/تعديل/إلغاء بتاريخ داخل فترة مقفلة (للجميع، والمدير العام يفتحها أولًا إن أراد)."""
    p = period_of(day)
    r = c.execute("SELECT locked_by, locked_at FROM period_locks WHERE client_id=? AND period=?", (cid, p)).fetchone()
    if r:
        raise ValueError(f"الفترة {p} مقفلة (بواسطة {r[0]} في {r[1]}): لا يمكن إضافة أو تعديل أو إلغاء مستندات بتاريخها. "
                         "يفتحها المدير العام من «الضوابط المالية» إن لزم.")


def lock_period(cid, period, user, note="") -> None:
    _require_admin(user)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period or ""):
        raise ValueError("الفترة بصيغة YYYY-MM.")
    with tx() as c:
        if c.execute("SELECT 1 FROM period_locks WHERE client_id=? AND period=?", (cid, period)).fetchone():
            raise ValueError("الفترة مقفلة بالفعل.")
        c.execute("INSERT INTO period_locks(client_id,period,locked_by,locked_at,note) VALUES(?,?,?,?,?)", (cid, period, user, _now(), note))
        log(c, user, "lock", "period", None, f"إقفال {period}" + (f" - {note}" if note else ""), None, cid)


def unlock_period(cid, period, user) -> None:
    _require_admin(user)
    with tx() as c:
        n = c.execute("DELETE FROM period_locks WHERE client_id=? AND period=?", (cid, period)).rowcount
        if not n:
            raise ValueError("الفترة غير مقفلة.")
        log(c, user, "unlock", "period", None, f"فتح {period}", None, cid)


def list_locks(cid) -> pd.DataFrame:
    return query("SELECT period, locked_by, locked_at, note FROM period_locks WHERE client_id=? ORDER BY period DESC", (cid,))


# ============================================================ الإعدادات
def get_setting(c, cid, key, default=None):
    if c is None:
        df = query("SELECT value FROM client_settings WHERE client_id=? AND key=?", (cid, key))
        return df["value"].iloc[0] if not df.empty else default
    r = c.execute("SELECT value FROM client_settings WHERE client_id=? AND key=?", (cid, key)).fetchone()
    return r[0] if r else default


def set_setting(cid, key, value, user) -> None:
    _require_admin(user)
    with tx() as c:
        c.execute("INSERT INTO client_settings(client_id,key,value) VALUES(?,?,?) ON CONFLICT(client_id,key) DO UPDATE SET value=excluded.value",
                  (cid, key, str(value)))
        log(c, user, "update", "settings", None, f"{key} = {value}", None, cid)


# ============================================================ الإشعارات
def notify(c, target, kind, title, body="", cid=None, ref=None) -> None:
    c.execute("INSERT INTO notifications(created_at,target,client_id,kind,title,body,ref_type,ref_id) VALUES(?,?,?,?,?,?,?,?)",
              (_now(), target, cid, kind, title, body, ref[0] if ref else None, ref[1] if ref else None))


def notify_if_large(c, cid, key, amount, what, user) -> None:
    """إشعار فوري للمدير العام إذا تجاوزت الحركة الحد المضبوط لهذا المطعم (0 = معطّل)."""
    try:
        thr = float(get_setting(c, cid, key, 0) or 0)
    except ValueError:
        return
    if thr > 0 and amount >= thr:
        name = c.execute("SELECT name FROM clients WHERE id=?", (cid,)).fetchone()
        notify(c, "admin", "large_transaction", f"حركة مالية كبيرة: {what}",
               f"{user} سجّل {what} بقيمة {amount:,.2f} (الحد {thr:,.2f}) — {name[0] if name else ''}", cid)


def inbox(username, is_admin, limit=200) -> pd.DataFrame:
    return query("""SELECT id, created_at, kind, title, body, is_read, client_id, ref_type, ref_id FROM notifications
                    WHERE target=? OR (? AND target='admin') ORDER BY id DESC LIMIT ?""", (username, 1 if is_admin else 0, limit))


def unread_count(username, is_admin) -> int:
    return int(query("SELECT COUNT(*) n FROM notifications WHERE is_read=0 AND (target=? OR (? AND target='admin'))",
                     (username, 1 if is_admin else 0))["n"].iloc[0])


def mark_read(username, is_admin, ids=None) -> None:
    with tx() as c:
        base = "UPDATE notifications SET is_read=1, read_at=? WHERE is_read=0 AND (target=? OR (? AND target='admin'))"
        params = [_now(), username, 1 if is_admin else 0]
        if ids:
            base += " AND id IN (%s)" % ",".join("?" * len(ids))
            params += [int(i) for i in ids]
        c.execute(base, params)


# ============================================================ طلبات التعديل
def create_edit_request(cid, doc_type, doc_id, reason, proposed, user) -> int:
    if doc_type not in DOC_TYPES:
        raise ValueError("نوع مستند غير مدعوم.")
    label, table, module = DOC_TYPES[doc_type]
    if not (reason or "").strip():
        raise ValueError("سبب الطلب مطلوب.")
    require(user, module, cid, edit=False)                       # يكفي أن يرى المستند
    with tx() as c:
        row = c.execute(f"SELECT client_id FROM {table} WHERE id=?", (doc_id,)).fetchone()
        if not row or row[0] != cid:
            raise ValueError("المستند غير موجود لهذا المطعم.")
        if c.execute("SELECT 1 FROM edit_requests WHERE doc_type=? AND doc_id=? AND status='pending'", (doc_type, doc_id)).fetchone():
            raise ValueError("يوجد طلب تعديل معلّق لهذا المستند بالفعل.")
        rid = c.execute("INSERT INTO edit_requests(client_id,doc_type,doc_id,requested_by,reason,proposed,created_at) VALUES(?,?,?,?,?,?,?)",
                        (cid, doc_type, doc_id, user, reason.strip(), (proposed or "").strip(), _now())).lastrowid
        notify(c, "admin", "edit_request", f"طلب تعديل: {label} #{doc_id}", f"{user}: {reason.strip()}", cid, ("edit_request", rid))
        log(c, user, "create", "edit_request", rid, f"{label} #{doc_id}: {reason.strip()}", None, cid)
    return rid


def requests_df(status=None, user=None, cid=None) -> pd.DataFrame:
    sql = "SELECT r.*, c.name AS client FROM edit_requests r JOIN clients c ON c.id=r.client_id WHERE 1=1"
    params = []
    if status:
        sql += " AND r.status=?"; params.append(status)
    if user:
        sql += " AND r.requested_by=?"; params.append(user)
    if cid:
        sql += " AND r.client_id=?"; params.append(cid)
    df = query(sql + " ORDER BY r.id DESC", tuple(params))
    if not df.empty:
        df["doc_label"] = df["doc_type"].map(lambda t: DOC_TYPES.get(t, (t,))[0])
    return df


def _execute_void(doc_type, doc_id, cid, reason, by, allow_negative):
    from utils import documents, services
    if doc_type == "purchase_invoice":
        documents.void_purchase(cid, doc_id, reason, by, allow_negative)
    elif doc_type == "purchase_return":
        documents.void_return(cid, doc_id, reason, by)
    elif doc_type == "transfer_voucher":
        documents.void_voucher(cid, doc_id, reason, by, allow_negative)
    elif doc_type == "production_batch":
        documents.void_batch(cid, doc_id, reason, by, allow_negative)
    elif doc_type == "sale":
        services.delete_sale(doc_id, by, cid)
    elif doc_type == "expense":
        services.delete_expense(doc_id, by, cid)
    elif doc_type == "waste":
        services.delete_waste(doc_id, by, cid)


def decide_request(req_id, approve, note, by, execute=False, allow_negative=False) -> None:
    """المدير العام يقبل/يرفض. عند القبول مع execute يُنفَّذ الإلغاء/العكس فورًا (وإن فشل يبقى الطلب معلّقًا)."""
    _require_admin(by)
    with tx() as c:
        r = c.execute("SELECT client_id, doc_type, doc_id, requested_by, reason, status FROM edit_requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        raise ValueError("الطلب غير موجود.")
    cid, doc_type, doc_id, requester, reason, status = r
    if status != "pending":
        raise ValueError("تم البت في هذا الطلب من قبل.")
    if approve and execute:
        _execute_void(doc_type, doc_id, cid, f"بناءً على طلب {requester}: {reason}", by, allow_negative)
    label = DOC_TYPES.get(doc_type, (doc_type,))[0]
    with tx() as c:
        c.execute("UPDATE edit_requests SET status=?, decided_by=?, decision_note=?, decided_at=? WHERE id=?",
                  ("approved" if approve else "rejected", by, (note or "").strip(), _now(), req_id))
        verdict = "تم قبول" if approve else "تم رفض"
        notify(c, requester, "edit_decision", f"{verdict} طلب تعديل {label} #{doc_id}",
               (note or "").strip() + (" — نُفّذ الإلغاء/العكس تلقائيًا." if approve and execute else ""), cid, ("edit_request", req_id))
        log(c, by, "approve" if approve else "reject", "edit_request", req_id,
            f"{label} #{doc_id} ({requester})" + (f": {note}" if note else ""), None, cid)
