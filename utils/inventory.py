"""المخازن، تحويل الوحدات، أذون التحويل، والجرد. كل العمليات داخل معاملة واحدة مع سجل تدقيق.

قواعد التكلفة والوحدات:
- كل الكميات والحركات تُخزَّن بـ «وحدة الاستهلاك» (الوحدة الأساسية للصنف items.unit).
- متوسط التكلفة المرجح (WAC) واحد لكل صنف ولكل وحدة استهلاك على مستوى المطعم، فنقل الصنف بين المخازن
  يحرّك الكمية بنفس التكلفة ولا يغيّر قيمة المخزون الإجمالية.
- التقريب: 6 منازل عشرية، وتُرفض الكميات الكسرية للأصناف غير القابلة للتجزئة (قطعة/علبة...).
"""
import pandas as pd

from database.db import tx, query
from utils.audit import log
from utils.security import guard
from utils import controls

DECIMALS = 6
DISCRETE_UNITS = {"قطعة", "حبة", "علبة", "كيس", "كرتونة", "عبوة", "رغيف", "piece", "pcs", "pc", "unit", "can", "bag", "box"}
KIND_AR = {"main": "رئيسي", "production": "تشغيل / إنتاج", "other": "آخر"}
STATUS_AR = {"pending": "قيد الاعتماد", "approved": "معتمد", "cancelled": "ملغي"}


def rnd(x) -> float:
    return round(float(x), DECIMALS)


def unit_cost(avg, last) -> float:
    """تكلفة الوحدة المعتمدة: المتوسط المرجح، وإن كان صفرًا فآخر سعر شراء (لتفادي تكلفة صفرية)."""
    a = float(avg or 0)
    return a if a > 0 else float(last or 0)


# ======================= مساعدات تعمل على اتصال المعاملة الجارية =======================
def default_wh(c, cid, purpose="ops") -> int:
    """purpose='purchase' => المخزن الرئيسي، 'ops' => مخزن التشغيل (وإلا الرئيسي)."""
    if purpose == "ops":
        r = c.execute("SELECT id FROM warehouses WHERE client_id=? AND is_operations=1 AND is_active=1 ORDER BY id LIMIT 1", (cid,)).fetchone()
        if r:
            return r[0]
    r = c.execute("SELECT id FROM warehouses WHERE client_id=? AND kind='main' AND is_active=1 ORDER BY id LIMIT 1", (cid,)).fetchone()
    if not r:
        r = c.execute("SELECT id FROM warehouses WHERE client_id=? AND is_active=1 ORDER BY id LIMIT 1", (cid,)).fetchone()
    if not r:
        raise ValueError("لا يوجد مخزن لهذا المطعم.")
    return r[0]


def check_wh(c, cid, wh_id) -> int:
    if not c.execute("SELECT 1 FROM warehouses WHERE id=? AND client_id=? AND is_active=1", (wh_id, cid)).fetchone():
        raise ValueError("المخزن غير موجود أو غير مفعّل لهذا المطعم.")
    return int(wh_id)


def wh_qty(c, wh_id, item_id) -> float:
    r = c.execute("SELECT qty FROM warehouse_stock WHERE warehouse_id=? AND item_id=?", (wh_id, item_id)).fetchone()
    return float(r[0]) if r else 0.0


def _consume_lots(c, item_id, wh_id, qty, prefer=None):
    """يخصم من الدفعات بنظام FEFO (الأقرب انتهاءً أولًا، والبدون صلاحية آخرًا). prefer = دفعة تُستهلك أولًا."""
    need, out, rows = float(qty), [], []
    if prefer:
        r = c.execute("SELECT id, qty_remaining FROM stock_lots WHERE id=? AND item_id=? AND warehouse_id=? AND qty_remaining>0.000000001",
                      (prefer, item_id, wh_id)).fetchone()
        if r:
            rows.append(r)
    rows += [r for r in c.execute("SELECT id, qty_remaining FROM stock_lots WHERE item_id=? AND warehouse_id=? AND qty_remaining>0.000000001 "
                                  "ORDER BY (expiry_date IS NULL), expiry_date, id", (item_id, wh_id)).fetchall()
             if not prefer or r[0] != prefer]
    for lot_id, rem in rows:
        if need <= 1e-9:
            break
        take = min(rem, need)
        c.execute("UPDATE stock_lots SET qty_remaining=ROUND(qty_remaining-?, 6) WHERE id=?", (take, lot_id))
        out.append([lot_id, rnd(take)])
        need -= take
    return out


