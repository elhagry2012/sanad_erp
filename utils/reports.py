"""تقارير القراءة فقط: ملخص المطعم، المخازن، كارت الصنف، كشف المورد، هندسة القائمة، تنبيهات المخزون..."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from database.db import query, IMAGES_DIR
from utils import treasury
from utils.ledger import SUPPLIER_BALANCE
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
    payable = _f(query(f"""SELECT SUM(b) v FROM (SELECT {SUPPLIER_BALANCE} AS b FROM suppliers s WHERE s.client_id=?)""", (cid,))["v"].iloc[0])
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
                   FROM purchase_invoices WHERE supplier_id=? AND client_id=? AND COALESCE(status,'active')='active'""", (supplier_id, cid))
    pay = query("SELECT id, pay_date AS d, amount, note FROM supplier_payments WHERE supplier_id=? AND client_id=?", (supplier_id, cid))
    rets = query("""SELECT id, return_date AS d, net_credit, refund_amount, reason FROM purchase_returns
                    WHERE supplier_id=? AND client_id=? AND status='active'""", (supplier_id, cid))
    recs = []
    for _, r in inv.iterrows():
        ref = r["invoice_no"] or str(r["id"])
        desc = "فاتورة شراء" + (f" (بعد خصم ضريبة الخصم {r['wht']:,.2f})" if r["wht"] else "")
        recs.append((r["d"], 0, r["id"], desc, ref, float(r["net"]), 0.0))
        if r["paid"] and r["paid"] > 0:
            recs.append((r["d"], 1, r["id"], "دفعة عند استلام الفاتورة", ref, 0.0, float(r["paid"])))
    for _, r in pay.iterrows():
        recs.append((r["d"], 2, r["id"], "دفعة" + (f" - {r['note']}" if r["note"] else ""), f"دفعة {r['id']}", 0.0, float(r["amount"])))
    for _, r in rets.iterrows():
        recs.append((r["d"], 3, r["id"], f"مرتجع مشتريات - {r['reason']} (إشعار دائن)", f"مرتجع {r['id']}", 0.0, float(r["net_credit"])))
        if r["refund_amount"] and r["refund_amount"] > 0:
            recs.append((r["d"], 4, r["id"], "استرداد نقدي/بنكي من المورد", f"مرتجع {r['id']}", float(r["refund_amount"]), 0.0))
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
           "waste": "هدر", "adjustment": "تسوية جرد", "transfer_in": "تحويل وارد", "transfer_out": "تحويل صادر",
           "opening": "رصيد افتتاحي", "purchase_return": "مرتجع مشتريات", "reversal": "عكس مستند",
           "purchase_adjust": "تعديل فاتورة شراء"}


def _doc_label(row, inv_map):
    t, ref = row["move_type"], row["ref_id"]
    if t in ("purchase", "purchase_adjust"):
        no, sup = inv_map.get(int(ref) if pd.notna(ref) else -1, ("", ""))
        head = "تعديل فاتورة شراء" if t == "purchase_adjust" else "فاتورة شراء"
        return f"{head} {no or int(ref)}" + (f" - {sup}" if sup else "")
    base = {"production_in": "تشغيلة إنتاج", "production_out": "تشغيلة إنتاج", "sale": "مبيعات كاشير", "waste": "هدر",
            "adjustment": "جرد وتسوية", "transfer_in": "إذن تحويل", "transfer_out": "إذن تحويل",
            "opening": "استيراد رصيد افتتاحي"}.get(t, t)
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
    inv_df = query("""SELECT p.id, COALESCE(p.doc_no, p.invoice_no) AS invoice_no, s.name AS sup FROM purchase_invoices p
                      JOIN suppliers s ON s.id=p.supplier_id WHERE p.client_id=?""", (cid,))
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


