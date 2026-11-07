"""تقارير القراءة فقط: ملخص المطعم، المخازن، كارت الصنف، كشف المورد، هندسة القائمة، تنبيهات المخزون..."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from database.db import query, IMAGES_DIR
from utils import treasury
from utils.costing import dish_costs
from utils.inventory import unit_cost, DECIMALS


def _f(x) -> float:
    return 0.0 if x is None or pd.isna(x) else float(x)


# ---------------------------------------------------------------- ملخص مطعم
def client_summary(cid, start) -> dict:
    sales = query("SELECT dish_id, qty, unit_price FROM pos_sales WHERE client_id=? AND sale_date>=?", (cid, str(start)))
    costs = dish_costs(cid).set_index("dish_id")["cost"] if not sales.empty else pd.Series(dtype=float)
    revenue = float((sales["qty"] * sales["unit_price"]).sum()) if not sales.empty else 0.0
    cogs = float((sales["dish_id"].map(costs).fillna(0) * sales["qty"]).sum()) if not sales.empty else 0.0
    opex = _f(query("SELECT SUM(amount) v FROM expenses WHERE client_id=? AND exp_date>=?", (cid, str(start)))["v"].iloc[0])
    waste = _f(query("SELECT SUM(cost) v FROM waste_log WHERE client_id=? AND waste_date>=?", (cid, str(start)))["v"].iloc[0])
    stock = _f(query("""SELECT SUM(qty_on_hand * CASE WHEN avg_cost>0 THEN avg_cost ELSE COALESCE(last_price,0) END) v
                        FROM items WHERE client_id=?""", (cid,))["v"].iloc[0])
    payable = _f(query("""SELECT SUM(b) v FROM (SELECT s.opening_balance
        + COALESCE((SELECT SUM(total - COALESCE(wht_amount,0)) FROM purchase_invoices WHERE supplier_id=s.id),0)
        - COALESCE((SELECT SUM(paid) FROM purchase_invoices WHERE supplier_id=s.id),0)
        - COALESCE((SELECT SUM(amount) FROM supplier_payments WHERE supplier_id=s.id),0) AS b
        FROM suppliers s WHERE s.client_id=?)""", (cid,))["v"].iloc[0])
    gross = revenue - cogs
    bal = treasury.balances(cid)
    return {"revenue": revenue, "cogs": cogs, "gross": gross, "opex": opex, "waste": waste,
            "net": gross - opex - waste, "cash": bal["cash"], "bank": bal["bank"],
            "stock_value": stock, "payables": payable}


# ---------------------------------------------------------------- الفاتورة
def get_invoice(cid, inv_id):
    h = query("""SELECT p.*, s.name AS supplier, s.phone AS phone, w.name AS warehouse FROM purchase_invoices p
                 JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN warehouses w ON w.id=p.warehouse_id
                 WHERE p.id=? AND p.client_id=?""", (inv_id, cid))
    if h.empty:
        return None, pd.DataFrame()
    hdr = h.iloc[0].to_dict()
    total = _f(hdr["total"])
    vat, wht = _f(hdr.get("vat_amount")), _f(hdr.get("wht_amount"))
    hdr["subtotal"] = _f(hdr.get("subtotal")) or (total - vat)
    hdr["vat_amount"], hdr["wht_amount"] = vat, wht
    hdr["net_due"] = total - wht
    hdr["remaining"] = total - wht - _f(hdr["paid"])
    lines = query("""SELECT i.name AS "الصنف", COALESCE(l.unit_name, i.unit) AS "الوحدة", l.qty AS "الكمية",
                     l.unit_price AS "سعر الوحدة", l.qty*l.unit_price AS "الإجمالي",
                     l.qty*COALESCE(l.unit_factor,1) AS "ما يعادل", i.unit AS "وحدة الاستهلاك"
                     FROM purchase_lines l JOIN items i ON i.id=l.item_id WHERE l.invoice_id=? ORDER BY l.id""", (inv_id,))
    return hdr, lines


def invoice_image_path(name):
    if not name or pd.isna(name):
        return None
    p = IMAGES_DIR / Path(str(name)).name      # يمنع الخروج من مجلد الصور
    return p if p.exists() else None


# ---------------------------------------------------------------- كشف حساب مورد
def supplier_statement(cid, supplier_id, start, end):
    sup = query("SELECT name, phone, opening_balance FROM suppliers WHERE id=? AND client_id=?", (supplier_id, cid))
    if sup.empty:
        return None, pd.DataFrame(), 0.0, 0.0
    sup = sup.iloc[0]
    inv = query("""SELECT id, invoice_date AS d, invoice_no, total - COALESCE(wht_amount,0) AS net, paid, COALESCE(wht_amount,0) AS wht
                   FROM purchase_invoices WHERE supplier_id=? AND client_id=?""", (supplier_id, cid))
    pay = query("SELECT id, pay_date AS d, amount, note FROM supplier_payments WHERE supplier_id=? AND client_id=?", (supplier_id, cid))
    recs = []
    for _, r in inv.iterrows():
        ref = r["invoice_no"] or str(r["id"])
        desc = "فاتورة شراء" + (f" (بعد خصم ضريبة الخصم {r['wht']:,.2f})" if r["wht"] else "")
        recs.append((r["d"], 0, r["id"], desc, ref, float(r["net"]), 0.0))
        if r["paid"] and r["paid"] > 0:
            recs.append((r["d"], 1, r["id"], "دفعة عند استلام الفاتورة", ref, 0.0, float(r["paid"])))
    for _, r in pay.iterrows():
        recs.append((r["d"], 2, r["id"], "دفعة" + (f" - {r['note']}" if r["note"] else ""), f"دفعة {r['id']}", 0.0, float(r["amount"])))
    df = pd.DataFrame(recs, columns=["d", "o", "id", "desc", "ref", "credit", "debit"]).sort_values(["d", "o", "id"])
    start, end = str(start), str(end)
    before = df[df["d"] < start]
    opening = float(sup["opening_balance"] or 0) + before["credit"].sum() - before["debit"].sum()
    cur = df[(df["d"] >= start) & (df["d"] <= end)].copy()
    cur["balance"] = opening + (cur["credit"] - cur["debit"]).cumsum()
    closing = float(cur["balance"].iloc[-1]) if not cur.empty else opening
    out = cur.rename(columns={"d": "التاريخ", "desc": "البيان", "ref": "المستند", "credit": "مشتريات (مستحق للمورد)",
                              "debit": "مدفوعات", "balance": "الرصيد"})[
        ["التاريخ", "البيان", "المستند", "مشتريات (مستحق للمورد)", "مدفوعات", "الرصيد"]]
    return sup.to_dict(), out, opening, closing


# ---------------------------------------------------------------- كارت الصنف
MOVE_AR = {"purchase": "شراء", "production_in": "دخول إنتاج", "production_out": "صرف للإنتاج", "sale": "بيع",
           "waste": "هدر", "adjustment": "تسوية جرد", "transfer_in": "تحويل وارد", "transfer_out": "تحويل صادر"}


def _doc_label(row, inv_map):
    t, ref = row["move_type"], row["ref_id"]
    if t == "purchase":
        no, sup = inv_map.get(int(ref) if pd.notna(ref) else -1, ("", ""))
        return f"فاتورة شراء {no or int(ref)}" + (f" - {sup}" if sup else "")
    base = {"production_in": "تشغيلة إنتاج", "production_out": "تشغيلة إنتاج", "sale": "مبيعات كاشير", "waste": "هدر",
            "adjustment": "جرد وتسوية", "transfer_in": "إذن تحويل", "transfer_out": "إذن تحويل"}.get(t, t)
    return base + (f" #{int(ref)}" if pd.notna(ref) else "")


def item_card(cid, item_id, start, end, warehouse_id=None):
    """كارت الصنف بوحدة الاستهلاك. warehouse_id=None: كل المخازن (التحويلات تظهر وارد وصادر وصافيها صفر)."""
    item = query("SELECT name, unit, qty_on_hand, avg_cost, last_price FROM items WHERE id=? AND client_id=?", (item_id, cid))
    if item.empty:
        return None, pd.DataFrame(), 0.0, 0.0
    item = item.iloc[0]
    sql = """SELECT m.id, m.move_date, m.move_type, m.qty, m.unit_cost, m.ref_id, m.created_by, w.name AS wh
             FROM stock_movements m LEFT JOIN warehouses w ON w.id=m.warehouse_id
             WHERE m.item_id=? AND m.client_id=?"""
    params = [item_id, cid]
    if warehouse_id:
        sql += " AND m.warehouse_id=?"
        params.append(warehouse_id)
    mv = query(sql + " ORDER BY m.move_date, m.id", tuple(params))
    if warehouse_id:
        cur_qty = query("SELECT qty FROM warehouse_stock WHERE warehouse_id=? AND item_id=?", (warehouse_id, item_id))
        current = float(cur_qty["qty"].iloc[0]) if not cur_qty.empty else 0.0
    else:
        current = float(item["qty_on_hand"])
    base = current - float(mv["qty"].sum() if not mv.empty else 0.0)       # رصيد غير مسجل بحركات
    start, end = str(start), str(end)
    before = mv[mv["move_date"] < start]["qty"].sum() if not mv.empty else 0.0
    opening = base + float(before)
    cur = mv[(mv["move_date"] >= start) & (mv["move_date"] <= end)].copy()
    inv_df = query("""SELECT p.id, p.invoice_no, s.name AS sup FROM purchase_invoices p JOIN suppliers s ON s.id=p.supplier_id
                      WHERE p.client_id=?""", (cid,))
    inv_map = {int(r["id"]): (r["invoice_no"], r["sup"]) for _, r in inv_df.iterrows()}
    meta = item.to_dict()
    meta["current_qty"] = current
    if cur.empty:
        return meta, pd.DataFrame(), opening, opening
    cur["balance"] = (opening + cur["qty"].cumsum()).round(DECIMALS)
    out = pd.DataFrame({
        "التاريخ": cur["move_date"], "نوع الحركة": cur["move_type"].map(MOVE_AR),
        "المستند": cur.apply(lambda r: _doc_label(r, inv_map), axis=1), "المخزن": cur["wh"],
        "وارد": cur["qty"].where(cur["qty"] > 0, 0.0), "صادر": (-cur["qty"]).where(cur["qty"] < 0, 0.0),
        "الرصيد": cur["balance"], "تكلفة الوحدة": cur["unit_cost"], "قيمة الحركة": cur["qty"].abs() * cur["unit_cost"],
        "المستخدم": cur["created_by"],
    })
    return meta, out, opening, float(cur["balance"].iloc[-1])


# ---------------------------------------------------------------- المخازن
def warehouse_list(cid, active_only=True) -> pd.DataFrame:
    sql = "SELECT id, name, kind, is_operations, is_active FROM warehouses WHERE client_id=?"
    if active_only:
        sql += " AND is_active=1"
    return query(sql + " ORDER BY id", (cid,))


def warehouse_matrix(cid) -> pd.DataFrame:
    """جرد مجمّع: أصناف × مخازن + الإجمالي وقيمته بوحدة الاستهلاك."""
    df = query("""SELECT i.id AS item_id, i.name AS item, i.unit, w.name AS wh, s.qty,
                  CASE WHEN i.avg_cost>0 THEN i.avg_cost ELSE COALESCE(i.last_price,0) END AS uc
                  FROM warehouse_stock s JOIN warehouses w ON w.id=s.warehouse_id JOIN items i ON i.id=s.item_id
                  WHERE i.client_id=? AND w.is_active=1""", (cid,))
    if df.empty:
        return df
    pv = df.pivot_table(index=["item", "unit", "uc"], columns="wh", values="qty", aggfunc="sum", fill_value=0.0).reset_index()
    whs = [c for c in pv.columns if c not in ("item", "unit", "uc")]
    pv["الإجمالي"] = pv[whs].sum(axis=1)
    pv["القيمة"] = pv["الإجمالي"] * pv["uc"]
    return pv.rename(columns={"item": "الصنف", "unit": "وحدة الاستهلاك", "uc": "تكلفة الوحدة"})


def warehouse_balances(cid, warehouse_id) -> pd.DataFrame:
    return query("""SELECT i.name AS "الصنف", i.unit AS "الوحدة", s.qty AS "الرصيد",
                    CASE WHEN i.avg_cost>0 THEN i.avg_cost ELSE COALESCE(i.last_price,0) END AS "تكلفة الوحدة",
                    s.qty * CASE WHEN i.avg_cost>0 THEN i.avg_cost ELSE COALESCE(i.last_price,0) END AS "القيمة"
                    FROM warehouse_stock s JOIN items i ON i.id=s.item_id
                    WHERE i.client_id=? AND s.warehouse_id=? AND ABS(s.qty)>0.000001 ORDER BY i.name""", (cid, warehouse_id))


def item_unit_equivalents(cid) -> dict:
    """item_id -> (اسم وحدة الشراء الافتراضية، معاملها) لعرض الرصيد بوحدة الشراء أيضًا."""
    df = query("""SELECT u.item_id, u.unit_name, u.factor FROM item_units u JOIN items i ON i.id=u.item_id
                  WHERE i.client_id=? ORDER BY u.is_purchase_default DESC, u.id""", (cid,))
    out = {}
    for _, r in df.iterrows():
        out.setdefault(int(r["item_id"]), (r["unit_name"], float(r["factor"])))
    return out


# ---------------------------------------------------------------- تنبيهات المخزون حسب معدل الاستهلاك
def low_stock(cid, window_days=30, cover_days=3) -> pd.DataFrame:
    """يحسب متوسط الاستهلاك اليومي (مبيعات + تصنيع + هدر) ويقدّر عدد أيام التغطية المتبقية."""
    items = query("SELECT id, name, unit, qty_on_hand, min_qty FROM items WHERE client_id=? ORDER BY name", (cid,))
    if items.empty:
        return items
    today = date.today()
    since = str(today - timedelta(days=window_days))
    used = query("""SELECT item_id, -SUM(qty) AS used FROM stock_movements WHERE client_id=? AND move_date>=?
                    AND move_type IN ('sale','production_out','waste') GROUP BY item_id""", (cid, since))
    first = query("SELECT MIN(move_date) d FROM stock_movements WHERE client_id=? AND move_date>=?", (cid, since))["d"].iloc[0]
    days = window_days if not first else max(1, min(window_days, (today - date.fromisoformat(first)).days + 1))
    items = items.merge(used, left_on="id", right_on="item_id", how="left").fillna({"used": 0.0})
    items["daily"] = items["used"] / days
    items["days_left"] = items.apply(lambda r: (r["qty_on_hand"] / r["daily"]) if r["daily"] > 0 and r["qty_on_hand"] > 0
                                     else (0.0 if r["qty_on_hand"] <= 0 and r["daily"] > 0 else float("inf")), axis=1)

    def level(r):
        if r["qty_on_hand"] <= 0 and (r["daily"] > 0 or r["min_qty"] > 0):
            return 3
        if r["days_left"] < cover_days or (r["min_qty"] > 0 and r["qty_on_hand"] <= r["min_qty"]):
            return 2
        if r["days_left"] < cover_days * 2:
            return 1
        return 0
    items["level"] = items.apply(level, axis=1)
    items["status"] = items["level"].map({3: "🔴 نفد", 2: "🟠 حرج", 1: "🟡 يقترب", 0: "🟢 جيد"})
    return items.sort_values(["level", "days_left"], ascending=[False, True])


def cost_warnings(cid) -> pd.DataFrame:
    """أصناف داخلة في وصفات وليس لها أي تكلفة (تجعل تكلفة الوجبات غير دقيقة)."""
    return query("""SELECT DISTINCT i.name AS "الصنف", i.unit AS "الوحدة", i.qty_on_hand AS "الرصيد"
                    FROM recipe_lines rl JOIN recipes r ON r.id=rl.recipe_id JOIN items i ON i.id=rl.item_id
                    WHERE r.client_id=? AND COALESCE(i.avg_cost,0)<=0 AND COALESCE(i.last_price,0)<=0""", (cid,))


# ---------------------------------------------------------------- هندسة القائمة
CLASS_AR = {"star": "⭐ نجم (Star)", "plow": "🐎 حصان العمل (Plowhorse)", "puzzle": "🧩 لغز (Puzzle)", "dog": "🐕 خاسر (Dog)"}
ADVICE = {"star": "ربحية وشعبية عاليتان: حافظ على الجودة وضعه في موضع بارز بالقائمة.",
          "plow": "يُباع كثيرًا بهامش ضعيف: ارفع سعره قليلًا أو خفّض تكلفته (الكمية/المورد).",
          "puzzle": "هامش جيد لكن مبيعاته قليلة: حسّن مكانه بالقائمة وروّج له أو أعد تسميته.",
          "dog": "ربحية وشعبية ضعيفتان: أعد تصميمه أو فكّر في حذفه."}


def menu_engineering(cid, start, end):
    """تحليل كاسافانا-سميث: الشعبية (حصة المبيعات) مقابل هامش المساهمة للوحدة.
    شعبي إذا حصته ≥ 70% من الحصة المتساوية (1/عدد الوجبات)، وهامشه عالٍ إذا ≥ المتوسط المرجح للهامش."""
    dc = dish_costs(cid)
    if dc.empty:
        return pd.DataFrame(), {}
    s = query("""SELECT dish_id, SUM(qty) AS qty, SUM(qty*unit_price) AS revenue FROM pos_sales
                 WHERE client_id=? AND sale_date BETWEEN ? AND ? GROUP BY dish_id""", (cid, str(start), str(end)))
    df = dc.merge(s, on="dish_id", how="left").fillna({"qty": 0.0, "revenue": 0.0})
    df["avg_price"] = df.apply(lambda r: r["revenue"] / r["qty"] if r["qty"] > 0 else r["price"], axis=1)
    df["cm"] = df["avg_price"] - df["cost"]
    df["food_cost_pct"] = df.apply(lambda r: r["cost"] / r["avg_price"] * 100 if r["avg_price"] else 0.0, axis=1)
    total_qty = df["qty"].sum()
    df["mix_pct"] = df["qty"] / total_qty * 100 if total_qty else 0.0
    pop_th = 70.0 / len(df)                                           # = 0.7 × (100% / n)
    cm_th = float((df["cm"] * df["qty"]).sum() / total_qty) if total_qty else float(df["cm"].mean())
    df["popular"] = df["mix_pct"] >= pop_th
    df["profitable"] = df["cm"] >= cm_th

    def cls(r):
        return "star" if r["popular"] and r["profitable"] else "plow" if r["popular"] else "puzzle" if r["profitable"] else "dog"
    df["class_key"] = df.apply(cls, axis=1)
    df["class"] = df["class_key"].map(CLASS_AR)
    df["advice"] = df["class_key"].map(ADVICE)
    df["total_cm"] = df["cm"] * df["qty"]
    return df, {"pop_threshold": pop_th, "cm_threshold": cm_th, "total_qty": total_qty}


def channel_sales(cid, start, end) -> pd.DataFrame:
    df = query("""SELECT COALESCE(channel,'unknown') AS channel, COALESCE(channel_detail,'') AS detail,
                  SUM(qty) AS qty, SUM(qty*unit_price) AS revenue, COUNT(*) AS lines FROM pos_sales
                  WHERE client_id=? AND sale_date BETWEEN ? AND ? GROUP BY 1,2""", (cid, str(start), str(end)))
    return df


# ---------------------------------------------------------------- تفجير الوصفة (احتياجات الخامات)
def explode_requirements(cid, recipe_id, runs, warehouse_id=None) -> pd.DataFrame:
    """يفكّك الوصفة (وجبة أو نصف مصنع) حتى الخامات عبر المستويات المتداخلة، مع تحويل الوحدات.
    runs = عدد الوجبات (للوجبة) أو عدد مرات تنفيذ الوصفة (للنصف مصنع)."""
    lines = query("""SELECT rl.recipe_id, rl.item_id, rl.qty, i.is_semi FROM recipe_lines rl JOIN recipes r ON r.id=rl.recipe_id
                     JOIN items i ON i.id=rl.item_id WHERE r.client_id=?""", (cid,))
    by_recipe = {rid: g for rid, g in lines.groupby("recipe_id")}
    semis = query("SELECT id, output_item_id, yield_qty FROM recipes WHERE client_id=? AND kind='semi'", (cid,))
    semi_of = {int(r["output_item_id"]): (int(r["id"]), float(r["yield_qty"] or 1)) for _, r in semis.iterrows()}
    need = {}

    def walk(rid, factor, path):
        if rid in path or len(path) > 6:
            return
        g = by_recipe.get(rid)
        if g is None:
            return
        for _, r in g.iterrows():
            q = float(r["qty"]) * factor
            iid = int(r["item_id"])
            if iid in semi_of:
                srid, yld = semi_of[iid]
                walk(srid, q / yld, path | {rid})
            else:
                need[iid] = need.get(iid, 0.0) + q
    walk(int(recipe_id), float(runs), frozenset())
    if not need:
        return pd.DataFrame()
    items = query("""SELECT id, name, unit, qty_on_hand, avg_cost, last_price FROM items WHERE client_id=? AND id IN (%s)"""
                  % ",".join(str(i) for i in need), (cid,))
    if warehouse_id:
        st = query("SELECT item_id, qty FROM warehouse_stock WHERE warehouse_id=?", (warehouse_id,)).set_index("item_id")["qty"]
    eq = item_unit_equivalents(cid)
    rows = []
    for _, it in items.iterrows():
        iid = int(it["id"])
        q = need[iid]
        have = float(st.get(iid, 0.0)) if warehouse_id else float(it["qty_on_hand"])
        pu = eq.get(iid)
        rows.append({"الصنف": it["name"], "الاحتياج": round(q, 4), "وحدة الاستهلاك": it["unit"],
                     "ما يعادل بوحدة الشراء": f"{q / pu[1]:,.2f} {pu[0]}" if pu else "—",
                     "المتاح": round(have, 4), "العجز": round(max(0.0, q - have), 4),
                     "التكلفة التقديرية": round(q * unit_cost(it["avg_cost"], it["last_price"]), 2)})
    return pd.DataFrame(rows).sort_values("العجز", ascending=False)