def adjust(c, cid, item_id, wh_id, delta, *, expiry=None, unit_cost=None, received=None, source=None,
           restore=None, clone_from=None, prefer_lot=None):
    """يغيّر رصيد صنف في مخزن محدد، ويحدّث الرصيد الإجمالي items.qty_on_hand معه (متسقان دائمًا).

    الدفعات: الصرف يخصم FEFO (أو prefer_lot أولًا) ويرجع قائمة [[lot_id, qty]]؛
    الدخول يُنشئ دفعة إن وُجد expiry، أو يعيد كميات لدفعات سابقة (restore)، أو ينسخ دفعات من مخزن آخر (clone_from).
    """
    if not c.execute("SELECT 1 FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone():
        raise ValueError("صنف غير موجود لهذا المطعم.")
    delta = rnd(delta)
    c.execute("""INSERT INTO warehouse_stock(warehouse_id,item_id,qty) VALUES(?,?,?)
                 ON CONFLICT(warehouse_id,item_id) DO UPDATE SET qty=ROUND(qty+excluded.qty, 6)""", (wh_id, item_id, delta))
    c.execute("UPDATE items SET qty_on_hand=ROUND(qty_on_hand+?, 6) WHERE id=?", (delta, item_id))
    lots = []
    if delta < 0:
        lots = _consume_lots(c, item_id, wh_id, -delta, prefer_lot)
    elif delta > 0:
        left = delta
        if restore:
            for lid, q in restore:
                put = min(float(q), left)
                if put > 1e-9 and c.execute("SELECT 1 FROM stock_lots WHERE id=? AND item_id=? AND warehouse_id=?", (lid, item_id, wh_id)).fetchone():
                    c.execute("UPDATE stock_lots SET qty_remaining=ROUND(qty_remaining+?, 6) WHERE id=?", (put, lid))
                    lots.append([lid, rnd(put)])
                    left -= put
        elif clone_from:
            for lid, q in clone_from:
                row = c.execute("SELECT expiry_date, unit_cost, received_date FROM stock_lots WHERE id=?", (lid,)).fetchone()
                put = min(float(q), left)
                if row and put > 1e-9:
                    nid = c.execute("INSERT INTO stock_lots(client_id,item_id,warehouse_id,qty_remaining,expiry_date,unit_cost,received_date,source_type,source_id) "
                                    "VALUES(?,?,?,?,?,?,?,?,?)", (cid, item_id, wh_id, rnd(put), row[0], row[1], row[2], "transfer", lid)).lastrowid
                    lots.append([nid, rnd(put)])
                    left -= put
        elif expiry:
            st, sid = source or (None, None)
            nid = c.execute("INSERT INTO stock_lots(client_id,item_id,warehouse_id,qty_remaining,expiry_date,unit_cost,received_date,source_type,source_id) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", (cid, item_id, wh_id, delta, str(expiry), rnd(unit_cost or 0), str(received) if received else None, st, sid)).lastrowid
            lots = [[nid, delta]]
    return lots


def move(c, cid, item_id, wh_id, day, mtype, qty, ucost, ref, user, lots=None) -> None:
    import json
    c.execute("INSERT INTO stock_movements(client_id,item_id,warehouse_id,move_date,move_type,qty,unit_cost,ref_id,created_by,lots) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)", (cid, item_id, wh_id, str(day), mtype, rnd(qty), rnd(ucost), ref, user,
                                               json.dumps(lots) if lots else None))


def adj_move(c, cid, item_id, wh_id, day, mtype, delta, ucost, ref, user, **kw):
    """تعديل الرصيد + تسجيل الحركة (مع الدفعات التي لُمست) في خطوة واحدة."""
    lots = adjust(c, cid, item_id, wh_id, delta, **kw)
    move(c, cid, item_id, wh_id, day, mtype, delta, ucost, ref, user, lots)
    return lots


def lots_of(movement_lots) -> list:
    import json
    return json.loads(movement_lots) if movement_lots else []


def wac_remove(c, item_id, base_qty, value) -> None:
    """يسحب قيمة كمية من المتوسط المرجح (إلغاء شراء/مرتجع). يُستدعى قبل تعديل الرصيد."""
    old_qty, old_avg = c.execute("SELECT qty_on_hand, avg_cost FROM items WHERE id=?", (item_id,)).fetchone()
    new_qty = old_qty - base_qty
    avg = old_avg
    if new_qty > 1e-9:
        avg = (old_qty * old_avg - value) / new_qty
        if avg < 0:
            avg = old_avg
    c.execute("UPDATE items SET avg_cost=? WHERE id=?", (round(float(avg), 8), item_id))


def wac_add(c, item_id, base_qty, value) -> None:
    old_qty, old_avg = c.execute("SELECT qty_on_hand, avg_cost FROM items WHERE id=?", (item_id,)).fetchone()
    avg = (old_qty * old_avg + value) / (old_qty + base_qty) if old_qty > 0 else value / base_qty
    c.execute("UPDATE items SET avg_cost=? WHERE id=?", (round(float(avg), 8), item_id))


def unit_table(c, item_id):
    r = c.execute("SELECT unit FROM items WHERE id=?", (item_id,)).fetchone()
    if not r:
        raise ValueError("صنف غير موجود.")
    units = {r[0]: 1.0}
    for name, f in c.execute("SELECT unit_name, factor FROM item_units WHERE item_id=?", (item_id,)).fetchall():
        units[name] = float(f)
    return r[0], units


def to_base(c, item_id, qty, unit_name=None, strict_discrete=False):
    """يحوّل كمية بوحدة معيّنة إلى وحدة الاستهلاك. يرجع (الكمية الأساسية، المعامل، اسم الوحدة)."""
    base, units = unit_table(c, item_id)
    name = (unit_name or "").strip() or base
    if name not in units:
        raise ValueError(f"الوحدة «{name}» غير معرّفة لهذا الصنف (المعرّف: {'، '.join(units)}).")
    factor = units[name]
    bq = rnd(float(qty) * factor)
    if bq <= 0:
        raise ValueError("الكمية بعد التحويل تساوي صفرًا.")
    if strict_discrete and base.strip().lower() in DISCRETE_UNITS and abs(bq - round(bq)) > 1e-6:
        raise ValueError(f"الكمية بعد التحويل ({bq:g} {base}) كسرية لصنف غير قابل للتجزئة. عدّل الكمية أو الوحدة.")
    return bq, factor, name


# ======================= المخازن =======================
def create_default_warehouses(c, cid, user, ops="production"):
    """عميل جديد: مخزن رئيسي + مخزن تشغيل/إنتاج، ويكون مخزن التشغيل هو الذي تُخصم منه المبيعات."""
    c.execute("INSERT INTO warehouses(client_id,name,kind,is_operations,created_by) VALUES(?,?,?,?,?)",
              (cid, "المخزن الرئيسي", "main", 1 if ops == "main" else 0, user))
    c.execute("INSERT INTO warehouses(client_id,name,kind,is_operations,created_by) VALUES(?,?,?,?,?)",
              (cid, "مخزن التشغيل / الإنتاج", "production", 1 if ops == "production" else 0, user))


@guard("warehouses")
def create_warehouse(cid, name, kind, user):
    if not name.strip():
        raise ValueError("اسم المخزن مطلوب.")
    if kind not in KIND_AR:
        raise ValueError("نوع المخزن غير صحيح.")
    with tx() as c:
        if c.execute("SELECT 1 FROM warehouses WHERE client_id=? AND name=?", (cid, name.strip())).fetchone():
            raise ValueError("يوجد مخزن بنفس الاسم.")
        wid = c.execute("INSERT INTO warehouses(client_id,name,kind,created_by) VALUES(?,?,?,?)",
                        (cid, name.strip(), kind, user)).lastrowid
        log(c, user, "create", "warehouse", wid, f"{name.strip()} ({KIND_AR[kind]})", None, cid)
    return wid


@guard("warehouses")
def rename_warehouse(cid, wh_id, name, user):
    if not name.strip():
        raise ValueError("اسم المخزن مطلوب.")
    with tx() as c:
        check_wh(c, cid, wh_id)
        if c.execute("SELECT 1 FROM warehouses WHERE client_id=? AND name=? AND id<>?", (cid, name.strip(), wh_id)).fetchone():
            raise ValueError("يوجد مخزن بنفس الاسم.")
        c.execute("UPDATE warehouses SET name=? WHERE id=?", (name.strip(), wh_id))
        log(c, user, "update", "warehouse", wh_id, f"إعادة تسمية إلى {name.strip()}", None, cid)


@guard("warehouses")
def set_operations_warehouse(cid, wh_id, user):
    with tx() as c:
        check_wh(c, cid, wh_id)
        c.execute("UPDATE warehouses SET is_operations=0 WHERE client_id=?", (cid,))
        c.execute("UPDATE warehouses SET is_operations=1 WHERE id=?", (wh_id,))
        log(c, user, "update", "warehouse", wh_id, "تعيين مخزن التشغيل (تُخصم منه المبيعات والتصنيع)", None, cid)


@guard("warehouses")
def set_warehouse_active(cid, wh_id, active, user):
    with tx() as c:
        row = c.execute("SELECT is_operations FROM warehouses WHERE id=? AND client_id=?", (wh_id, cid)).fetchone()
        if not row:
            raise ValueError("المخزن غير موجود.")
        if not active:
            if row[0]:
                raise ValueError("لا يمكن تعطيل مخزن التشغيل. عيّن مخزنًا آخر أولًا.")
            if c.execute("SELECT 1 FROM warehouse_stock WHERE warehouse_id=? AND ABS(qty)>0.000001", (wh_id,)).fetchone():
                raise ValueError("لا يمكن تعطيل مخزن به أرصدة. حوّلها أولًا.")
            if c.execute("SELECT 1 FROM transfer_vouchers WHERE status='pending' AND (from_wh=? OR to_wh=?)", (wh_id, wh_id)).fetchone():
                raise ValueError("توجد أذون تحويل معلّقة على هذا المخزن.")
        c.execute("UPDATE warehouses SET is_active=? WHERE id=?", (1 if active else 0, wh_id))
        log(c, user, "update", "warehouse", wh_id, "تفعيل" if active else "تعطيل", None, cid)


# ======================= الوحدات =======================
@guard("purchases")
def set_item_units(cid, item_id, units, user):
    """units = [(unit_name, factor, is_purchase_default)] — وحدات الشراء/البدائل مقابل وحدة الاستهلاك."""
    with tx() as c:
        row = c.execute("SELECT unit, name FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone()
        if not row:
            raise ValueError("الصنف غير موجود.")
        base, iname = row
        seen, clean = set(), []
        for u in units:
            name, factor = str(u[0]).strip(), float(u[1])
            default = bool(u[2]) if len(u) > 2 else False
            if not name:
                continue
            if name == base:
                raise ValueError(f"«{name}» هي الوحدة الأساسية، لا تُضاف كوحدة بديلة.")
            if name in seen:
                raise ValueError(f"الوحدة «{name}» مكررة.")
            if factor <= 0:
                raise ValueError("معامل التحويل يجب أن يكون أكبر من صفر.")
            seen.add(name)
            clean.append((name, rnd(factor), default))
        if sum(1 for u in clean if u[2]) > 1:
            raise ValueError("اختر وحدة شراء افتراضية واحدة فقط.")
        c.execute("DELETE FROM item_units WHERE item_id=?", (item_id,))
        c.executemany("INSERT INTO item_units(item_id,unit_name,factor,is_purchase_default) VALUES(?,?,?,?)",
                      [(item_id, n, f, 1 if d else 0) for n, f, d in clean])
        desc = "؛ ".join(f"1 {n} = {f:g} {base}" for n, f, _ in clean) or "بدون وحدات بديلة"
        log(c, user, "update", "item", item_id, f"وحدات {iname}: {desc}", None, cid)


# ======================= أذون التحويل بين المخازن =======================
def _aggregate(lines):
    agg = {}
    for item_id, q in lines:
        agg[item_id] = agg.get(item_id, 0.0) + q
    return agg


@guard("warehouses")
def create_voucher(cid, from_wh, to_wh, day, lines, notes, user):
    """lines = [(item_id, qty, unit_name)] — إذن بحالة «قيد الاعتماد» ولا يؤثر على الأرصدة حتى يُعتمد."""
    if from_wh == to_wh:
        raise ValueError("المخزن المصدر والمستلم يجب أن يختلفا.")
    if not lines:
        raise ValueError("أضف صنفًا واحدًا على الأقل.")
    with tx() as c:
        check_wh(c, cid, from_wh)
        check_wh(c, cid, to_wh)
        vid = c.execute("INSERT INTO transfer_vouchers(client_id,voucher_date,from_wh,to_wh,status,notes,created_by,created_at) "
                        "VALUES(?,?,?,?,'pending',?,?,datetime('now','localtime'))",
                        (cid, str(day), from_wh, to_wh, (notes or "").strip(), user)).lastrowid
        for item_id, qty, unit in lines:
            if not c.execute("SELECT 1 FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone():
                raise ValueError("صنف غير موجود لهذا المطعم.")
            if float(qty) <= 0:
                raise ValueError("الكمية يجب أن تكون أكبر من صفر.")
            bq, factor, uname = to_base(c, item_id, qty, unit, strict_discrete=True)
            c.execute("INSERT INTO transfer_lines(voucher_id,item_id,entered_qty,unit_name,unit_factor,base_qty) VALUES(?,?,?,?,?,?)",
                      (vid, item_id, float(qty), uname, factor, bq))
        log(c, user, "create", "transfer", vid, f"إذن تحويل #{vid} ({len(lines)} صنف)", None, cid)
    return vid


@guard("warehouses")
def approve_voucher(cid, voucher_id, user, allow_negative=False):
    """يعتمد الإذن وينفّذ الحركة (صادر من المصدر، وارد للمستلم) بتكلفة المتوسط المرجح الحالية."""
    with tx() as c:
        v = c.execute("SELECT status, from_wh, to_wh, voucher_date FROM transfer_vouchers WHERE id=? AND client_id=?",
                      (voucher_id, cid)).fetchone()
        if not v:
            raise ValueError("الإذن غير موجود.")
        if v[0] != "pending":
            raise ValueError("هذا الإذن ليس قيد الاعتماد.")
        status, src, dst, day = v
        controls.check_open(c, cid, day)
        check_wh(c, cid, src)
        check_wh(c, cid, dst)
        lines = c.execute("""SELECT l.item_id, l.base_qty, i.name, i.avg_cost, i.last_price FROM transfer_lines l
                             JOIN items i ON i.id=l.item_id WHERE l.voucher_id=?""", (voucher_id,)).fetchall()
        need = _aggregate([(r[0], r[1]) for r in lines])
        names = {r[0]: r[2] for r in lines}
        short = [f"{names[i]} (المتاح {wh_qty(c, src, i):g} والمطلوب {q:g})" for i, q in need.items() if wh_qty(c, src, i) < q - 1e-9]
        if short and not allow_negative:
            raise ValueError("رصيد غير كافٍ بالمخزن المصدر: " + "، ".join(short))
        value = 0.0
        for item_id, bq, _, avg, last in lines:
            uc = unit_cost(avg, last)
            consumed = adj_move(c, cid, item_id, src, day, "transfer_out", -bq, uc, voucher_id, user)
            adj_move(c, cid, item_id, dst, day, "transfer_in", bq, uc, voucher_id, user, clone_from=consumed)
            value += bq * uc
        c.execute("UPDATE transfer_vouchers SET status='approved', approved_by=?, approved_at=datetime('now','localtime') WHERE id=?",
                  (user, voucher_id))
        log(c, user, "approve", "transfer", voucher_id, f"اعتماد إذن تحويل #{voucher_id}", value, cid)
    return value


@guard("warehouses")
def cancel_voucher(cid, voucher_id, user):
    with tx() as c:
        v = c.execute("SELECT status FROM transfer_vouchers WHERE id=? AND client_id=?", (voucher_id, cid)).fetchone()
        if not v:
            raise ValueError("الإذن غير موجود.")
        if v[0] != "pending":
            raise ValueError("لا يمكن إلغاء إذن معتمد.")
        c.execute("UPDATE transfer_vouchers SET status='cancelled' WHERE id=?", (voucher_id,))
        log(c, user, "cancel", "transfer", voucher_id, f"إلغاء إذن تحويل #{voucher_id}", None, cid)


def vouchers(cid, status=None) -> pd.DataFrame:
    df = query("""SELECT v.id, v.voucher_date, w1.name AS from_name, w2.name AS to_name, v.status, v.notes,
                  v.created_by, v.approved_by, v.approved_at, COALESCE(v.is_voided,0) AS is_voided, v.void_reason,
                  (SELECT COUNT(*) FROM transfer_lines l WHERE l.voucher_id=v.id) AS lines_count
                  FROM transfer_vouchers v JOIN warehouses w1 ON w1.id=v.from_wh JOIN warehouses w2 ON w2.id=v.to_wh
                  WHERE v.client_id=? ORDER BY v.id DESC""", (cid,))
    return df if status is None else df[df["status"] == status]


def voucher_lines(voucher_id) -> pd.DataFrame:
    return query("""SELECT i.name AS "الصنف", l.entered_qty AS "الكمية", COALESCE(l.unit_name, i.unit) AS "الوحدة",
                    l.base_qty AS "ما يعادل (وحدة الاستهلاك)", i.unit AS "وحدة الاستهلاك" FROM transfer_lines l
                    JOIN items i ON i.id=l.item_id WHERE l.voucher_id=? ORDER BY l.id""", (voucher_id,))


# ======================= الجرد لكل مخزن =======================
@guard("inventory")
def apply_stocktake(cid, wh_id, day, counts: dict, user, apply_adjustment: bool):
    """counts = {item_id: actual_qty} لمخزن محدد. عند الاعتماد تُسوّى أرصدة هذا المخزن فقط بحركة adjustment."""
    with tx() as c:
        check_wh(c, cid, wh_id)
        controls.check_open(c, cid, day)
        tid = c.execute("INSERT INTO stocktakes(client_id,count_date,warehouse_id,created_by) VALUES(?,?,?,?)",
                        (cid, str(day), wh_id, user)).lastrowid
        value_diff = 0.0
        for item_id, actual in counts.items():
            row = c.execute("SELECT avg_cost, last_price FROM items WHERE id=? AND client_id=?", (item_id, cid)).fetchone()
            if not row:
                raise ValueError("صنف غير موجود لهذا المطعم.")
            if float(actual) < 0:
                raise ValueError("الكمية الفعلية لا يمكن أن تكون سالبة.")
            system = wh_qty(c, wh_id, item_id)
            uc = unit_cost(*row)
            c.execute("INSERT INTO stocktake_lines(stocktake_id,item_id,system_qty,actual_qty) VALUES(?,?,?,?)",
                      (tid, item_id, system, float(actual)))
            diff = rnd(float(actual) - system)
            value_diff += diff * uc
            if apply_adjustment and abs(diff) > 1e-9:
                adj_move(c, cid, item_id, wh_id, day, "adjustment", diff, uc, tid, user)
        name = c.execute("SELECT name FROM warehouses WHERE id=?", (wh_id,)).fetchone()[0]
        log(c, user, "create", "stocktake", tid,
            f"جرد {name} " + ("مع تسوية الأرصدة" if apply_adjustment else "بدون تسوية"), abs(value_diff), cid)
    return tid, value_diff
