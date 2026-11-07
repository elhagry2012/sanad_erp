from datetime import date
import pandas as pd
import streamlit as st

from database.db import query
from utils import theme, auth, services, reports
from utils.costing import dish_costs


def _grid(key, names, unit_opts):
    base = pd.DataFrame({"المكوّن": [None] * 4, "الوحدة": [None] * 4, "الكمية": [0.0] * 4})
    return st.data_editor(base, num_rows="dynamic", use_container_width=True, key=key, column_config={
        "المكوّن": st.column_config.SelectboxColumn(options=names, required=True),
        "الوحدة": st.column_config.SelectboxColumn(options=unit_opts, help="اتركها فارغة لوحدة الاستهلاك؛ التحويل تلقائي"),
        "الكمية": st.column_config.NumberColumn(min_value=0.0, format="%.4f")})


def _to_lines(grid, ids):
    return [(ids[r["المكوّن"]], r["الكمية"], r["الوحدة"] if isinstance(r["الوحدة"], str) and r["الوحدة"] else None)
            for _, r in grid.iterrows() if r["المكوّن"] in ids and r["الكمية"] > 0]


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("التصنيع والوصفات", auth.client_name())
    items = query("SELECT id, name, unit, is_semi, avg_cost FROM items WHERE client_id=? ORDER BY name", (cid,))
    ids = dict(zip(items["name"], items["id"]))
    names = list(items["name"])
    units_df = query("SELECT i.name AS item, u.unit_name FROM item_units u JOIN items i ON i.id=u.item_id WHERE i.client_id=?", (cid,))
    unit_opts = sorted(set(items["unit"]) | set(units_df["unit_name"]))
    whs = reports.warehouse_list(cid)

    cw = reports.cost_warnings(cid)
    if not cw.empty:
        st.warning("أصناف بلا تكلفة (تؤدي لتكلفة صفرية في الوجبات): " + "، ".join(cw["الصنف"]) + ". سجّل لها فواتير شراء.")

    t1, t2, t3, t4, t5, t6 = st.tabs(["نصف مصنع (وصفات فرعية)", "تشغيلة إنتاج", "الوجبات النهائية", "تكلفة الوجبات", "تعديل الوصفات", "تخطيط الاحتياجات"])

    with t1:
        if items.empty:
            st.info("أضف الخامات أولًا من صفحة المشتريات.")
        else:
            with st.form("semi_form"):
                a, b, c = st.columns(3)
                n = a.text_input("اسم المنتج (عجينة كريب، صوص ثومية...)")
                u = b.text_input("وحدة الناتج", "كجم")
                y = c.number_input("كمية الناتج من التشغيلة الواحدة", min_value=0.0, value=1.0)
                st.caption("مكونات التشغيلة الواحدة (بأي وحدة معرّفة، وتُحوَّل تلقائيًا لوحدة الاستهلاك):")
                g = _grid("semi_grid", names, unit_opts)
                if st.form_submit_button("حفظ المنتج النصف مصنع"):
                    try:
                        services.create_semi(cid, n, u, y, _to_lines(g, ids), user)
                        st.success("تم الحفظ.")
                    except ValueError as e:
                        st.error(str(e))
        semis = query("""SELECT r.name AS "المنتج", r.yield_qty AS "ناتج التشغيلة", i.unit AS "الوحدة", i.qty_on_hand AS "الرصيد",
                         i.avg_cost AS "تكلفة الوحدة (متوسط مرجح)" FROM recipes r JOIN items i ON i.id=r.output_item_id
                         WHERE r.client_id=? AND r.kind='semi'""", (cid,))
        if not semis.empty:
            st.dataframe(semis.round(4), use_container_width=True, hide_index=True)

    with t2:
        rec = query("SELECT id, name, yield_qty FROM recipes WHERE client_id=? AND kind='semi' ORDER BY name", (cid,))
        if rec.empty:
            st.info("أنشئ منتجًا نصف مصنع أولًا.")
        else:
            ops_idx = int(whs.index[whs["is_operations"] == 1][0]) if (whs["is_operations"] == 1).any() else 0
            with st.form("batch_form"):
                a, b, c, d = st.columns(4)
                rn = a.selectbox("المنتج", rec["name"])
                runs = b.number_input("عدد مرات تنفيذ الوصفة", min_value=0.0, value=1.0)
                day = c.date_input("تاريخ الإنتاج", date.today())
                wn = d.selectbox("مخزن التصنيع", list(whs["name"]), index=ops_idx)
                neg = st.checkbox("السماح بالصرف حتى لو كان الرصيد غير كافٍ")
                zero = st.checkbox("السماح بمكونات بلا تكلفة (غير موصى به)")
                if st.form_submit_button("تنفيذ التشغيلة"):
                    try:
                        rid = int(rec.loc[rec["name"] == rn, "id"].iloc[0])
                        wid = int(whs.loc[whs["name"] == wn, "id"].iloc[0])
                        _, cost, out = services.produce_batch(cid, rid, runs, day, user, neg, wid, zero)
                        st.success(f"تم إنتاج {out:g} بتكلفة إجمالية {cost:,.2f} (تكلفة الوحدة {cost / out:,.4f}) وأُضيف لمخزن «{wn}».")
                    except ValueError as e:
                        st.error(str(e))
        st.subheader("سجل التشغيلات")
        st.dataframe(query("""SELECT b.batch_date AS "التاريخ", r.name AS "المنتج", b.output_qty AS "الكمية", b.total_cost AS "التكلفة",
                              b.total_cost/b.output_qty AS "تكلفة الوحدة", b.created_by AS "المستخدم" FROM production_batches b
                              JOIN recipes r ON r.id=b.recipe_id WHERE b.client_id=? ORDER BY b.batch_date DESC, b.id DESC""", (cid,)).round(4),
                     use_container_width=True, hide_index=True)

    with t3:
        if items.empty:
            st.info("أضف الخامات أولًا.")
        else:
            with st.form("dish_form"):
                a, b, c = st.columns(3)
                n = a.text_input("اسم الوجبة")
                cat = b.text_input("التصنيف", "كريب")
                p = c.number_input("سعر البيع", min_value=0.0, step=5.0)
                st.caption("مكونات الوجبة الواحدة (خامات أو منتجات نصف مصنعة، بأي وحدة معرّفة):")
                g = _grid("dish_grid", names, unit_opts)
                if st.form_submit_button("حفظ الوجبة"):
                    try:
                        services.create_dish(cid, n, cat, p, _to_lines(g, ids), user)
                        st.success("تم الحفظ.")
                    except ValueError as e:
                        st.error(str(e))
        dishes = query("SELECT id, name, category, price FROM dishes WHERE client_id=? ORDER BY name", (cid,))
        if not dishes.empty:
            ed = st.data_editor(dishes, hide_index=True, use_container_width=True, key="ed_dishes",
                                column_config={"id": None, "name": "الوجبة", "category": "التصنيف", "price": "سعر البيع"})
            if st.button("حفظ تعديلات الوجبات"):
                try:
                    k = services.apply_edits("dishes", dishes, ed, user, cid)
                    st.success(f"تم حفظ {k} تعديل.") if k else st.info("لا توجد تعديلات.")
                except ValueError as e:
                    st.error(str(e))

    with t4:
        dc = dish_costs(cid)
        if dc.empty:
            st.info("لا توجد وجبات.")
        else:
            dc["الهامش"] = dc["price"] - dc["cost"]
            dc["نسبة التكلفة %"] = (dc["cost"] / dc["price"] * 100).round(1)
            st.dataframe(dc.rename(columns={"name": "الوجبة", "category": "التصنيف", "price": "السعر", "cost": "التكلفة",
                                            "missing_cost": "مكونات بلا تكلفة"}).drop(columns="dish_id").round(2),
                         use_container_width=True, hide_index=True)
            pick = st.selectbox("تفصيل مكونات وجبة", dc["name"])
            st.dataframe(query("""SELECT i.name AS "المكوّن", COALESCE(rl.entered_qty, rl.qty) AS "الكمية",
                                  COALESCE(rl.entered_unit, i.unit) AS "الوحدة", rl.qty AS "بوحدة الاستهلاك", i.unit AS "وحدة الاستهلاك",
                                  CASE WHEN i.avg_cost>0 THEN i.avg_cost ELSE COALESCE(i.last_price,0) END AS "تكلفة الوحدة",
                                  rl.qty*CASE WHEN i.avg_cost>0 THEN i.avg_cost ELSE COALESCE(i.last_price,0) END AS "التكلفة"
                                  FROM dishes d JOIN recipe_lines rl ON rl.recipe_id=d.recipe_id JOIN items i ON i.id=rl.item_id
                                  WHERE d.client_id=? AND d.name=?""", (cid, pick)).round(4), use_container_width=True, hide_index=True)

    with t5:
        allr = query("SELECT id, name, kind, yield_qty FROM recipes WHERE client_id=? ORDER BY kind, name", (cid,))
        if allr.empty:
            st.info("لا توجد وصفات.")
        else:
            label = allr["name"] + allr["kind"].map({"semi": " (نصف مصنع)", "dish": " (وجبة)"})
            pick = st.selectbox("الوصفة", label)
            row = allr.iloc[list(label).index(pick)]
            cur = query("""SELECT i.name AS "المكوّن", COALESCE(rl.entered_unit, i.unit) AS "الوحدة", COALESCE(rl.entered_qty, rl.qty) AS "الكمية",
                           i.unit AS base_unit, rl.qty AS base_qty FROM recipe_lines rl JOIN items i ON i.id=rl.item_id
                           WHERE rl.recipe_id=?""", (int(row["id"]),))
            valid = {(r["item"], r["unit_name"]) for _, r in units_df.iterrows()}
            # وحدة لم تعد معرّفة للصنف: نعرض الكمية بوحدة الاستهلاك بدل أن تفشل الحفظ لاحقًا
            for i, r in cur.iterrows():
                if r["الوحدة"] != r["base_unit"] and (r["المكوّن"], r["الوحدة"]) not in valid:
                    cur.at[i, "الوحدة"], cur.at[i, "الكمية"] = r["base_unit"], r["base_qty"]
            ed = st.data_editor(cur[["المكوّن", "الوحدة", "الكمية"]], num_rows="dynamic", use_container_width=True, key=f"re_{row['id']}",
                                column_config={"المكوّن": st.column_config.SelectboxColumn(options=names, required=True),
                                               "الوحدة": st.column_config.SelectboxColumn(options=unit_opts),
                                               "الكمية": st.column_config.NumberColumn(min_value=0.0, format="%.4f")})
            ny = None
            if row["kind"] == "semi":
                ny = st.number_input("ناتج التشغيلة", min_value=0.0, value=float(row["yield_qty"]), key=f"ry_{row['id']}")
            if st.button("حفظ الوصفة"):
                try:
                    services.replace_recipe_lines(int(row["id"]), _to_lines(ed, ids), user, cid, ny)
                    st.success("تم تحديث الوصفة. تكلفة الوجبات تتحدث فورًا.")
                except ValueError as e:
                    st.error(str(e))

    with t6:
        st.caption("تفجير الوصفة: يفكّك الوجبة أو المنتج النصف مصنع حتى الخامات عبر كل المستويات، ويحوّل الاحتياج لوحدة الشراء ويقارنه بالمتاح.")
        allr = query("SELECT id, name, kind FROM recipes WHERE client_id=? ORDER BY kind, name", (cid,))
        if allr.empty:
            st.info("لا توجد وصفات.")
        else:
            label = allr["name"] + allr["kind"].map({"semi": " (نصف مصنع - مرات التنفيذ)", "dish": " (وجبة - عدد الوجبات)"})
            a, b, c = st.columns(3)
            pick = a.selectbox("الوصفة", label, key="plan_recipe")
            qty = b.number_input("الكمية المطلوبة", min_value=1.0, value=100.0, key="plan_qty")
            wn = c.selectbox("المقارنة بمخزن", ["كل المخازن"] + list(whs["name"]), key="plan_wh")
            wid = None if wn == "كل المخازن" else int(whs.loc[whs["name"] == wn, "id"].iloc[0])
            plan = reports.explode_requirements(cid, int(allr.iloc[list(label).index(pick)]["id"]), qty, wid)
            if plan.empty:
                st.info("لا توجد مكونات.")
            else:
                m = st.columns(2)
                m[0].metric("التكلفة التقديرية", f"{plan['التكلفة التقديرية'].sum():,.2f}")
                m[1].metric("أصناف بها عجز", int((plan["العجز"] > 0).sum()))
                st.dataframe(plan, use_container_width=True, hide_index=True)
