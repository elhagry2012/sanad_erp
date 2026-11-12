"""منطق الأعمال: كل عملية في معاملة واحدة (BEGIN IMMEDIATE)، بصلاحيات مفروضة، وسجل تدقيق باسم المستخدم."""
import io
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

from database.db import tx, query, IMAGES_DIR
from utils import treasury, inventory as inv, controls, sequences
from utils.ledger import SUPPLIER_BALANCE
from utils.audit import log
from utils.security import guard, require, client_of

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
CHANNELS = {"dine_in": "صالة (داخل المطعم)", "delivery": "دليفري (توصيل)", "takeaway": "تيك أواي (استلام)",
            "aggregator": "تطبيقات خارجية (طلبات / مرسول / ...)"}
WASTE_REASONS = ["تلف", "انتهاء صلاحية", "خطأ تحضير", "إرجاع من عميل", "حرق / تسوية أثناء الطهي", "فقد / سرقة", "أخرى"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(v):
    """يحوّل قيم numpy/NaN إلى قيم بايثون صالحة لـ SQLite."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    return v.item() if hasattr(v, "item") else v


def _method(pay_method, legacy_flag):
    """يدعم الوسيط القديم from_treasury (True=نقدي، False=بدون)."""
    if legacy_flag is not None:
        return "cash" if legacy_flag else "none"
    return pay_method


# ======================= العملاء =======================
@guard("clients")
def add_client(name, cuisine, user, opening_cash=0.0, opening_bank=0.0):
    if not name.strip():
        raise ValueError("اسم العميل مطلوب.")
    with tx() as c:
        cid = c.execute("INSERT INTO clients(name,cuisine,created_by) VALUES(?,?,?)",
                        (name.strip(), cuisine.strip(), user)).lastrowid
        inv.create_default_warehouses(c, cid, user, ops="production")
        if opening_cash or opening_bank:
            c.execute("INSERT INTO treasury_settings(client_id,opening_balance,bank_opening_balance,updated_by,updated_at) "
                      "VALUES(?,?,?,?,?)", (cid, float(opening_cash), float(opening_bank), user, _now()))
        log(c, user, "create", "client", cid, name.strip(), None, cid)
    return cid


# ======================= تعديل الجداول الرئيسية =======================
EDITABLE = {
    "clients": (["name", "cuisine"], "client", "clients"),
    "suppliers": (["name", "phone", "opening_balance"], "supplier", "purchases"),
    "items": (["name", "unit", "min_qty", "track_expiry", "shelf_life_days"], "item", "purchases"),
    "dishes": (["name", "category", "price"], "dish", "production"),
    "expenses": (["exp_date", "amount", "note"], "expense", "expenses"),
}


def apply_edits(table, original: pd.DataFrame, edited: pd.DataFrame, user, client_id=None) -> int:
    """يقارن الجدول الأصلي بالمعدّل ويحدّث الصفوف المتغيرة فقط، مع تسجيل كل تغيير."""
    cols, entity, module = EDITABLE[table]
    require(user, module, client_id)
    orig = original.set_index("id")
    changed = 0
    try:
        with tx() as c:
            for _, row in edited.iterrows():
                rid = int(row["id"])
                o = orig.loc[rid]
                diff = {k: _clean(row[k]) for k in cols if _clean(row[k]) != _clean(o[k])}
                if not diff:
                    continue
                if table != "clients" and client_id is not None:
                    owner = c.execute(f"SELECT client_id FROM {table} WHERE id=?", (rid,)).fetchone()
                    if not owner or owner[0] != client_id:
                        raise ValueError("سجل يتبع مطعمًا آخر.")
                if table == "items" and "unit" in diff:
                    used = c.execute("SELECT 1 FROM stock_movements WHERE item_id=? LIMIT 1", (rid,)).fetchone() or \
                        c.execute("SELECT 1 FROM item_units WHERE item_id=? LIMIT 1", (rid,)).fetchone()
                    if used:
                        raise ValueError("لا يمكن تغيير وحدة صنف عليه حركات أو معاملات تحويل. أنشئ صنفًا جديدًا.")
                sets = ", ".join(f"{k}=?" for k in diff)
                c.execute(f"UPDATE {table} SET {sets}, updated_by=?, updated_at=? WHERE id=?",
                          (*diff.values(), user, _now(), rid))
                if table == "expenses" and ("amount" in diff or "exp_date" in diff):
                    treasury.sync_source(c, "expense", rid, diff.get("amount"), diff.get("exp_date"))
                details = "؛ ".join(f"{k}: {_clean(o[k])} ← {v}" for k, v in diff.items())
                log(c, user, "update", entity, rid, details, None, client_id)
                changed += 1
    except sqlite3.IntegrityError:
        raise ValueError("لا يمكن الحفظ: الاسم مكرر أو قيمة غير صالحة.")
    return changed


# ======================= الموردون والأصناف =======================
@guard("purchases")
def add_supplier(cid, name, phone, opening, user):
    if not name.strip():
        raise ValueError("اسم المورد مطلوب.")
    with tx() as c:
        sid = c.execute("INSERT INTO suppliers(client_id,name,phone,opening_balance,created_by) VALUES(?,?,?,?,?)",
                        (cid, name.strip(), phone.strip(), opening, user)).lastrowid
        log(c, user, "create", "supplier", sid, name.strip(), None, cid)
    return sid


@guard("purchases")
def add_item(cid, name, unit, min_qty, user, is_semi=0, purchase_unit=None, factor=1.0, track_expiry=False, shelf_life_days=None):
    """unit = وحدة الاستهلاك (الأساسية). purchase_unit + factor: 1 purchase_unit = factor × unit."""
    if not name.strip() or not unit.strip():
        raise ValueError("اسم الصنف ووحدته مطلوبان.")
    try:
        with tx() as c:
            iid = c.execute("INSERT INTO items(client_id,name,unit,is_semi,min_qty,track_expiry,shelf_life_days,created_by) VALUES(?,?,?,?,?,?,?,?)",
                            (cid, name.strip(), unit.strip(), is_semi, min_qty, 1 if track_expiry else 0, shelf_life_days, user)).lastrowid
            _add_purchase_unit(c, iid, unit.strip(), purchase_unit, factor)
            log(c, user, "create", "item", iid, name.strip(), None, cid)
    except sqlite3.IntegrityError:
        raise ValueError("هذا الصنف موجود بالفعل.")
    return iid


def _add_purchase_unit(c, item_id, base, purchase_unit, factor):
    pu = (purchase_unit or "").strip()
    if not pu or pu == base:
        return
    if float(factor) <= 0:
        raise ValueError("معامل التحويل يجب أن يكون أكبر من صفر.")
    c.execute("INSERT INTO item_units(item_id,unit_name,factor,is_purchase_default) VALUES(?,?,?,1)",
              (item_id, pu, inv.rnd(factor)))


def calc_invoice(amounts, vat_rate=0.0, wht_rate=0.0) -> dict:
    """amounts = [(qty, price)]. ضريبة القيمة المضافة تُضاف، وضريبة الخصم تحت حساب الدفعات المقدمة تُخصم من المستحق للمورد."""
    sub = sum(float(q) * float(p) for q, p in amounts)
    vat = round(sub * float(vat_rate) / 100, 2)
    wht = round(sub * float(wht_rate) / 100, 2)
    total = round(sub + vat, 2)
    return {"subtotal": round(sub, 2), "vat": vat, "wht": wht, "total": total, "net_due": round(total - wht, 2)}


def _save_invoice_image(data: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_EXT:
        raise ValueError("صيغة الصورة غير مدعومة (JPG أو PNG أو WEBP فقط).")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("حجم الصورة أكبر من 8 ميجابايت.")
    try:
        from PIL import Image
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        raise ValueError("الملف المرفوع ليس صورة صالحة.")
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (IMAGES_DIR / name).write_bytes(data)
    return name


def _norm_line(l):
    if isinstance(l, (tuple, list)):
        i, q, p = l[:3]
        return {"item_id": int(i), "qty": float(q), "price": float(p), "unit": None, "expiry": None}
    return {"item_id": l.get("item_id"), "new_name": (l.get("new_name") or "").strip(),
            "new_unit": (l.get("new_unit") or "").strip(), "new_purchase_unit": (l.get("new_purchase_unit") or "").strip(),
            "new_factor": float(l.get("new_factor") or 1), "new_track_expiry": bool(l.get("new_track_expiry")),
            "new_shelf_life": l.get("new_shelf_life"), "unit": (l.get("unit") or "").strip() or None,
            "expiry": (str(l["expiry"]) if l.get("expiry") else None), "qty": float(l["qty"]), "price": float(l["price"])}


def _prepare_purchase(lines, paid, vat_rate, wht_rate):
    lines = [_norm_line(l) for l in lines]
    if not lines:
        raise ValueError("أضف صنفًا واحدًا على الأقل.")
    for l in lines:
        if l["qty"] <= 0 or l["price"] < 0:
            raise ValueError("الكمية يجب أن تكون أكبر من صفر والسعر غير سالب.")
        if not l["item_id"] and not (l.get("new_name") and l.get("new_unit")):
            raise ValueError("سطر بدون صنف: اختر صنفًا أو أدخل اسم ووحدة الصنف الجديد.")
        if l["expiry"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", l["expiry"]):
            raise ValueError("تاريخ الصلاحية غير صالح.")
    if not (0 <= vat_rate <= 100 and 0 <= wht_rate <= 100):
        raise ValueError("نسبة الضريبة يجب أن تكون بين 0 و100.")
    t = calc_invoice([(l["qty"], l["price"]) for l in lines], vat_rate, wht_rate)
    if paid < 0 or paid > t["net_due"] + 0.005:
        raise ValueError("المبلغ المدفوع يجب أن يكون بين صفر وصافي المستحق للمورد.")
    return lines, t


def _add_purchase_c(c, cid, supplier_id, invoice_no, inv_date, lines, t, paid, user, vat_rate, wht_rate,
                    saved_image, pay_method, warehouse_id, replaces_id=None):
    """جسم فاتورة الشراء داخل معاملة قائمة (يُستخدم من add_purchase ومن «تعديل الفاتورة»)."""
    account = treasury.account_of(pay_method)
    controls.check_open(c, cid, inv_date)
    if not c.execute("SELECT 1 FROM suppliers WHERE id=? AND client_id=?", (supplier_id, cid)).fetchone():
        raise ValueError("المورد غير موجود.")
    wh = inv.check_wh(c, cid, warehouse_id) if warehouse_id else inv.default_wh(c, cid, "purchase")
    invoice = c.execute(
        "INSERT INTO purchase_invoices(client_id,supplier_id,invoice_no,invoice_date,total,paid,subtotal,"
        "vat_rate,vat_amount,wht_rate,wht_amount,image_path,warehouse_id,pay_method,created_by,replaces_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, supplier_id, invoice_no.strip(), str(inv_date), t["total"], paid, t["subtotal"], vat_rate,
         t["vat"], wht_rate, t["wht"], saved_image, wh, pay_method, user, replaces_id)).lastrowid
    doc_no = sequences.assign(c, cid, "purchase_invoice", invoice, inv_date)
    created = {}
    for l in lines:
        item_id, unit = l["item_id"], l["unit"]
        if not item_id:
            nm = l["new_name"]
            if nm in created:
                item_id = created[nm]
            else:
                if c.execute("SELECT 1 FROM items WHERE client_id=? AND name=?", (cid, nm)).fetchone():
                    raise ValueError(f"الصنف «{nm}» موجود بالفعل، اخترْه من القائمة.")
                item_id = c.execute("INSERT INTO items(client_id,name,unit,is_semi,track_expiry,shelf_life_days,created_by) VALUES(?,?,?,0,?,?,?)",
                                    (cid, nm, l["new_unit"], 1 if l["new_track_expiry"] else 0, l["new_shelf_life"], user)).lastrowid
                _add_purchase_unit(c, item_id, l["new_unit"], l["new_purchase_unit"], l["new_factor"])
                created[nm] = item_id
                log(c, user, "create", "item", item_id, f"{nm} (من فاتورة شراء {invoice})", None, cid)
            unit = l["new_purchase_unit"] or None
        row = c.execute("SELECT qty_on_hand, avg_cost, track_expiry, name FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone()
        if not row:
            raise ValueError("صنف غير موجود.")
        old_qty, old_avg, track, iname = row
        expiry = l["expiry"]
        if track and not expiry:
            raise ValueError(f"تاريخ الصلاحية مطلوب للصنف «{iname}».")
        if expiry and expiry < str(inv_date):
            raise ValueError(f"تاريخ صلاحية «{iname}» ({expiry}) قبل تاريخ الفاتورة.")
        bq, factor, uname = inv.to_base(c, item_id, l["qty"], unit, strict_discrete=True)
        value = l["qty"] * l["price"]
        new_avg = (old_qty * old_avg + value) / (old_qty + bq) if old_qty > 0 else value / bq
        base_price = inv.rnd(l["price"] / factor)
        line_id = c.execute("INSERT INTO purchase_lines(invoice_id,item_id,qty,unit_price,unit_name,unit_factor,expiry_date) VALUES(?,?,?,?,?,?,?)",
                            (invoice, item_id, l["qty"], l["price"], uname, factor, expiry)).lastrowid
        c.execute("UPDATE items SET avg_cost=?, last_price=?, updated_by=?, updated_at=? WHERE id=?",
                  (round(new_avg, 8), base_price, user, _now(), item_id))
        c.execute("INSERT INTO price_history(item_id,price_date,unit_price,ref_id) VALUES(?,?,?,?)", (item_id, str(inv_date), base_price, invoice))
        inv.adj_move(c, cid, item_id, wh, inv_date, "purchase", bq, base_price, invoice, user,
                     expiry=expiry, unit_cost=base_price, received=inv_date, source=("purchase_line", line_id))
    if account and paid > 0:
        treasury.post(c, cid, inv_date, "out", paid, "مشتريات",
                      f"سداد فاتورة شراء {invoice_no.strip() or invoice}", "purchase_invoice", invoice, user, account)
    log(c, user, "create", "purchase_invoice", invoice,
        f"فاتورة {doc_no} (رقم المورد {invoice_no.strip() or '—'}) - {len(lines)} صنف" + (" - بصورة مرفقة" if saved_image else "") +
        (f" - بديلة للفاتورة #{replaces_id}" if replaces_id else ""), t["total"], cid)
    controls.notify_if_large(c, cid, "thr_purchase", t["total"], f"فاتورة شراء {invoice_no.strip() or invoice}", user)
    return invoice


@guard("purchases")
def add_purchase(cid, supplier_id, invoice_no, inv_date, lines, paid, user, *, vat_rate=0.0, wht_rate=0.0,
                 image=None, pay_method="cash", warehouse_id=None, from_treasury=None):
    """فاتورة شراء كاملة في معاملة واحدة.

    الكمية والسعر بوحدة الشراء المختارة (كرتونة مثلًا)؛ تُحوَّل تلقائيًا لوحدة الاستهلاك (قطعة) ويُحسب
    المتوسط المرجح للتكلفة لكل وحدة استهلاك. لكل سطر تاريخ صلاحية اختياري (إلزامي للأصناف المتتبَّعة) تُنشأ له دفعة.
    """
    pay_method = _method(pay_method, from_treasury)
    treasury.account_of(pay_method)
    lines, t = _prepare_purchase(lines, paid, vat_rate, wht_rate)
    saved_image = _save_invoice_image(*image) if image else None
    try:
        with tx() as c:
            invoice = _add_purchase_c(c, cid, supplier_id, invoice_no, inv_date, lines, t, paid, user, vat_rate, wht_rate,
                                      saved_image, pay_method, warehouse_id)
    except BaseException:
        if saved_image:
            (IMAGES_DIR / saved_image).unlink(missing_ok=True)
        raise
    return invoice, t


@guard("purchases")
def add_payment(cid, supplier_id, pay_date, amount, note, user, pay_method="cash", from_treasury=None):
    pay_method = _method(pay_method, from_treasury)
    account = treasury.account_of(pay_method)
    if amount <= 0:
        raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")
    with tx() as c:
        controls.check_open(c, cid, pay_date)
        sup = c.execute("SELECT name FROM suppliers WHERE id=? AND client_id=?", (supplier_id, cid)).fetchone()
        if not sup:
            raise ValueError("المورد غير موجود.")
        pid = c.execute("INSERT INTO supplier_payments(client_id,supplier_id,pay_date,amount,note,pay_method,created_by) "
                        "VALUES(?,?,?,?,?,?,?)", (cid, supplier_id, str(pay_date), amount, note.strip(), pay_method, user)).lastrowid
        doc_no = sequences.assign(c, cid, "supplier_payment", pid, pay_date)
        if account:
            treasury.post(c, cid, pay_date, "out", amount, "دفعة مورد",
                          f"{sup[0]} {('- ' + note.strip()) if note.strip() else ''}".strip(), "supplier_payment", pid, user, account)
        log(c, user, "create", "supplier_payment", pid, f"{doc_no} {note.strip()}".strip(), amount, cid)
        controls.notify_if_large(c, cid, "thr_payment", amount, f"دفعة لمورد {sup[0]}", user)


def supplier_balances(cid) -> pd.DataFrame:
    """الرصيد المستحق = أول المدة + صافي الفواتير النشطة - المدفوع - الدفعات - المرتجعات + المستردات (ledger.SUPPLIER_BALANCE)."""
    return query(f"""SELECT s.id, s.name AS "المورد", {SUPPLIER_BALANCE} AS "الرصيد المستحق"
                     FROM suppliers s WHERE s.client_id=? ORDER BY s.name""", (cid,))


# ======================= الوصفات والتصنيع =======================
def _resolve_lines(c, cid, lines):
    """lines = [(item_id, qty[, unit])] -> [(item_id, base_qty, entered_qty, entered_unit)] بالتحويل التلقائي للوحدات."""
    if not lines:
        raise ValueError("أضف مكونًا واحدًا على الأقل.")
    out = []
    for l in lines:
        item_id, qty = int(l[0]), float(l[1])
        unit = l[2] if len(l) > 2 else None
        if qty <= 0:
            raise ValueError("كميات المكونات يجب أن تكون أكبر من صفر.")
        if not c.execute("SELECT 1 FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone():
            raise ValueError("مكوّن غير موجود لهذا المطعم.")
        bq, _, uname = inv.to_base(c, item_id, qty, unit)
        out.append((item_id, bq, qty, uname))
    return out


def _insert_recipe_lines(c, rid, resolved):
    c.executemany("INSERT INTO recipe_lines(recipe_id,item_id,qty,entered_qty,entered_unit) VALUES(?,?,?,?,?)",
                  [(rid, i, bq, eq, eu) for i, bq, eq, eu in resolved])


def _create_semi_c(c, cid, name, unit, yield_qty, lines, user):
    """ينشئ منتجًا نصف مصنع داخل معاملة قائمة (يستخدمه الاستيراد الجماعي لضمان الذرية)."""
    if yield_qty <= 0:
        raise ValueError("كمية الناتج يجب أن تكون أكبر من صفر.")
    row = c.execute("SELECT id,is_semi FROM items WHERE client_id=? AND name=?", (cid, name.strip())).fetchone()
    if row and not row[1]:
        raise ValueError("يوجد صنف خام بنفس الاسم. اختر اسمًا مختلفًا.")
    if row and c.execute("SELECT 1 FROM recipes WHERE output_item_id=?", (row[0],)).fetchone():
        raise ValueError("توجد وصفة لهذا المنتج بالفعل. عدّلها من تبويب التعديل.")
    out_id = row[0] if row else c.execute("INSERT INTO items(client_id,name,unit,is_semi,created_by) VALUES(?,?,?,1,?)",
                                          (cid, name.strip(), unit.strip(), user)).lastrowid
    resolved = _resolve_lines(c, cid, lines)
    if any(i == out_id for i, *_ in resolved):
        raise ValueError("لا يمكن أن يكون المنتج أحد مكونات نفسه.")
    rid = c.execute("INSERT INTO recipes(client_id,name,kind,output_item_id,yield_qty,created_by) VALUES(?,?,'semi',?,?,?)",
                    (cid, name.strip(), out_id, yield_qty, user)).lastrowid
    _insert_recipe_lines(c, rid, resolved)
    log(c, user, "create", "recipe", rid, f"نصف مصنع: {name.strip()}", None, cid)
    return rid


def _create_dish_c(c, cid, name, category, price, lines, user):
    if price <= 0:
        raise ValueError("سعر البيع يجب أن يكون أكبر من صفر.")
    resolved = _resolve_lines(c, cid, lines)
    rid = c.execute("INSERT INTO recipes(client_id,name,kind,created_by) VALUES(?,?,'dish',?)", (cid, name.strip(), user)).lastrowid
    _insert_recipe_lines(c, rid, resolved)
    did = c.execute("INSERT INTO dishes(client_id,name,category,price,recipe_id,created_by) VALUES(?,?,?,?,?,?)",
                    (cid, name.strip(), category.strip(), price, rid, user)).lastrowid
    log(c, user, "create", "dish", did, f"وجبة: {name.strip()} بسعر {price}", None, cid)
    return did


@guard("production")
def create_semi(cid, name, unit, yield_qty, lines, user):
    with tx() as c:
        return _create_semi_c(c, cid, name, unit, yield_qty, lines, user)


@guard("production")
def create_dish(cid, name, category, price, lines, user):
    with tx() as c:
        return _create_dish_c(c, cid, name, category, price, lines, user)


def replace_recipe_lines(recipe_id, lines, user, client_id=None, yield_qty=None):
    owner = query("SELECT client_id FROM recipes WHERE id=?", (recipe_id,))
    if owner.empty:
        raise ValueError("الوصفة غير موجودة.")
    cid = int(owner["client_id"].iloc[0])
    require(user, "production", cid)
    with tx() as c:
        resolved = _resolve_lines(c, cid, lines)
        c.execute("DELETE FROM recipe_lines WHERE recipe_id=?", (recipe_id,))
        _insert_recipe_lines(c, recipe_id, resolved)
        if yield_qty:
            c.execute("UPDATE recipes SET yield_qty=? WHERE id=?", (yield_qty, recipe_id))
        c.execute("UPDATE recipes SET updated_by=?, updated_at=? WHERE id=?", (user, _now(), recipe_id))
        log(c, user, "update", "recipe", recipe_id, "تعديل مكونات الوصفة", None, cid)


@guard("production")
def produce_batch(cid, recipe_id, runs, day, user, allow_negative=False, warehouse_id=None, allow_zero_cost=False):
    """تشغيلة إنتاج: تخصم الخامات من مخزن التشغيل بوحدة الاستهلاك، وتضيف الناتج بمتوسط مرجح للتكلفة."""
    if runs <= 0:
        raise ValueError("عدد مرات التشغيل يجب أن يكون أكبر من صفر.")
    with tx() as c:
        rec = c.execute("SELECT name,kind,output_item_id,yield_qty FROM recipes WHERE id=? AND client_id=?", (recipe_id, cid)).fetchone()
        if not rec or rec[1] != "semi":
            raise ValueError("اختر وصفة منتج نصف مصنع.")
        name, _, out_id, yld = rec
        wh = inv.check_wh(c, cid, warehouse_id) if warehouse_id else inv.default_wh(c, cid, "ops")
        controls.check_open(c, cid, day)
        lines = c.execute("SELECT rl.item_id, rl.qty*?, i.avg_cost, i.last_price, i.name FROM recipe_lines rl "
                          "JOIN items i ON i.id=rl.item_id WHERE rl.recipe_id=?", (runs, recipe_id)).fetchall()
        need = {}
        for item_id, q, _, _, n in lines:
            need[item_id] = need.get(item_id, 0) + q
        names = {r[0]: r[4] for r in lines}
        short = [f"{names[i]} (المتاح {inv.wh_qty(c, wh, i):g} والمطلوب {q:g})" for i, q in need.items()
                 if inv.wh_qty(c, wh, i) < q - 1e-9]
        if short and not allow_negative:
            raise ValueError("رصيد غير كافٍ في مخزن التشغيل: " + "، ".join(short))
        zero = [n for _, _, a, l_, n in lines if inv.unit_cost(a, l_) <= 0]
        if zero and not allow_zero_cost:
            raise ValueError("مكونات بدون تكلفة (سجّل لها فاتورة شراء أولًا): " + "، ".join(sorted(set(zero))))
        cost = sum(q * inv.unit_cost(a, l_) for _, q, a, l_, _ in lines)
        out_qty = inv.rnd(yld * runs)
        batch = c.execute("INSERT INTO production_batches(client_id,recipe_id,batch_date,output_qty,total_cost,created_by) "
                          "VALUES(?,?,?,?,?,?)", (cid, recipe_id, str(day), out_qty, cost, user)).lastrowid
        sequences.assign(c, cid, "production_batch", batch, day)
        for item_id, q, a, l_, _ in lines:
            inv.adj_move(c, cid, item_id, wh, day, "production_out", -q, inv.unit_cost(a, l_), batch, user)
        old_qty, old_avg = c.execute("SELECT qty_on_hand, avg_cost FROM items WHERE id=?", (out_id,)).fetchone()
        new_avg = cost / out_qty if old_qty <= 0 else (old_qty * old_avg + cost) / (old_qty + out_qty)
        c.execute("UPDATE items SET avg_cost=?, last_price=?, updated_by=?, updated_at=? WHERE id=?",
                  (round(new_avg, 8), inv.rnd(cost / out_qty), user, _now(), out_id))
        inv.adj_move(c, cid, out_id, wh, day, "production_in", out_qty, cost / out_qty, batch, user)
        log(c, user, "create", "production_batch", batch, f"{name} × {runs:g} = {out_qty:g}", cost, cid)
    return batch, cost, out_qty


# ======================= مبيعات الكاشير =======================
@guard("sales")
def record_sales(cid, rows, user, payment="cash", bank_amount=0.0, channel="dine_in", channel_detail="",
                 warehouse_id=None, collect_cash=None):
    """rows = [(date, dish_id, qty)].

    payment: cash | bank | split (bank_amount بالبنك والباقي نقدًا) | none (بدون تحصيل الآن).
    channel: قناة الطلب (صالة / دليفري / تيك أواي / تطبيقات خارجية).
    يخصم مكونات الوصفة من مخزن التشغيل، ويضيف التحصيل للخزينة و/أو البنك.
    """
    if collect_cash is not None:
        payment = "cash" if collect_cash else "none"
    if payment not in ("cash", "bank", "split", "none"):
        raise ValueError("طريقة الدفع غير صحيحة.")
    if channel not in CHANNELS:
        raise ValueError("قناة الطلب غير صحيحة.")
    rows = [(str(d), int(i), float(q)) for d, i, q in rows if float(q) > 0]
    if not rows:
        raise ValueError("لا توجد كميات للتسجيل.")
    detail = (channel_detail or "").strip()
    with tx() as c:
        wh = inv.check_wh(c, cid, warehouse_id) if warehouse_id else inv.default_wh(c, cid, "ops")
        for d_ in {r[0] for r in rows}:
            controls.check_open(c, cid, d_)
        dishes = {}
        for _, did, _ in rows:
            if did not in dishes:
                d = c.execute("SELECT price, recipe_id FROM dishes WHERE id=? AND client_id=?", (did, cid)).fetchone()
                if not d:
                    raise ValueError("وجبة غير موجودة.")
                dishes[did] = d
        total = sum(q * dishes[did][0] for _, did, q in rows)
        if payment == "split" and not (0 <= bank_amount <= total + 0.005):
            raise ValueError("مبلغ البنك يجب أن يكون بين صفر وإجمالي المبيعات.")
        share = {"cash": 0.0, "bank": 1.0, "none": 0.0}.get(payment, (bank_amount / total) if total else 0.0)
        by_day, line_ids = {}, []
        for day, did, qty in rows:
            price, recipe_id = dishes[did]
            rev = qty * price
            b_amt = round(rev * share, 2) if payment != "none" else None
            c_amt = round(rev - b_amt, 2) if payment != "none" else None
            sid = c.execute("INSERT INTO pos_sales(client_id,dish_id,sale_date,qty,unit_price,cash_amount,bank_amount,channel,"
                            "channel_detail,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (cid, did, day, qty, price, c_amt, b_amt, channel, detail, user)).lastrowid
            line_ids.append((sid, day))
            if payment != "none":
                cb = by_day.setdefault(day, {"cash": 0.0, "bank": 0.0, "ids": []})
                cb["cash"] += c_amt
                cb["bank"] += b_amt
                cb["ids"].append(sid)
            for item_id, rq, avg, last in c.execute(
                    "SELECT rl.item_id, rl.qty, i.avg_cost, i.last_price FROM recipe_lines rl JOIN items i ON i.id=rl.item_id "
                    "WHERE rl.recipe_id=?", (recipe_id,)).fetchall():
                inv.adj_move(c, cid, item_id, wh, day, "sale", -rq * qty, inv.unit_cost(avg, last), sid, user)
        label = CHANNELS[channel] + (f" - {detail}" if detail else "")
        for day, cb in by_day.items():
            cash_tx = treasury.post(c, cid, day, "in", cb["cash"], "مبيعات كاشير", f"مبيعات {day} - {label}", "sale_batch", None, user, "cash")
            bank_tx = treasury.post(c, cid, day, "in", cb["bank"], "مبيعات بنك / فيزا", f"مبيعات {day} - {label}", "sale_batch", None, user, "bank")
            for sid in cb["ids"]:
                c.execute("UPDATE pos_sales SET treasury_tx_id=?, bank_tx_id=? WHERE id=?", (cash_tx, bank_tx, sid))
        log(c, user, "create", "sale", None,
            f"{len(rows)} سطر مبيعات - {label} - الدفع: {payment}", total, cid)
    return len(rows), total


def delete_sale(sale_id, user, client_id=None):
    """يلغي سطر مبيعات، يعيد المكونات لمخازنها، ويخصم قيمته من تحصيل الخزينة/البنك المرتبط."""
    owner = client_of("pos_sales", sale_id)
    if owner is None:
        raise ValueError("السجل غير موجود.")
    require(user, "sales", owner)
    with tx() as c:
        s_ = c.execute("SELECT qty, unit_price, treasury_tx_id, bank_tx_id, cash_amount, bank_amount, sale_date FROM pos_sales WHERE id=?",
                       (sale_id,)).fetchone()
        controls.check_open(c, owner, s_[6])
        for item_id, wh_id, qty, lots in c.execute("SELECT item_id, warehouse_id, qty, lots FROM stock_movements "
                                                   "WHERE move_type='sale' AND ref_id=?", (sale_id,)).fetchall():
            inv.adjust(c, owner, item_id, wh_id, -qty, restore=inv.lots_of(lots))
        c.execute("DELETE FROM stock_movements WHERE move_type='sale' AND ref_id=?", (sale_id,))
        value = s_[0] * s_[1]
        cash_part = s_[4] if s_[4] is not None else (value if s_[2] else 0.0)     # سجلات قديمة: كلها نقدي
        treasury.reduce_tx(c, s_[2], cash_part)
        treasury.reduce_tx(c, s_[3], s_[5] or 0.0)
        c.execute("DELETE FROM pos_sales WHERE id=?", (sale_id,))
        log(c, user, "delete", "sale", sale_id, "إلغاء سطر مبيعات وإرجاع المخزون", value, owner)


# ======================= الهدر والتالف =======================
@guard("waste")
def record_waste(cid, day, item_id, qty, reason, user, warehouse_id=None, unit=None):
    """هدر خامة أو منتج نصف مصنع (بأي وحدة معرّفة). يخصم من المخزن المحدد (الافتراضي: مخزن التشغيل)."""
    with tx() as c:
        row = c.execute("SELECT avg_cost, last_price FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone()
        if not row:
            raise ValueError("الصنف غير موجود.")
        wh = inv.check_wh(c, cid, warehouse_id) if warehouse_id else inv.default_wh(c, cid, "ops")
        controls.check_open(c, cid, day)
        bq, _, _ = inv.to_base(c, item_id, qty, unit, strict_discrete=True)
        uc = inv.unit_cost(*row)
        cost = bq * uc
        wid = c.execute("INSERT INTO waste_log(client_id,waste_date,item_id,qty,reason,cost,warehouse_id,created_by) "
                        "VALUES(?,?,?,?,?,?,?,?)", (cid, str(day), item_id, bq, reason, cost, wh, user)).lastrowid
        sequences.assign(c, cid, "waste", wid, day)
        inv.adj_move(c, cid, item_id, wh, day, "waste", -bq, uc, wid, user)
        log(c, user, "create", "waste", wid, reason, cost, cid)
    return cost


@guard("waste")
def record_waste_dish(cid, day, dish_id, qty, reason, user, warehouse_id=None):
    """هدر وجبة جاهزة: تُخصم مكوناتها من المخزن وتُسجَّل بتكلفتها."""
    if qty <= 0:
        raise ValueError("الكمية يجب أن تكون أكبر من صفر.")
    with tx() as c:
        d = c.execute("SELECT recipe_id, name FROM dishes WHERE id=? AND client_id=?", (dish_id, cid)).fetchone()
        if not d:
            raise ValueError("الوجبة غير موجودة.")
        wh = inv.check_wh(c, cid, warehouse_id) if warehouse_id else inv.default_wh(c, cid, "ops")
        controls.check_open(c, cid, day)
        comps = c.execute("SELECT rl.item_id, rl.qty, i.avg_cost, i.last_price FROM recipe_lines rl JOIN items i ON i.id=rl.item_id "
                          "WHERE rl.recipe_id=?", (d[0],)).fetchall()
        cost = sum(rq * qty * inv.unit_cost(a, l_) for _, rq, a, l_ in comps)
        wid = c.execute("INSERT INTO waste_log(client_id,waste_date,dish_id,qty,reason,cost,warehouse_id,created_by) "
                        "VALUES(?,?,?,?,?,?,?,?)", (cid, str(day), dish_id, qty, reason, cost, wh, user)).lastrowid
        sequences.assign(c, cid, "waste", wid, day)
        for item_id, rq, a, l_ in comps:
            inv.adj_move(c, cid, item_id, wh, day, "waste", -rq * qty, inv.unit_cost(a, l_), wid, user)
        log(c, user, "create", "waste", wid, f"وجبة {d[1]} × {qty:g} - {reason}", cost, cid)
    return cost


def delete_waste(waste_id, user, client_id=None):
    owner = client_of("waste_log", waste_id)
    if owner is None:
        raise ValueError("السجل غير موجود.")
    require(user, "waste", owner)
    with tx() as c:
        cost, wdate = c.execute("SELECT cost, waste_date FROM waste_log WHERE id=?", (waste_id,)).fetchone()
        controls.check_open(c, owner, wdate)
        for item_id, wh_id, qty, lots in c.execute("SELECT item_id, warehouse_id, qty, lots FROM stock_movements "
                                                   "WHERE move_type='waste' AND ref_id=?", (waste_id,)).fetchall():
            inv.adjust(c, owner, item_id, wh_id, -qty, restore=inv.lots_of(lots))
        c.execute("DELETE FROM stock_movements WHERE move_type='waste' AND ref_id=?", (waste_id,))
        c.execute("DELETE FROM waste_log WHERE id=?", (waste_id,))
        log(c, user, "delete", "waste", waste_id, "حذف تسجيل هدر وإرجاع المخزون", cost, owner)


# ======================= المصروفات =======================
@guard("expenses")
def add_expense_category(name, nature, user):
    if not name.strip():
        raise ValueError("اسم البند مطلوب.")
    try:
        with tx() as c:
            cid_ = c.execute("INSERT INTO expense_categories(name,nature,created_by) VALUES(?,?,?)",
                             (name.strip(), nature, user)).lastrowid
            log(c, user, "create", "expense_category", cid_, name.strip())
    except sqlite3.IntegrityError:
        raise ValueError("هذا البند موجود بالفعل.")


@guard("expenses")
def add_expense(cid, category_id, day, amount, note, user, pay_method="cash", from_treasury=None):
    pay_method = _method(pay_method, from_treasury)
    account = treasury.account_of(pay_method)
    if amount <= 0:
        raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")
    with tx() as c:
        controls.check_open(c, cid, day)
        cat = c.execute("SELECT name FROM expense_categories WHERE id=?", (category_id,)).fetchone()
        if not cat:
            raise ValueError("بند المصروف غير موجود.")
        eid = c.execute("INSERT INTO expenses(client_id,category_id,exp_date,amount,note,pay_method,created_by) VALUES(?,?,?,?,?,?,?)",
                        (cid, category_id, str(day), amount, note.strip(), pay_method, user)).lastrowid
        sequences.assign(c, cid, "expense", eid, day)
        if account:
            treasury.post(c, cid, day, "out", amount, "مصروف", f"{cat[0]} {('- ' + note.strip()) if note.strip() else ''}".strip(),
                          "expense", eid, user, account)
        log(c, user, "create", "expense", eid, note.strip(), amount, cid)
        controls.notify_if_large(c, cid, "thr_expense", amount, f"مصروف {cat[0]}", user)


def delete_expense(expense_id, user, client_id=None):
    owner = client_of("expenses", expense_id)
    if owner is None:
        raise ValueError("السجل غير موجود.")
    require(user, "expenses", owner)
    with tx() as c:
        amount, exp_date = c.execute("SELECT amount, exp_date FROM expenses WHERE id=?", (expense_id,)).fetchone()
        controls.check_open(c, owner, exp_date)
        c.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        treasury.remove_by_source(c, "expense", expense_id)
        log(c, user, "delete", "expense", expense_id, "حذف مصروف", amount, owner)
