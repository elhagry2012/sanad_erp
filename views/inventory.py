from datetime import date, timedelta
import streamlit as st

from database.db import query
from utils import theme, auth, reports, inventory
from utils.costing import price_variance, price_impact

ALL_WH = "كل المخازن (مجمّع)"


def _wh_picker(cid, key, with_all=True, default_ops=False):
    whs = reports.warehouse_list(cid)
    names = ([ALL_WH] if with_all else []) + list(whs["name"])
    idx = 0
    if default_ops and (whs["is_operations"] == 1).any():
        idx = names.index(whs[whs["is_operations"] == 1]["name"].iloc[0])
    pick = st.selectbox("المخزن", names, index=idx, key=key)
    wid = None if pick == ALL_WH else int(whs.loc[whs["name"] == pick, "id"].iloc[0])
    return pick, wid


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("المخزن والأسعار والجرد", auth.client_name())
    t1, t2, t3, t4, t5 = st.tabs(["أرصدة المخزون", "انحراف الأسعار", "كارت الصنف", "حركة المخزن", "جرد المخازن"])

    with t1:
        pick, wid = _wh_picker(cid, "bal_wh")
        eq = reports.item_unit_equivalents(cid)
        if wid:
            df = reports.warehouse_balances(cid, wid)
            if df.empty:
                st.info("لا توجد أرصدة في هذا المخزن.")
            else:
                st.metric(f"قيمة مخزون «{pick}»", f"{df['القيمة'].sum():,.2f}")
                st.dataframe(df.round(3), use_container_width=True, hide_index=True)
        else:
            df = query("""SELECT id, name AS "الصنف", CASE is_semi WHEN 1 THEN 'نصف مصنع' ELSE 'خامة' END AS "النوع", unit AS "وحدة الاستهلاك",
                          qty_on_hand AS "الرصيد", avg_cost AS "متوسط التكلفة (للوحدة)",
                          qty_on_hand*CASE WHEN avg_cost>0 THEN avg_cost ELSE COALESCE(last_price,0) END AS "القيمة",
                          last_price AS "آخر سعر", min_qty AS "حد الطلب" FROM items WHERE client_id=? ORDER BY is_semi, name""", (cid,))
            if df.empty:
                st.info("لا توجد أصناف بعد.")
            else:
                df["بوحدة الشراء"] = df.apply(lambda r: f"{r['الرصيد'] / eq[int(r['id'])][1]:,.2f} {eq[int(r['id'])][0]}" if int(r["id"]) in eq else "", axis=1)
                c1, c2 = st.columns(2)
                c1.metric("إجمالي قيمة المخزون (كل المخازن)", f"{df['القيمة'].sum():,.2f}")
                neg = df[df["الرصيد"] < 0]
                c2.metric("أصناف برصيد سالب", len(neg))
                if not neg.empty:
                    st.error("رصيد سالب (مبيعات/صرف بدون رصيد مسجل بالمخزن): " + "، ".join(neg["الصنف"]))
                low = df[(df["حد الطلب"] > 0) & (df["الرصيد"] <= df["حد الطلب"]) & (df["الرصيد"] >= 0)]
                if not low.empty:
                    st.warning("وصلت لحد إعادة الطلب: " + "، ".join(low["الصنف"]))
                st.dataframe(df.drop(columns="id").round(3), use_container_width=True, hide_index=True)

    with t2:
        pv = price_variance(cid)
        if pv.empty:
            st.info("يلزم فاتورتا شراء على الأقل لنفس الخامة.")
        else:
            st.subheader("تغير آخر سعر شراء عن السابق (لكل وحدة استهلاك)")
            st.dataframe(pv, use_container_width=True, hide_index=True)
            st.subheader("أثر التغير على تكلفة الوجبة الواحدة")
            imp = price_impact(cid)
            if imp.empty:
                st.caption("لا توجد وجبات تستخدم الخامات المتغيرة.")
            else:
                st.dataframe(imp, use_container_width=True, hide_index=True)

    with t3:
        allitems = query("SELECT id, name FROM items WHERE client_id=? ORDER BY is_semi, name", (cid,))
        if allitems.empty:
            st.info("لا توجد أصناف.")
        else:
            a, b, c = st.columns(3)
            nm = a.selectbox("الصنف", allitems["name"], key="card_item")
            f = b.date_input("من", date(date.today().year, 1, 1), key="card_f")
            t = c.date_input("إلى", date.today(), key="card_t")
            pick, wid = _wh_picker(cid, "card_wh")
            item, card, opening, closing = reports.item_card(cid, int(allitems.loc[allitems["name"] == nm, "id"].iloc[0]), f, t, wid)
            st.markdown(f"#### كارت صنف: {item['name']} ({item['unit']}) — {pick}")
            m = st.columns(4)
            m[0].metric("رصيد أول الفترة", f"{opening:,.3f}")
            m[1].metric("إجمالي الوارد", f"{card['وارد'].sum() if not card.empty else 0:,.3f}")
            m[2].metric("إجمالي الصادر", f"{card['صادر'].sum() if not card.empty else 0:,.3f}")
            m[3].metric("رصيد آخر الفترة", f"{closing:,.3f}")
            st.caption(f"الرصيد الحالي: {item['current_qty']:,.3f} {item['unit']} — متوسط التكلفة المرجح للوحدة: {item['avg_cost']:,.4f}")
            if card.empty:
                st.info("لا توجد حركات في هذه الفترة.")
            else:
                st.dataframe(card.round(4), use_container_width=True, hide_index=True)
                st.download_button("تنزيل الكارت CSV", card.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"card_{item['name']}.csv", mime="text/csv")

    with t4:
        a, b, c = st.columns(3)
        f = a.date_input("من", date.today() - timedelta(days=30), key="mv_f")
        t = b.date_input("إلى", date.today(), key="mv_t")
        names = query("SELECT name FROM items WHERE client_id=? ORDER BY name", (cid,))["name"].tolist()
        pick_i = c.multiselect("الصنف", names)
        mv = query("""SELECT m.move_date AS "التاريخ", i.name AS "الصنف", m.move_type AS t, COALESCE(w.name,'') AS "المخزن",
                      m.qty AS "الكمية", m.unit_cost AS "تكلفة الوحدة", m.qty*m.unit_cost AS "القيمة", m.created_by AS "المستخدم"
                      FROM stock_movements m JOIN items i ON i.id=m.item_id LEFT JOIN warehouses w ON w.id=m.warehouse_id
                      WHERE m.client_id=? AND m.move_date BETWEEN ? AND ? ORDER BY m.move_date DESC, m.id DESC""", (cid, str(f), str(t)))
        mv["نوع الحركة"] = mv["t"].map(reports.MOVE_AR)
        if pick_i:
            mv = mv[mv["الصنف"].isin(pick_i)]
        st.dataframe(mv.drop(columns="t").round(4), use_container_width=True, hide_index=True)

    with t5:
        st.caption("الجرد لمخزن واحد في كل مرة. عند الاعتماد تُسوّى أرصدة هذا المخزن فقط بحركة «تسوية جرد» مسجلة باسمك.")
        pick, wid = _wh_picker(cid, "count_wh", with_all=False, default_ops=True)
        items = query("SELECT id, name, unit FROM items WHERE client_id=? ORDER BY is_semi, name", (cid,))
        if items.empty:
            st.info("لا توجد أصناف للجرد.")
        else:
            stock = query("SELECT item_id, qty FROM warehouse_stock WHERE warehouse_id=?", (wid,)).set_index("item_id")["qty"]
            day = st.date_input("تاريخ الجرد", date.today())
            grid = items.rename(columns={"name": "الصنف", "unit": "الوحدة"}).copy()
            grid["رصيد النظام"] = grid["id"].map(stock).fillna(0.0)
            grid["الكمية الفعلية"] = grid["رصيد النظام"]
            ed = st.data_editor(grid, hide_index=True, use_container_width=True, key=f"count_grid_{wid}",
                                disabled=["id", "الصنف", "الوحدة", "رصيد النظام"], column_config={"id": None})
            apply_adj = st.checkbox("اعتماد التسوية وتعديل أرصدة هذا المخزن حسب الجرد")
            if st.button("حفظ الجرد"):
                counts = {int(r["id"]): float(r["الكمية الفعلية"]) for _, r in ed.iterrows()}
                try:
                    _, diff = inventory.apply_stocktake(cid, wid, day, counts, user, apply_adj)
                    ed["الفرق"] = ed["الكمية الفعلية"] - ed["رصيد النظام"]
                    st.success(f"تم حفظ جرد «{pick}». صافي فرق القيمة: {diff:,.2f}")
                    bad = ed[ed["الفرق"].abs() > 1e-9][["الصنف", "رصيد النظام", "الكمية الفعلية", "الفرق"]]
                    if not bad.empty:
                        st.warning("فروق بين المخزون النظري والفعلي:")
                        st.dataframe(bad, use_container_width=True, hide_index=True)
                except ValueError as e:
                    st.error(str(e))
