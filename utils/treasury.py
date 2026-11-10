"""الخزينة النقدية وحساب البنك: رصيد فعلي لكل مطعم، مرتبط بالمبيعات والمشتريات والمصروفات."""
import pandas as pd

from database.db import tx, query
from utils.audit import log
from utils.security import guard
from utils import controls

SOURCE_AR = {"manual": "قيد يدوي", "purchase_invoice": "فاتورة شراء", "supplier_payment": "دفعة مورد",
             "expense": "مصروف", "sale_batch": "مبيعات", "cash_bank_transfer": "تحويل خزينة/بنك"}
ACCOUNT_AR = {"cash": "الخزينة النقدية", "bank": "حساب البنك"}
METHOD_AR = {"cash": "نقدي", "bank": "بنك / فيزا", "none": "بدون (آجل / لم يُسدَّد الآن)"}
MANUAL_IN = ["إيداع من المالك / الشركاء", "تحصيل", "إيراد آخر", "تسوية رصيد"]
MANUAL_OUT = ["سحب مالك / شركاء", "نثريات", "سلفة موظف", "رسوم بنكية", "تسوية رصيد", "أخرى"]


def account_of(method: str):
    """طريقة الدفع -> الحساب (None = لا حركة نقدية)."""
    if method not in ("cash", "bank", "none"):
        raise ValueError("طريقة الدفع غير صحيحة.")
    return None if method == "none" else method


# ---------- تُستدعى من داخل معاملات الخدمات الأخرى (نفس الاتصال) ----------
def post(c, cid, day, direction, amount, category, description, source_type, source_id, user, account="cash"):
    if amount is None or amount <= 0:
        return None
    if account not in ("cash", "bank"):
        raise ValueError("حساب غير صحيح.")
    return c.execute(
        "INSERT INTO treasury_transactions(client_id,tx_date,direction,amount,category,description,"
        "source_type,source_id,created_by,account) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (cid, str(day), direction, round(float(amount), 2), category, description or "", source_type, source_id,
         user, account)).lastrowid


def remove_by_source(c, source_type, source_id):
    c.execute("DELETE FROM treasury_transactions WHERE source_type=? AND source_id=?", (source_type, source_id))


def sync_source(c, source_type, source_id, amount=None, day=None):
    if amount is not None:
        c.execute("UPDATE treasury_transactions SET amount=? WHERE source_type=? AND source_id=?",
                  (float(amount), source_type, source_id))
    if day is not None:
        c.execute("UPDATE treasury_transactions SET tx_date=? WHERE source_type=? AND source_id=?",
                  (str(day), source_type, source_id))


def reduce_tx(c, tx_id, amount):
    """يخصم جزءًا من حركة (عند إلغاء سطر مبيعات) ويحذفها إن استُنفدت."""
    if not tx_id or amount <= 0:
        return
    row = c.execute("SELECT amount FROM treasury_transactions WHERE id=?", (tx_id,)).fetchone()
    if not row:
        return
    if row[0] - amount <= 0.005:
        c.execute("DELETE FROM treasury_transactions WHERE id=?", (tx_id,))
    else:
        c.execute("UPDATE treasury_transactions SET amount=? WHERE id=?", (round(row[0] - amount, 2), tx_id))


# ---------- قراءة الأرصدة ----------
def opening_balance(cid, account="cash") -> float:
    col = "opening_balance" if account == "cash" else "bank_opening_balance"
    df = query(f"SELECT {col} AS v FROM treasury_settings WHERE client_id=?", (cid,))
    return float(df["v"].iloc[0] or 0) if not df.empty else 0.0


def balance(cid, account="cash") -> dict:
    t = query("""SELECT COALESCE(SUM(CASE direction WHEN 'in' THEN amount END),0) AS cash_in,
                        COALESCE(SUM(CASE direction WHEN 'out' THEN amount END),0) AS cash_out
                 FROM treasury_transactions WHERE client_id=? AND COALESCE(account,'cash')=?""", (cid, account))
    op = opening_balance(cid, account)
    i, o = float(t["cash_in"].iloc[0]), float(t["cash_out"].iloc[0])
    return {"opening": op, "cash_in": i, "cash_out": o, "balance": op + i - o}


def balances(cid) -> dict:
    return {"cash": balance(cid, "cash")["balance"], "bank": balance(cid, "bank")["balance"]}