# ---------------------------------------------------------------- سجل الجرد
def stocktake_history(cid) -> pd.DataFrame:
    return query("""SELECT s.id, s.count_date, COALESCE(w.name,'—') AS warehouse, s.created_by,
                    (SELECT COUNT(*) FROM stocktake_lines l WHERE l.stocktake_id=s.id) AS lines_count,
                    (SELECT COUNT(*) FROM stocktake_lines l WHERE l.stocktake_id=s.id AND ABS(l.actual_qty-l.system_qty)>0.000001) AS diff_count
                    FROM stocktakes s LEFT JOIN warehouses w ON w.id=s.warehouse_id WHERE s.client_id=? ORDER BY s.id DESC""", (cid,))


def stocktake_lines(stocktake_id) -> pd.DataFrame:
    """بنود جرد محفوظ: فرق الكمية وقيمته بتكلفة الوحدة الحالية (تقريبية)."""
    return query("""SELECT i.name AS "الصنف", i.unit AS "الوحدة", l.system_qty AS "رصيد النظام", l.actual_qty AS "الكمية الفعلية",
                    l.actual_qty - l.system_qty AS "الفرق",
                    (l.actual_qty - l.system_qty) * CASE WHEN i.avg_cost>0 THEN i.avg_cost ELSE COALESCE(i.last_price,0) END AS "قيمة الفرق"
                    FROM stocktake_lines l JOIN items i ON i.id=l.item_id WHERE l.stocktake_id=? ORDER BY i.name""", (stocktake_id,))


# ---------------------------------------------------------------- القوائم المالية
def income_statement(cid, start, end) -> dict:
    """قائمة الدخل التشغيلية للفترة + المصروفات حسب البند + أرصدة الخزينة والبنك + المستحقات."""
    s, e = str(start), str(end)
    sales = query("""SELECT s.dish_id, s.qty, s.unit_price FROM pos_sales s WHERE s.client_id=? AND s.sale_date BETWEEN ? AND ?""", (cid, s, e))
    costs = dish_costs(cid).set_index("dish_id")["cost"]
    revenue = float((sales["qty"] * sales["unit_price"]).sum()) if not sales.empty else 0.0
    cogs = float((sales["dish_id"].map(costs).fillna(0) * sales["qty"]).sum()) if not sales.empty else 0.0
    waste = _f(query("SELECT SUM(cost) v FROM waste_log WHERE client_id=? AND waste_date BETWEEN ? AND ?", (cid, s, e))["v"].iloc[0])
    exp = query("""SELECT c.name AS "البند", CASE c.nature WHEN 'fixed' THEN 'ثابت' ELSE 'متغير' END AS "الطبيعة", SUM(x.amount) AS "المبلغ"
                   FROM expenses x JOIN expense_categories c ON c.id=x.category_id WHERE x.client_id=? AND x.exp_date BETWEEN ? AND ?
                   GROUP BY c.id ORDER BY SUM(x.amount) DESC""", (cid, s, e))
    opex = float(exp["المبلغ"].sum()) if not exp.empty else 0.0
    gross = revenue - cogs
    net = gross - waste - opex
    pct = lambda v: round(v / revenue * 100, 1) if revenue else 0.0           # noqa: E731
    stmt = pd.DataFrame([
        ("إجمالي المبيعات", revenue, 100.0 if revenue else 0.0),
        ("تكلفة البضاعة المباعة", -cogs, pct(cogs)),
        ("مجمل الربح", gross, pct(gross)),
        ("قيمة الهدر والتالف", -waste, pct(waste)),
        ("المصروفات التشغيلية", -opex, pct(opex)),
        ("صافي الربح", net, pct(net))], columns=["البند", "المبلغ", "% من المبيعات"])
    bal = treasury.balances(cid)
    cash_bank = pd.DataFrame([("الخزينة النقدية", bal["cash"]), ("حساب البنك", bal["bank"]),
                              ("الإجمالي النقدي", bal["cash"] + bal["bank"])], columns=["الحساب", "الرصيد"])
    pay = query(f"""SELECT s.name AS "المورد", {SUPPLIER_BALANCE} AS "الرصيد المستحق" FROM suppliers s WHERE s.client_id=? ORDER BY 2 DESC""", (cid,))
    pay = pay[pay["الرصيد المستحق"].abs() > 0.005]
    return {"statement": stmt, "expenses": exp, "cash_bank": cash_bank, "payables": pay}


