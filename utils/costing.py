"""منطق التكلفة: تكلفة الوجبة اللحظية وانحراف الأسعار."""
from database.db import query


def dish_costs(client_id: int):
    """تكلفة كل وجبة = مجموع (كمية المكوّن بوحدة الاستهلاك × تكلفة الوحدة).
    تكلفة الوحدة = المتوسط المرجح، وإن كان صفرًا فآخر سعر شراء (لتفادي التكلفة الصفرية).
    missing_cost = عدد مكونات الوجبة التي لا تكلفة لها إطلاقًا (تكلفة الوجبة غير دقيقة)."""
    return query("""
        SELECT d.id AS dish_id, d.name, d.category, d.price,
               COALESCE(SUM(rl.qty * CASE WHEN i.avg_cost > 0 THEN i.avg_cost ELSE COALESCE(i.last_price, 0) END), 0) AS cost,
               COALESCE(SUM(CASE WHEN i.id IS NOT NULL AND COALESCE(i.avg_cost,0) <= 0 AND COALESCE(i.last_price,0) <= 0
                                 THEN 1 ELSE 0 END), 0) AS missing_cost
        FROM dishes d
        LEFT JOIN recipe_lines rl ON rl.recipe_id = d.recipe_id
        LEFT JOIN items i ON i.id = rl.item_id
        WHERE d.client_id = ?
        GROUP BY d.id
    """, (client_id,))


def price_variance(client_id: int):
    """مقارنة آخر سعر شراء بالسعر الذي قبله لكل خامة."""
    df = query("""
        SELECT i.id AS item_id, i.name, i.unit, ph.price_date, ph.unit_price
        FROM price_history ph JOIN items i ON i.id = ph.item_id
        WHERE i.client_id = ? ORDER BY i.id, ph.price_date
    """, (client_id,))
    rows = []
    for _, g in df.groupby("item_id"):
        if len(g) < 2:
            continue
        prev, last = g.iloc[-2]["unit_price"], g.iloc[-1]["unit_price"]
        rows.append({"الصنف": g.iloc[-1]["name"], "الوحدة": g.iloc[-1]["unit"],
                     "السعر السابق": prev, "السعر الحالي": last,
                     "التغير %": round((last - prev) / prev * 100, 1) if prev else 0})
    import pandas as pd
    return pd.DataFrame(rows).sort_values("التغير %", ascending=False) if rows else pd.DataFrame()


def price_impact(client_id: int):
    """أثر تغير سعر كل خامة (آخر سعر مقابل السابق) على تكلفة كل وجبة."""
    import pandas as pd
    var = price_variance(client_id)
    if var.empty:
        return pd.DataFrame()
    lines = query("""
        SELECT d.name AS dish, i.name AS item, rl.qty
        FROM dishes d JOIN recipe_lines rl ON rl.recipe_id = d.recipe_id
        JOIN items i ON i.id = rl.item_id WHERE d.client_id = ?""", (client_id,))
    m = lines.merge(var, left_on="item", right_on="الصنف")
    if m.empty:
        return pd.DataFrame()
    m["أثر التغير على تكلفة الوجبة"] = (m["qty"] * (m["السعر الحالي"] - m["السعر السابق"])).round(2)
    out = m[["dish", "item", "التغير %", "أثر التغير على تكلفة الوجبة"]].rename(
        columns={"dish": "الوجبة", "item": "الخامة"})
    return out.sort_values("أثر التغير على تكلفة الوجبة", ascending=False)