def ledger(cid, start, end, direction=None, account="cash"):
    """يرجع (جدول الحركة برصيد جارٍ، الرصيد قبل الفترة)."""
    prior = query("""SELECT COALESCE(SUM(CASE direction WHEN 'in' THEN amount ELSE -amount END),0) AS v
                     FROM treasury_transactions WHERE client_id=? AND COALESCE(account,'cash')=? AND tx_date < ?""",
                  (cid, account, str(start)))
    opening = opening_balance(cid, account) + float(prior["v"].iloc[0])
    df = query("""SELECT id, tx_date, category, description, direction, amount, source_type, created_by
                  FROM treasury_transactions WHERE client_id=? AND COALESCE(account,'cash')=? AND tx_date BETWEEN ? AND ?
                  ORDER BY tx_date, id""", (cid, account, str(start), str(end)))
    if df.empty:
        return df, opening
    df["signed"] = df["amount"].where(df["direction"] == "in", -df["amount"])
    df["balance"] = opening + df["signed"].cumsum()
    df["in_amt"] = df["amount"].where(df["direction"] == "in", 0.0)
    df["out_amt"] = df["amount"].where(df["direction"] == "out", 0.0)
    df["source"] = df["source_type"].map(SOURCE_AR).fillna(df["source_type"])
    if direction in ("in", "out"):
        df = df[df["direction"] == direction]
    return df, opening


# ---------- عمليات مستقلة (معاملة خاصة بها + صلاحيات) ----------
@guard("treasury")
def add_manual(cid, day, direction, amount, category, note, user, account="cash"):
    if direction not in ("in", "out"):
        raise ValueError("اتجاه الحركة غير صحيح.")
    if amount <= 0:
        raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")
    with tx() as c:
        controls.check_open(c, cid, day)
        tid = post(c, cid, day, direction, amount, category, note, "manual", None, user, account)
        log(c, user, "create", "treasury", tid,
            f"{ACCOUNT_AR[account]}: {'إيداع' if direction == 'in' else 'سحب'} يدوي - {category}", amount, cid)
        if direction == "out":
            controls.notify_if_large(c, cid, "thr_treasury_out", amount, f"سحب من {ACCOUNT_AR[account]}", user)
    return tid


@guard("treasury")
def transfer_between(cid, day, from_account, amount, note, user):
    """تحويل بين الخزينة والبنك (مثل إيداع النقدية في البنك): حركتان مرتبطتان في معاملة واحدة."""
    if from_account not in ("cash", "bank") or amount <= 0:
        raise ValueError("بيانات التحويل غير صحيحة.")
    to_account = "bank" if from_account == "cash" else "cash"
    with tx() as c:
        bal = c.execute("""SELECT COALESCE(SUM(CASE direction WHEN 'in' THEN amount ELSE -amount END),0)
                           FROM treasury_transactions WHERE client_id=? AND COALESCE(account,'cash')=?""",
                        (cid, from_account)).fetchone()[0]
        op = c.execute("SELECT %s FROM treasury_settings WHERE client_id=?" %
                       ("opening_balance" if from_account == "cash" else "bank_opening_balance"), (cid,)).fetchone()
        if (bal + (op[0] or 0 if op else 0)) < amount - 0.005:
            raise ValueError(f"رصيد {ACCOUNT_AR[from_account]} لا يكفي للتحويل.")
        desc = f"{note or ''} ({ACCOUNT_AR[from_account]} ← {ACCOUNT_AR[to_account]})".strip()
        a = post(c, cid, day, "out", amount, "تحويل بين الحسابات", desc, "cash_bank_transfer", None, user, from_account)
        b = post(c, cid, day, "in", amount, "تحويل بين الحسابات", desc, "cash_bank_transfer", a, user, to_account)
        log(c, user, "create", "treasury", a, f"تحويل {ACCOUNT_AR[from_account]} إلى {ACCOUNT_AR[to_account]}", amount, cid)
    return a, b


@guard("treasury")
def delete_manual(tx_id, user, cid=None):
    with tx() as c:
        row = c.execute("SELECT amount, source_type, client_id FROM treasury_transactions WHERE id=?", (tx_id,)).fetchone()
        if not row:
            raise ValueError("الحركة غير موجودة.")
        if cid is not None and row[2] != cid:
            raise ValueError("الحركة تتبع مطعمًا آخر.")
        if row[1] != "manual":
            raise ValueError("الحركات المرتبطة بمستند (مبيعات/مشتريات/مصروف/تحويل) تُلغى من مستندها الأصلي.")
        c.execute("DELETE FROM treasury_transactions WHERE id=?", (tx_id,))
        log(c, user, "delete", "treasury", tx_id, "حذف قيد يدوي", row[0], row[2])


@guard("treasury")
def set_opening(cid, amount, user, account="cash"):
    col = "opening_balance" if account == "cash" else "bank_opening_balance"
    with tx() as c:
        old = c.execute(f"SELECT {col} FROM treasury_settings WHERE client_id=?", (cid,)).fetchone()
        c.execute(f"""INSERT INTO treasury_settings(client_id,{col},updated_by,updated_at)
                     VALUES(?,?,?,datetime('now','localtime'))
                     ON CONFLICT(client_id) DO UPDATE SET {col}=excluded.{col},
                     updated_by=excluded.updated_by, updated_at=excluded.updated_at""", (cid, float(amount), user))
        log(c, user, "update", "treasury", None,
            f"الرصيد الافتتاحي ({ACCOUNT_AR[account]}): {(old[0] if old and old[0] is not None else 0):g} ← {float(amount):g}",
            amount, cid)