# ================================================================= الصلاحية والدفعات
def expiring_lots(cid, days=14) -> pd.DataFrame:
    """دفعات ستنتهي صلاحيتها خلال `days` يومًا (أو انتهت بالفعل) وما زال لها رصيد."""
    limit = str(date.today() + timedelta(days=days))
    df = query("""SELECT l.id, i.name AS item, i.unit, w.name AS warehouse, l.qty_remaining AS qty, l.expiry_date, l.unit_cost
                  FROM stock_lots l JOIN items i ON i.id=l.item_id JOIN warehouses w ON w.id=l.warehouse_id
                  WHERE l.client_id=? AND l.qty_remaining>0.000000001 AND l.expiry_date IS NOT NULL AND l.expiry_date<=?
                  ORDER BY l.expiry_date, i.name""", (cid, limit))
    if df.empty:
        return df
    today = date.today()
    df["days_left"] = df["expiry_date"].map(lambda d: (date.fromisoformat(d) - today).days)
    df["status"] = df["days_left"].map(lambda n: "🔴 منتهي الصلاحية" if n < 0 else "🟠 خلال أسبوع" if n <= 7 else "🟡 خلال أسبوعين")
    df["value"] = df["qty"] * df["unit_cost"]
    return df


def lots_df(cid, warehouse_id=None) -> pd.DataFrame:
    """كل الدفعات ذات الرصيد (لشاشة الجرد وتقرير الصلاحية)."""
    sql = """SELECT i.name AS "الصنف", i.unit AS "الوحدة", w.name AS "المخزن", l.qty_remaining AS "الرصيد", l.expiry_date AS "تاريخ الصلاحية",
             l.received_date AS "تاريخ الاستلام", l.unit_cost AS "تكلفة الوحدة"
             FROM stock_lots l JOIN items i ON i.id=l.item_id JOIN warehouses w ON w.id=l.warehouse_id
             WHERE l.client_id=? AND l.qty_remaining>0.000000001"""
    params = [cid]
    if warehouse_id:
        sql += " AND l.warehouse_id=?"
        params.append(warehouse_id)
    df = query(sql + " ORDER BY (l.expiry_date IS NULL), l.expiry_date, i.name", tuple(params))
    if not df.empty:
        today = date.today()
        df["أيام للانتهاء"] = df["تاريخ الصلاحية"].map(lambda d: (date.fromisoformat(d) - today).days if d else None)
    return df


# ================================================================= الأصناف الراكدة
def dead_stock(cid, days=60) -> pd.DataFrame:
    """أصناف لها رصيد ولم تُستهلك (بيع/تصنيع/هدر) منذ `days` يومًا أو أكثر، مصنّفة حسب مدة الركود."""
    today = date.today()
    items = query("SELECT id, name, unit, qty_on_hand, avg_cost, last_price, is_semi FROM items WHERE client_id=? AND qty_on_hand>0.000001", (cid,))
    if items.empty:
        return items
    last_out = query("""SELECT item_id, MAX(move_date) AS last_out FROM stock_movements WHERE client_id=?
                        AND move_type IN ('sale','production_out','waste') GROUP BY item_id""", (cid,))
    last_in = query("""SELECT item_id, MAX(move_date) AS last_in FROM stock_movements WHERE client_id=?
                       AND move_type IN ('purchase','opening','production_in') GROUP BY item_id""", (cid,))
    df = items.merge(last_out, left_on="id", right_on="item_id", how="left").drop(columns="item_id")
    df = df.merge(last_in, left_on="id", right_on="item_id", how="left").drop(columns="item_id")

    def idle(r):
        ref = r["last_out"] if isinstance(r["last_out"], str) else r["last_in"] if isinstance(r["last_in"], str) else None
        return (today - date.fromisoformat(ref)).days if ref else 9999
    df["idle_days"] = df.apply(idle, axis=1)
    df["value"] = df["qty_on_hand"] * df.apply(lambda r: unit_cost(r["avg_cost"], r["last_price"]), axis=1)
    df = df[df["idle_days"] >= days].copy()
    df["class"] = df["idle_days"].map(lambda n: "ميت (180 يومًا فأكثر)" if n >= 180 else "راكد جدًا (90 يومًا فأكثر)" if n >= 90 else "راكد")
    df["last_out"] = df["last_out"].where(df["last_out"].notna(), None)
    return df.sort_values("value", ascending=False)


