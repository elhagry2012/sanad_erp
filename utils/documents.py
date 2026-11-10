"""مرتجع المشتريات + إلغاء/استبدال المستندات (للمدير العام) مع عكس المخزون والتكلفة والحسابات تلقائيًا.

مبدأ العكس: لا تُحذف الحركات الأصلية؛ تُضاف حركات «عكس» (reversal) فيبقى سجل الصنف كاملًا، وتُعاد الكميات
لنفس الدفعات (تواريخ الصلاحية) عند الإمكان. المتوسط المرجح للتكلفة يُصحَّح بسحب/إضافة قيمة الكمية نفسها.
"""
import json
import re
from datetime import datetime

from database.db import tx, query, IMAGES_DIR
from utils import inventory as inv, treasury, controls, services as svc
from utils.audit import log
from utils.security import guard, _require_admin

RETURN_REASONS = ["تالف", "منتهي الصلاحية", "مخالف للمواصفات", "زيادة عن الطلب", "أخرى"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _remove_from_lots(c, cid, item_id, wh_id, qty, lots):
    """يخصم qty من الدفعات المحددة أولًا ثم FEFO للباقي، ويرجع الدفعات المستهلكة."""
    consumed, left = [], qty
    for lid, q in (lots or []):
        take = min(float(q), left)
        if take > 1e-9:
            consumed += inv.adjust(c, cid, item_id, wh_id, -take, prefer_lot=lid)
            left -= take
    if left > 1e-9:
        consumed += inv.adjust(c, cid, item_id, wh_id, -left)
    return consumed


def _need_check(c, wh_id, needs: dict, names: dict, allow_negative: bool, what: str):
    short = [f"{names[i]} (المتاح {inv.wh_qty(c, wh_id, i):g} والمطلوب {q:g})" for i, q in needs.items() if inv.wh_qty(c, wh_id, i) < q - 1e-9]
    if short and not allow_negative:
        raise ValueError(f"لا يمكن {what} لأن جزءًا من الكمية لم يعد موجودًا بالمخزن (استُهلك/حُوِّل/بيع): " + "، ".join(short) +
                         ". استخدم مرتجعًا أو جردًا، أو فعّل «السماح بالرصيد السالب».")


# =============================================================== مرتجع المشتريات
@guard("purchases")
def create_return(cid, supplier_id, ret_date, lines, reason, user, *, invoice_id=None, warehouse_id=None,
                  refund_method="none", refund_amount=0.0, vat_rate=None, wht_rate=None, notes=""):
    """مرتجع للمورد: يخرج المخزون، ويُصحَّح المتوسط المرجح، ويُخفَّض حساب المورد (إشعار دائن)، واختياريًا يُسجَّل استرداد نقدي/بنكي.

    lines: [{item_id, unit, qty, price (اختياري عند ربطه بفاتورة), lot_id (اختياري)}]
    """
    account = treasury.account_of(refund_method) if refund_method != "none" else None
    if not lines:
        raise ValueError("أضف صنفًا واحدًا على الأقل.")
    if reason not in RETURN_REASONS:
        raise ValueError("سبب المرتجع غير صحيح.")
    with tx() as c:
        controls.check_open(c, cid, ret_date)
        if not c.execute("SELECT 1 FROM suppliers WHERE id=? AND client_id=?", (supplier_id, cid)).fetchone():
            raise ValueError("المورد غير موجود.")
        irow = None
        if invoice_id:
            irow = c.execute("SELECT supplier_id, invoice_date, warehouse_id, vat_rate, wht_rate, status FROM purchase_invoices WHERE id=? AND client_id=?",
                             (invoice_id, cid)).fetchone()
            if not irow or irow[5] != "active":
                raise ValueError("الفاتورة غير موجودة أو ملغاة.")
            if irow[0] != supplier_id:
                raise ValueError("الفاتورة تتبع مورّدًا آخر.")
            if str(ret_date) < irow[1]:
                raise ValueError("تاريخ المرتجع قبل تاريخ الفاتورة.")
        wh = inv.check_wh(c, cid, warehouse_id) if warehouse_id else (
            inv.check_wh(c, cid, irow[2]) if irow and irow[2] and c.execute("SELECT 1 FROM warehouses WHERE id=? AND is_active=1", (irow[2],)).fetchone()
            else inv.default_wh(c, cid, "purchase"))
        vr = float(vat_rate if vat_rate is not None else ((irow[3] or 0) if irow else 0))
        wr = float(wht_rate if wht_rate is not None else ((irow[4] or 0) if irow else 0))
        prepared, needs, names = [], {}, {}
        for l in lines:
            item_id, qty = int(l["item_id"]), float(l["qty"])
            if qty <= 0:
                raise ValueError("الكمية يجب أن تكون أكبر من صفر.")
            it = c.execute("SELECT name FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone()
            if not it:
                raise ValueError("صنف غير موجود.")
            bq, factor, uname = inv.to_base(c, item_id, qty, l.get("unit"), strict_discrete=True)
            price = l.get("price")
            if irow:
                got = c.execute("""SELECT SUM(qty*COALESCE(unit_factor,1)), SUM(qty*unit_price) FROM purchase_lines WHERE invoice_id=? AND item_id=?""",
                                (invoice_id, item_id)).fetchone()
                if not got or not got[0]:
                    raise ValueError(f"الصنف «{it[0]}» ليس في الفاتورة المحددة.")
                done = c.execute("""SELECT COALESCE(SUM(l.base_qty),0) FROM purchase_return_lines l JOIN purchase_returns r ON r.id=l.return_id
                                    WHERE r.invoice_id=? AND r.status='active' AND l.item_id=?""", (invoice_id, item_id)).fetchone()[0]
                if bq + done > got[0] + 1e-6:
                    raise ValueError(f"الكمية المرتجعة من «{it[0]}» تتجاوز المشتراة ({got[0]:g}) مع ما سبق إرجاعه ({done:g}).")
                if price is None:
                    price = (got[1] / got[0]) * factor             # متوسط سعر الفاتورة للوحدة المختارة
            if price is None or float(price) < 0:
                raise ValueError("سعر الوحدة مطلوب.")
            prepared.append({"item_id": item_id, "qty": qty, "unit": uname, "factor": factor, "price": float(price), "bq": bq,
                             "lot": l.get("lot_id")})
            needs[item_id] = needs.get(item_id, 0) + bq
            names[item_id] = it[0]
        _need_check(c, wh, needs, names, False, "إنشاء المرتجع")
        sub = sum(p["qty"] * p["price"] for p in prepared)
        vat, wht = round(sub * vr / 100, 2), round(sub * wr / 100, 2)
        total, net = round(sub + vat, 2), round(sub + vat - wht, 2)
        if refund_method != "none" and not (0 < refund_amount <= net + 0.005):
            raise ValueError("مبلغ الاسترداد يجب أن يكون بين صفر وصافي الإشعار الدائن.")
        rid = c.execute("""INSERT INTO purchase_returns(client_id,supplier_id,invoice_id,return_date,warehouse_id,reason,subtotal,vat_rate,vat_amount,
                           wht_rate,wht_amount,total,net_credit,refund_method,refund_amount,notes,created_by,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (cid, supplier_id, invoice_id, str(ret_date), wh, reason, round(sub, 2), vr, vat, wr, wht, total, net,
                         refund_method, refund_amount if refund_method != "none" else 0, notes.strip(), user, _now())).lastrowid
        for p in prepared:
            base_price = inv.rnd(p["price"] / p["factor"])
            c.execute("INSERT INTO purchase_return_lines(return_id,item_id,entered_qty,unit_name,unit_factor,unit_price,base_qty,lot_id) VALUES(?,?,?,?,?,?,?,?)",
                      (rid, p["item_id"], p["qty"], p["unit"], p["factor"], p["price"], p["bq"], p["lot"]))
            inv.wac_remove(c, p["item_id"], p["bq"], p["qty"] * p["price"])
            inv.adj_move(c, cid, p["item_id"], wh, ret_date, "purchase_return", -p["bq"], base_price, rid, user, prefer_lot=p["lot"])
        if account:
            treasury.post(c, cid, ret_date, "in", refund_amount, "مرتجع مشتريات", f"استرداد من المورد - مرتجع #{rid}", "purchase_return", rid, user, account)
        log(c, user, "create", "purchase_return", rid, f"مرتجع ({reason}) {len(prepared)} صنف" + (f" على فاتورة #{invoice_id}" if invoice_id else ""), total, cid)
    return rid, {"subtotal": round(sub, 2), "vat": vat, "wht": wht, "total": total, "net_credit": net}


def void_return(cid, return_id, reason, user):
    _require_admin(user)
    with tx() as c:
        r = c.execute("SELECT status, return_date, supplier_id FROM purchase_returns WHERE id=? AND client_id=?", (return_id, cid)).fetchone()
        if not r or r[0] != "active":
            raise ValueError("المرتجع غير موجود أو ملغي.")
        controls.check_open(c, cid, r[1])
        for item_id, wh, qty, uc, lots in c.execute("SELECT item_id, warehouse_id, qty, unit_cost, lots FROM stock_movements "
                                                    "WHERE move_type='purchase_return' AND ref_id=?", (return_id,)).fetchall():
            back = -qty
            inv.wac_add(c, item_id, back, back * uc)
            inv.adj_move(c, cid, item_id, wh, str(datetime.now().date()), "reversal", back, uc, return_id, user, restore=inv.lots_of(lots))
        treasury.remove_by_source(c, "purchase_return", return_id)
        c.execute("UPDATE purchase_returns SET status='voided', voided_by=?, voided_at=?, void_reason=? WHERE id=?", (user, _now(), reason, return_id))
        log(c, user, "void", "purchase_return", return_id, f"إلغاء مرتجع #{return_id}: {reason}", None, cid)


# =============================================================== إلغاء/استبدال فاتورة الشراء
def _void_purchase_c(c, cid, invoice_id, reason, user, allow_negative, today):
    r = c.execute("SELECT status, invoice_date, image_path FROM purchase_invoices WHERE id=? AND client_id=?", (invoice_id, cid)).fetchone()
    if not r:
        raise ValueError("الفاتورة غير موجودة.")
    if r[0] != "active":
        raise ValueError("الفاتورة ملغاة بالفعل.")
    controls.check_open(c, cid, r[1])
    if c.execute("SELECT 1 FROM purchase_returns WHERE invoice_id=? AND status='active'", (invoice_id,)).fetchone():
        raise ValueError("يوجد مرتجع نشط مرتبط بهذه الفاتورة. ألغِ المرتجع أولًا.")
    movs = c.execute("SELECT item_id, warehouse_id, qty, unit_cost, lots FROM stock_movements WHERE move_type='purchase' AND ref_id=?", (invoice_id,)).fetchall()
    needs, names = {}, {}
    for item_id, wh, qty, uc, lots in movs:
        needs[(wh, item_id)] = needs.get((wh, item_id), 0) + qty
        names[item_id] = c.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()[0]
    for (wh, item_id), q in needs.items():
        _need_check(c, wh, {item_id: q}, names, allow_negative, "إلغاء الفاتورة")
    for item_id, wh, qty, uc, lots in movs:
        inv.wac_remove(c, item_id, qty, qty * uc)
        consumed = _remove_from_lots(c, cid, item_id, wh, qty, inv.lots_of(lots))
        inv.move(c, cid, item_id, wh, today, "reversal", -qty, uc, invoice_id, user, consumed)
    for (item_id,) in c.execute("SELECT DISTINCT item_id FROM purchase_lines WHERE invoice_id=?", (invoice_id,)).fetchall():
        c.execute("DELETE FROM price_history WHERE item_id=? AND ref_id=?", (item_id, invoice_id))
        last = c.execute("SELECT unit_price FROM price_history WHERE item_id=? ORDER BY price_date DESC, id DESC LIMIT 1", (item_id,)).fetchone()
        if last:
            c.execute("UPDATE items SET last_price=? WHERE id=?", (last[0], item_id))
    treasury.remove_by_source(c, "purchase_invoice", invoice_id)
    c.execute("UPDATE purchase_invoices SET status='voided', voided_by=?, voided_at=?, void_reason=? WHERE id=?", (user, _now(), reason, invoice_id))
    return r[2]


def void_purchase(cid, invoice_id, reason, user, allow_negative=False):
    """إلغاء فاتورة شراء (للمدير العام): يعكس المخزون والدفعات والمتوسط المرجح وسجل الأسعار والخزينة وحساب المورد."""
    _require_admin(user)
    if not (reason or "").strip():
        raise ValueError("سبب الإلغاء مطلوب.")
    with tx() as c:
        _void_purchase_c(c, cid, invoice_id, reason.strip(), user, allow_negative, str(datetime.now().date()))
        total = c.execute("SELECT total FROM purchase_invoices WHERE id=?", (invoice_id,)).fetchone()[0]
        log(c, user, "void", "purchase_invoice", invoice_id, f"إلغاء فاتورة #{invoice_id}: {reason.strip()}", total, cid)


def replace_purchase(cid, old_id, supplier_id, invoice_no, inv_date, lines, paid, user, *, vat_rate=0.0, wht_rate=0.0,
                     image=None, pay_method="cash", warehouse_id=None, allow_negative=False):
    """تعديل فاتورة شراء = إلغاء الأصل وإنشاء بديل في معاملة واحدة (إما ينجحان معًا أو لا شيء)."""
    _require_admin(user)
    treasury.account_of(pay_method)
    lines, t = svc._prepare_purchase(lines, paid, vat_rate, wht_rate)
    saved = svc._save_invoice_image(*image) if image else None
    try:
        with tx() as c:
            old_image = _void_purchase_c(c, cid, old_id, f"استُبدلت بفاتورة جديدة", user, allow_negative, str(datetime.now().date()))
            new_id = svc._add_purchase_c(c, cid, supplier_id, invoice_no, inv_date, lines, t, paid, user, vat_rate, wht_rate,
                                         saved or old_image, pay_method, warehouse_id, replaces_id=old_id)
            log(c, user, "update", "purchase_invoice", old_id, f"تعديل فاتورة #{old_id}: ألغيت واستُبدلت بالفاتورة #{new_id}", t["total"], cid)
    except BaseException:
        if saved:
            (IMAGES_DIR / saved).unlink(missing_ok=True)
        raise
    return new_id, t


# =============================================================== إلغاء إذن تحويل / تشغيلة إنتاج
def void_voucher(cid, voucher_id, reason, user, allow_negative=False):
    """عكس إذن تحويل معتمد: تعود الكميات (وتواريخ صلاحيتها) للمخزن المصدر."""
    _require_admin(user)
    if not (reason or "").strip():
        raise ValueError("سبب الإلغاء مطلوب.")
    today = str(datetime.now().date())
    with tx() as c:
        v = c.execute("SELECT status, is_voided, voucher_date FROM transfer_vouchers WHERE id=? AND client_id=?", (voucher_id, cid)).fetchone()
        if not v or v[0] != "approved" or v[1]:
            raise ValueError("الإذن غير موجود أو غير معتمد أو معكوس بالفعل.")
        controls.check_open(c, cid, v[2])
        ins = c.execute("SELECT item_id, warehouse_id, qty, unit_cost, lots FROM stock_movements WHERE move_type='transfer_in' AND ref_id=? ORDER BY id", (voucher_id,)).fetchall()
        outs = c.execute("SELECT item_id, warehouse_id, qty, unit_cost, lots FROM stock_movements WHERE move_type='transfer_out' AND ref_id=? ORDER BY id", (voucher_id,)).fetchall()
        names = {i[0]: c.execute("SELECT name FROM items WHERE id=?", (i[0],)).fetchone()[0] for i in ins}
        agg = {}
        for item_id, wh, qty, uc, lots in ins:
            agg[(wh, item_id)] = agg.get((wh, item_id), 0) + qty
        for (wh, item_id), q in agg.items():
            _need_check(c, wh, {item_id: q}, names, allow_negative, "إلغاء الإذن")
        pool = {}
        for item_id, wh, qty, uc, lots in outs:
            pool.setdefault(item_id, []).append((wh, qty, uc, lots))
        for item_id, wh, qty, uc, lots in ins:
            consumed = _remove_from_lots(c, cid, item_id, wh, qty, inv.lots_of(lots))
            inv.move(c, cid, item_id, wh, today, "reversal", -qty, uc, voucher_id, user, consumed)
            src_wh, oq, ouc, olots = pool[item_id].pop(0)
            inv.adj_move(c, cid, item_id, src_wh, today, "reversal", qty, ouc, voucher_id, user, restore=inv.lots_of(olots))
        c.execute("UPDATE transfer_vouchers SET is_voided=1, voided_by=?, voided_at=?, void_reason=? WHERE id=?", (user, _now(), reason.strip(), voucher_id))
        log(c, user, "void", "transfer", voucher_id, f"إلغاء إذن تحويل #{voucher_id}: {reason.strip()}", None, cid)


def void_batch(cid, batch_id, reason, user, allow_negative=False):
    """عكس تشغيلة إنتاج: تُسحب كمية الناتج، وتعود الخامات المصروفة (لنفس دفعاتها)، ويُصحَّح متوسط تكلفة الناتج."""
    _require_admin(user)
    if not (reason or "").strip():
        raise ValueError("سبب الإلغاء مطلوب.")
    today = str(datetime.now().date())
    with tx() as c:
        b = c.execute("SELECT is_voided, batch_date FROM production_batches WHERE id=? AND client_id=?", (batch_id, cid)).fetchone()
        if not b or b[0]:
            raise ValueError("التشغيلة غير موجودة أو معكوسة بالفعل.")
        controls.check_open(c, cid, b[1])
        outs = c.execute("SELECT item_id, warehouse_id, qty, unit_cost, lots FROM stock_movements WHERE move_type='production_in' AND ref_id=?", (batch_id,)).fetchall()
        for item_id, wh, qty, uc, lots in outs:
            nm = c.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()[0]
            _need_check(c, wh, {item_id: qty}, {item_id: nm}, allow_negative, "إلغاء التشغيلة")
            inv.wac_remove(c, item_id, qty, qty * uc)
            consumed = _remove_from_lots(c, cid, item_id, wh, qty, inv.lots_of(lots))
            inv.move(c, cid, item_id, wh, today, "reversal", -qty, uc, batch_id, user, consumed)
        for item_id, wh, qty, uc, lots in c.execute("SELECT item_id, warehouse_id, qty, unit_cost, lots FROM stock_movements WHERE move_type='production_out' AND ref_id=?", (batch_id,)).fetchall():
            inv.adj_move(c, cid, item_id, wh, today, "reversal", -qty, uc, batch_id, user, restore=inv.lots_of(lots))
        c.execute("UPDATE production_batches SET is_voided=1, voided_by=?, voided_at=? WHERE id=?", (user, _now(), batch_id))
        log(c, user, "void", "production_batch", batch_id, f"إلغاء تشغيلة #{batch_id}: {reason.strip()}", None, cid)
