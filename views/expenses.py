from datetime import date, timedelta
import streamlit as st

from database.db import query
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
from utils import theme, auth, services, treasury


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("المصروفات", auth.client_name())
    t1, t2 = st.tabs(["تسجيل مصروف", "سجل المصروفات"])
    cats = query("SELECT id, name, nature FROM expense_categories ORDER BY name")

    with t1:
        with st.expander("إضافة بند مصروف جديد (إيجار، مرتبات، كهرباء...)", expanded=cats.empty):
            with st.form("cat_form", clear_on_submit=True):
                a, b = st.columns(2)
                n = a.text_input("اسم البند")
                k = b.selectbox("طبيعته", ["fixed", "variable"], format_func={"fixed": "ثابت", "variable": "متغير"}.get)
                if st.form_submit_button("إضافة البند"):
                    try:
                        services.add_expense_category(n, k, user)
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        if not cats.empty:
            bal = treasury.balances(cid)
            st.caption(f"رصيد النقدية: {bal['cash']:,.2f} — رصيد البنك: {bal['bank']:,.2f}")
            with st.form("exp_form", clear_on_submit=True):
                a, b, c, d = st.columns(4)
                cn = a.selectbox("البند", cats["name"])
                day = b.date_input("التاريخ", date.today())
                amt = c.number_input("المبلغ", min_value=0.0, step=100.0)
                note = d.text_input("ملاحظة")
                method = st.radio("طريقة الدفع", ["cash", "bank", "none"], format_func=treasury.METHOD_AR.get, horizontal=True)
                if st.form_submit_button("تسجيل المصروف"):
                    try:
                        services.add_expense(cid, int(cats.loc[cats["name"] == cn, "id"].iloc[0]), day, amt, note, user, method)
                        st.success("تم التسجيل.")
                    except ValueError as e:
                        st.error(str(e))

    with t2:
        a, b = st.columns(2)
        f = a.date_input("من", date.today() - timedelta(days=60), key="e_f")
        t = b.date_input("إلى", date.today(), key="e_t")
        df = query("""SELECT e.id, e.exp_date, c.name AS category, e.amount, e.note,
                      COALESCE(e.pay_method,'cash') AS pay_method, e.created_by
                      FROM expenses e JOIN expense_categories c ON c.id=e.category_id
                      WHERE e.client_id=? AND e.exp_date BETWEEN ? AND ? ORDER BY e.exp_date DESC, e.id DESC""", (cid, str(f), str(t)))
        if df.empty:
            st.info("لا توجد مصروفات في هذه الفترة.")
        else:
            st.metric("إجمالي المصروفات", f"{df['amount'].sum():,.0f}")
            df["الدفع"] = df["pay_method"].map(treasury.METHOD_AR)
            _det = df.drop(columns=["pay_method"]).rename(columns={"id": "رقم", "exp_date": "التاريخ", "category": "البند", "amount": "المبلغ", "note": "ملاحظة", "created_by": "سجّله"})
            _sum = df.groupby("category", as_index=False)["amount"].sum().rename(columns={"category": "البند", "amount": "المبلغ"})
            export_buttons(sections=[Section("سجل المصروفات", _det, {"المبلغ": FMT_MONEY}, ["المبلغ"]), Section("الملخص حسب البند", _sum, {"المبلغ": FMT_MONEY}, ["المبلغ"])],
                           title="سجل المصروفات", filename="expenses", subtitle=f"{auth.client_name()} — من {f} إلى {t}", key="exp_exp")
            ed = st.data_editor(df.drop(columns="pay_method"), hide_index=True, use_container_width=True, key="ed_exp",
                                disabled=["id", "category", "created_by", "الدفع"],
                                column_config={"id": None, "exp_date": "التاريخ", "category": "البند", "amount": "المبلغ",
                                               "note": "ملاحظة", "created_by": "سجّله"})
            if st.button("حفظ تعديلات المصروفات"):
                try:
                    n = services.apply_edits("expenses", df.drop(columns=["pay_method", "الدفع"]), ed.drop(columns=["الدفع"]), user, cid)
                    st.success(f"تم حفظ {n} تعديل.") if n else st.info("لا توجد تعديلات.")
                except ValueError as e:
                    st.error(str(e))
            c1, c2 = st.columns([2, 1])
            eid = c1.selectbox("حذف مصروف (رقم السجل)", df["id"])
            if c2.button("حذف"):
                try:
                    services.delete_expense(int(eid), user, cid)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