# ================================================================= مؤشرات اليوم الحية
def live_today(cid) -> dict:
    d = str(date.today())
    sales = query("SELECT dish_id, qty, unit_price FROM pos_sales WHERE client_id=? AND sale_date=?", (cid, d))
    costs = dish_costs(cid).set_index("dish_id")["cost"] if not sales.empty else pd.Series(dtype=float)
    rev = float((sales["qty"] * sales["unit_price"]).sum()) if not sales.empty else 0.0
    cogs = float((sales["dish_id"].map(costs).fillna(0) * sales["qty"]).sum()) if not sales.empty else 0.0
    exp = _f(query("SELECT SUM(amount) v FROM expenses WHERE client_id=? AND exp_date=?", (cid, d))["v"].iloc[0])
    waste = _f(query("SELECT SUM(cost) v FROM waste_log WHERE client_id=? AND waste_date=?", (cid, d))["v"].iloc[0])
    t = query("""SELECT COALESCE(account,'cash') AS a, direction, SUM(amount) AS v FROM treasury_transactions
                 WHERE client_id=? AND tx_date=? GROUP BY 1,2""", (cid, d))
    g = lambda a, k: float(t[(t["a"] == a) & (t["direction"] == k)]["v"].sum()) if not t.empty else 0.0       # noqa: E731
    recent = query("""SELECT ts, username, action, entity, details, amount FROM audit_log WHERE client_id=?
                      AND action IN ('create','delete','update','void','approve') ORDER BY id DESC LIMIT 12""", (cid,))
    return {"date": d, "sales": rev, "sales_lines": int(len(sales)), "cogs": cogs, "expenses": exp, "waste": waste,
            "profit": rev - cogs - exp - waste, "cash_in": g("cash", "in"), "cash_out": g("cash", "out"),
            "bank_in": g("bank", "in"), "bank_out": g("bank", "out"), "recent": recent}


def smart_alerts(cid) -> dict:
    """ملخص التنبيهات الذكية (مخزون، صلاحية، ركود، تكاليف ناقصة) للوحة والإشعارات."""
    ls = low_stock(cid)
    exp = expiring_lots(cid, 14)
    return {"stock_out": int((ls["level"] == 3).sum()) if not ls.empty else 0,
            "stock_critical": int((ls["level"] == 2).sum()) if not ls.empty else 0,
            "expired": int((exp["days_left"] < 0).sum()) if not exp.empty else 0,
            "expiring_7": int(((exp["days_left"] >= 0) & (exp["days_left"] <= 7)).sum()) if not exp.empty else 0,
            "expiring_14": int(((exp["days_left"] > 7) & (exp["days_left"] <= 14)).sum()) if not exp.empty else 0,
            "dead": int(len(dead_stock(cid, 90))), "no_cost": int(len(cost_warnings(cid)))}


# ================================================================= مرتجعات المشتريات
def returns_list(cid, start, end) -> pd.DataFrame:
    return query("""SELECT r.id AS "رقم", COALESCE(r.doc_no,'') AS "رقم المرتجع", r.return_date AS "التاريخ", s.name AS "المورد",
                    COALESCE(p.doc_no, CAST(r.invoice_id AS TEXT), '') AS "الفاتورة", r.reason AS "السبب",
                    r.subtotal AS "قبل الضريبة", r.vat_amount AS "الضريبة", r.total AS "الإجمالي", r.net_credit AS "صافي الإشعار الدائن",
                    r.refund_amount AS "المسترد", CASE r.status WHEN 'active' THEN '' ELSE 'ملغي' END AS "الحالة", r.created_by AS "أدخله"
                    FROM purchase_returns r JOIN suppliers s ON s.id=r.supplier_id LEFT JOIN purchase_invoices p ON p.id=r.invoice_id
                    WHERE r.client_id=? AND r.return_date BETWEEN ? AND ? ORDER BY r.id DESC""", (cid, str(start), str(end)))


