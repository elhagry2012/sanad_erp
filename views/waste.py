from datetime import date, timedelta
import plotly.express as px
import streamlit as st

from database.db import query
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
from utils import theme, auth, services, reports
from utils.reports import _f


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("الهدر والتالف", auth.client_name())
    whs = reports.warehouse_list(cid)
    wh_names = dict(zip(whs["name"], whs["id"]))
    ops_idx = int(whs.index[whs["is_operations"] == 1][0]) if (whs["is_operations"] == 1).any() else 0
    t1, t2, t3 = st.tabs(["تسجيل هدر", "سجل الهدر", "تحليل الهدر"])

    with t1:
        kind = st.radio("نوع الهدر", ["خامة / منتج نصف مصنع", "وجبة جاهزة"], horizontal=True)
        items = query("SELECT id, name, unit FROM items WHERE client_id=? ORDER BY name", (cid,))
        dishes = query("SELECT id, name FROM dishes WHERE client_id=? ORDER BY name", (cid,))
        units = query("SELECT u.item_id, u.unit_name FROM item_units u JOIN items i ON i.id=u.item_id WHERE i.client_id=?", (cid,))
        a, b, c = st.columns(3)
        wname = a.selectbox("يُخصم من مخزن", list(wh_names), index=ops_idx)
        day = b.date_input("التاريخ", date.today())
        reason = c.selectbox("السبب", services.WASTE_REASONS)
        if kind.startswith("خامة"):
            if items.empty:
                st.info("لا توجد أصناف.")
            else:
                x, y, z = st.columns(3)
                nm = x.selectbox("الصنف", items["name"])
                row = items[items["name"] == nm].iloc[0]
                opts = [row["unit"]] + list(units[units["item_id"] == row["id"]]["unit_name"])
                unit = y.selectbox("الوحدة", opts)
                qty = z.number_input("الكمية", min_value=0.0, step=0.5, format="%.3f")
                if st.button("تسجيل الهدر", type="primary"):
                    try:
                        cost = services.record_waste(cid, day, int(row["id"]), qty, reason, user, wh_names[wname], unit)
                        st.success(f"تم التسجيل. قيمة الهدر {cost:,.2f} وخُصمت الكمية من المخزن.")
                    except ValueError as e:
                        st.error(str(e))
        else:
            if dishes.empty:
                st.info("لا توجد وجبات.")
            else:
                x, y = st.columns(2)
                nm = x.selectbox("الوجبة", dishes["name"])
                qty = y.number_input("عدد الوجبات", min_value=0.0, step=1.0)
                st.caption("تُخصم مكونات الوجبة من المخزن وتُسجَّل بتكلفتها.")
                if st.button("تسجيل هدر الوجبات", type="primary"):
                    try:
                        cost = services.record_waste_dish(cid, day, int(dishes.loc[dishes["name"] == nm, "id"].iloc[0]), qty, reason, user, wh_names[wname])
                        st.success(f"تم التسجيل. قيمة الهدر {cost:,.2f}.")
                    except ValueError as e:
                        st.error(str(e))

    def load(f, t):
        return query("""SELECT w.id, COALESCE(w.doc_no,'') AS doc_no, w.waste_date AS d, COALESCE(i.name, 'وجبة: ' || dd.name) AS what, w.qty, COALESCE(i.unit,'وجبة') AS unit,
                        w.reason, w.cost, COALESCE(wh.name,'') AS wh, w.created_by FROM waste_log w
                        LEFT JOIN items i ON i.id=w.item_id LEFT JOIN dishes dd ON dd.id=w.dish_id
                        LEFT JOIN warehouses wh ON wh.id=w.warehouse_id
                        WHERE w.client_id=? AND w.waste_date BETWEEN ? AND ? ORDER BY w.waste_date DESC, w.id DESC""", (cid, str(f), str(t)))

    with t2:
        a, b = st.columns(2)
        f = a.date_input("من", date.today() - timedelta(days=30), key="w_f")
        t = b.date_input("إلى", date.today(), key="w_t")
        df = load(f, t)
        if df.empty:
            st.info("لا يوجد هدر مسجل في هذه الفترة.")
        else:
            st.metric("قيمة الهدر", f"{df['cost'].sum():,.2f}")
            _tbl13 = df.drop(columns="id").rename(columns={"doc_no": "رقم المستند", "d": "التاريخ", "what": "البند", "qty": "الكمية", "unit": "الوحدة", "reason": "السبب",
                                            "cost": "القيمة", "wh": "المخزن", "created_by": "سجّله"})
            st.dataframe(_tbl13.round(3), use_container_width=True, hide_index=True)
            export_buttons(_tbl13, "سجل الهدر والتالف", "waste_log", subtitle=f"{auth.client_name()} — من {f} إلى {t}", formats={"الكمية": FMT_QTY, "القيمة": FMT_MONEY}, totals=["القيمة"], key="waste_log")
            c1, c2 = st.columns([2, 1])
            wnos = dict(zip(df["id"], df["doc_no"]))
            wid = c1.selectbox("حذف تسجيل (يعيد الكمية للمخزن)", df["id"], key="w_del", format_func=lambda i: wnos.get(i) or f"#{i}")
            if c2.button("حذف"):
                try:
                    services.delete_waste(int(wid), user, cid)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with t3:
        a, b = st.columns(2)
        f = a.date_input("من", date.today() - timedelta(days=30), key="wa_f")
        t = b.date_input("إلى", date.today(), key="wa_t")
        df = load(f, t)
        if df.empty:
            st.info("لا توجد بيانات.")
        else:
            cogs = _f(query("SELECT SUM(s.qty*s.unit_price) v FROM pos_sales s WHERE s.client_id=? AND s.sale_date BETWEEN ? AND ?",
                            (cid, str(f), str(t)))["v"].iloc[0])
            m = st.columns(3)
            m[0].metric("إجمالي الهدر", f"{df['cost'].sum():,.0f}")
            m[1].metric("عدد التسجيلات", len(df))
            m[2].metric("نسبة الهدر من المبيعات", f"{df['cost'].sum() / cogs * 100:.1f}%" if cogs else "—")
            l, r = st.columns(2)
            by_reason = df.groupby("reason", as_index=False)["cost"].sum()
            l.plotly_chart(theme.style_fig(px.pie(by_reason, names="reason", values="cost", hole=.5, title="الهدر حسب السبب")), use_container_width=True)
            top = df.groupby("what", as_index=False)["cost"].sum().sort_values("cost", ascending=False).head(10)
            r.plotly_chart(theme.style_fig(px.bar(top, x="cost", y="what", orientation="h", title="أعلى 10 بنود هدرًا",
                                                  labels={"cost": "القيمة", "what": ""})), use_container_width=True)
            daily = df.groupby("d", as_index=False)["cost"].sum()
            export_buttons(sections=[
                Section("الهدر حسب السبب", by_reason.rename(columns={"reason": "السبب", "cost": "القيمة"}), {"القيمة": FMT_MONEY}, ["القيمة"]),
                Section("أعلى البنود هدرًا", top.rename(columns={"what": "البند", "cost": "القيمة"}), {"القيمة": FMT_MONEY}, ["القيمة"]),
                Section("الهدر اليومي", daily.rename(columns={"d": "اليوم", "cost": "القيمة"}), {"القيمة": FMT_MONEY}, ["القيمة"])],
                title="تحليل الهدر والتالف", filename="waste_analysis", subtitle=f"{auth.client_name()} — من {f} إلى {t}", key="waste_an")
            st.plotly_chart(theme.style_fig(px.bar(daily, x="d", y="cost", title="الهدر اليومي", labels={"d": "اليوم", "cost": "القيمة"})),
                            use_container_width=True)