def invoice_return_lines(cid, invoice_id) -> pd.DataFrame:
    """بنود فاتورة شراء جاهزة للإرجاع: الصنف والوحدة والسعر من الفاتورة، والمتبقي القابل للإرجاع بعد المرتجعات النشطة السابقة،
    والدفعة (الصلاحية) التي أنشأها السطر ليُخصم المرتجع منها أولًا.

    المرتجعات السابقة تُحسب لكل صنف (بوحدة الاستهلاك) كما يتحقق منها documents.create_return، وتوزَّع على أسطر الصنف بالترتيب.
    """
    df = query("""SELECT l.id AS line_id, l.item_id, i.name, COALESCE(l.unit_name, i.unit) AS unit, i.unit AS base_unit,
                         COALESCE(l.unit_factor, 1) AS factor, l.qty, l.unit_price, l.expiry_date,
                         (SELECT s.id FROM stock_lots s WHERE s.source_type='purchase_line' AND s.source_id=l.id ORDER BY s.id LIMIT 1) AS lot_id
                  FROM purchase_lines l JOIN items i ON i.id=l.item_id JOIN purchase_invoices p ON p.id=l.invoice_id
                  WHERE l.invoice_id=? AND p.client_id=? ORDER BY l.id""", (int(invoice_id), cid))
    if df.empty:
        return df
    done = dict(query("""SELECT l.item_id, SUM(l.base_qty) AS q FROM purchase_return_lines l JOIN purchase_returns r ON r.id=l.return_id
                         WHERE r.invoice_id=? AND r.status='active' GROUP BY l.item_id""", (int(invoice_id),)).values)
    left = []
    for _, r in df.iterrows():
        base = float(r["qty"]) * float(r["factor"])
        used = min(base, float(done.get(r["item_id"], 0) or 0))
        done[r["item_id"]] = float(done.get(r["item_id"], 0) or 0) - used
        left.append(round((base - used) / float(r["factor"]), 6))
    df["returnable"] = left
    return df


def return_lines(return_id) -> pd.DataFrame:
    return query("""SELECT i.name AS "الصنف", l.entered_qty AS "الكمية", COALESCE(l.unit_name, i.unit) AS "الوحدة", l.unit_price AS "سعر الوحدة",
                    l.entered_qty*l.unit_price AS "الإجمالي", l.base_qty AS "ما يعادل", i.unit AS "وحدة الاستهلاك"
                    FROM purchase_return_lines l JOIN items i ON i.id=l.item_id WHERE l.return_id=?""", (return_id,))


# ================================================================= الضرائب والإقفال
def tax_summary(cid, start, end) -> dict:
    """ملخص ضريبي للفترة (ورقة عمل تساعد المحاسب على الإقرار، وليست إقرارًا رسميًا).

    ضريبة المبيعات = من إيراد الكاشير حسب نسبة المطعم (client_settings: vat_rate، sales_prices_include_vat).
    ضريبة المشتريات (المدخلات) = ضريبة الفواتير النشطة - ضريبة المرتجعات النشطة. ضريبة الخصم من الفواتير والمرتجعات.
    """
    s, e = str(start), str(end)
    from utils import controls
    rate = float(controls.get_setting(None, cid, "vat_rate", 14) or 14)
    incl = str(controls.get_setting(None, cid, "sales_prices_include_vat", "1")) == "1"
    gross = _f(query("SELECT SUM(qty*unit_price) v FROM pos_sales WHERE client_id=? AND sale_date BETWEEN ? AND ?", (cid, s, e))["v"].iloc[0])
    out_vat = gross * rate / (100 + rate) if incl else gross * rate / 100
    net_sales = gross - out_vat if incl else gross
    inv_ = query("""SELECT COALESCE(SUM(subtotal),0) AS sub, COALESCE(SUM(vat_amount),0) AS vat, COALESCE(SUM(wht_amount),0) AS wht
                    FROM purchase_invoices WHERE client_id=? AND COALESCE(status,'active')='active' AND invoice_date BETWEEN ? AND ?""", (cid, s, e)).iloc[0]
    ret = query("""SELECT COALESCE(SUM(subtotal),0) AS sub, COALESCE(SUM(vat_amount),0) AS vat, COALESCE(SUM(wht_amount),0) AS wht
                   FROM purchase_returns WHERE client_id=? AND status='active' AND return_date BETWEEN ? AND ?""", (cid, s, e)).iloc[0]
    in_vat = float(inv_["vat"] - ret["vat"])
    summary = pd.DataFrame([
        ("إجمالي المبيعات (حسب الكاشير)", gross), ("المبيعات الخاضعة بعد استبعاد الضريبة", net_sales),
        (f"ضريبة القيمة المضافة على المبيعات ({rate:g}%)", out_vat),
        ("مشتريات الفترة قبل الضريبة (بعد المرتجعات)", float(inv_["sub"] - ret["sub"])),
        ("ضريبة المشتريات القابلة للخصم (بعد المرتجعات)", in_vat),
        ("صافي ضريبة القيمة المضافة المستحقة (+) / القابلة للترحيل (-)", out_vat - in_vat),
        ("ضريبة الخصم تحت حساب الدفعات المقدمة المحتجزة (بعد المرتجعات)", float(inv_["wht"] - ret["wht"]))],
        columns=["البند", "المبلغ"])
    wht = query("""SELECT s.name AS "المورد", SUM(p.subtotal) AS "قيمة المشتريات", SUM(p.wht_amount) AS "ضريبة الخصم"
                   FROM purchase_invoices p JOIN suppliers s ON s.id=p.supplier_id
                   WHERE p.client_id=? AND COALESCE(p.status,'active')='active' AND p.invoice_date BETWEEN ? AND ? AND COALESCE(p.wht_amount,0)>0
                   GROUP BY s.id ORDER BY 3 DESC""", (cid, s, e))
    invoices = query("""SELECT p.invoice_date AS "التاريخ", COALESCE(NULLIF(p.invoice_no,''), p.id) AS "رقم الفاتورة", s.name AS "المورد",
                        p.subtotal AS "قبل الضريبة", p.vat_amount AS "ضريبة القيمة المضافة", p.wht_amount AS "ضريبة الخصم", p.total AS "الإجمالي"
                        FROM purchase_invoices p JOIN suppliers s ON s.id=p.supplier_id
                        WHERE p.client_id=? AND COALESCE(p.status,'active')='active' AND p.invoice_date BETWEEN ? AND ?
                        ORDER BY p.invoice_date""", (cid, s, e))
    return {"summary": summary, "withholding": wht, "invoices": invoices, "rate": rate, "includes_vat": incl}


def monthly_closing(cid, year: int) -> pd.DataFrame:
    """جدول شهري للسنة: مبيعات، ضريبة مبيعات، ضريبة مشتريات، صافي الضريبة، مصروفات، وصافي ربح تشغيلي."""
    import calendar
    rows = []
    for m in range(1, 13):
        a, b = date(year, m, 1), date(year, m, calendar.monthrange(year, m)[1])
        t = tax_summary(cid, a, b)["summary"].set_index("البند")["المبلغ"]
        st = income_statement(cid, a, b)["statement"].set_index("البند")["المبلغ"]
        rows.append({"الشهر": f"{year}-{m:02d}", "المبيعات": st["إجمالي المبيعات"], "ضريبة المبيعات": t.iloc[2],
                     "ضريبة المشتريات": t.iloc[4], "صافي ضريبة القيمة المضافة": t.iloc[5], "ضريبة الخصم": t.iloc[6],
                     "المصروفات": -st["المصروفات التشغيلية"], "صافي الربح": st["صافي الربح"]})
    return pd.DataFrame(rows)
