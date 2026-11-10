from datetime import date, timedelta
import plotly.express as px
import streamlit as st

from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
from utils import theme, auth, treasury

ACC = {"cash": ("💵 الخزينة النقدية", "الوارد: مبيعات نقدية وقيود الإيداع. المنصرف: سداد نقدي لفواتير الشراء والدفعات والمصروفات."),
       "bank": ("🏦 حساب البنك", "الوارد: مبيعات الفيزا/الدفع الإلكتروني وإيداعات. المنصرف: مدفوعات بنكية (تحويل/فيزا) ورسوم.")}


def _account_tab(cid, user, account):
    title, hint = ACC[account]
    b = treasury.balance(cid, account)
    m = st.columns(4)
    m[0].metric("الرصيد الافتتاحي", f"{b['opening']:,.2f}")
    m[1].metric("إجمالي الوارد", f"{b['cash_in']:,.2f}")
    m[2].metric("إجمالي المنصرف", f"{b['cash_out']:,.2f}")
    m[3].metric("الرصيد الحالي", f"{b['balance']:,.2f}")
    if b["balance"] < 0:
        st.error("الرصيد سالب. غالبًا الرصيد الافتتاحي غير مسجل. اضبطه من تبويب «الإعدادات».")
    st.caption(hint)
    a, c, d = st.columns(3)
    f = a.date_input("من", date.today() - timedelta(days=30), key=f"tr_f_{account}")
    t = c.date_input("إلى", date.today(), key=f"tr_t_{account}")
    kind = d.selectbox("النوع", ["الكل", "وارد", "منصرف"], key=f"tr_k_{account}")
    led, opening = treasury.ledger(cid, f, t, {"وارد": "in", "منصرف": "out"}.get(kind), account)
    st.write(f"الرصيد في بداية الفترة: **{opening:,.2f}**")
    if led.empty:
        st.info("لا توجد حركات في هذه الفترة.")
        return
    show = led.rename(columns={"id": "رقم", "tx_date": "التاريخ", "category": "البند", "description": "التفاصيل", "in_amt": "وارد",
                               "out_amt": "منصرف", "balance": "الرصيد", "source": "المصدر", "created_by": "المستخدم"})
    _tbl10 = show[["رقم", "التاريخ", "البند", "التفاصيل", "وارد", "منصرف", "الرصيد", "المصدر", "المستخدم"]]
    st.dataframe(_tbl10.round(2), use_container_width=True, hide_index=True)
    export_buttons(_tbl10, f"كشف حساب {treasury.ACCOUNT_AR[account]}", f"ledger_{account}", subtitle=f"{auth.client_name()} — من {f} إلى {t} — الرصيد في بداية الفترة: {opening:,.2f}", formats={"وارد": FMT_MONEY, "منصرف": FMT_MONEY, "الرصيد": FMT_MONEY}, totals=["وارد", "منصرف"], key=f"led_{account}")
    daily = led.groupby(["tx_date", "direction"], as_index=False)["amount"].sum()
    daily["النوع"] = daily["direction"].map({"in": "وارد", "out": "منصرف"})
    fig = px.bar(daily, x="tx_date", y="amount", color="النوع", barmode="group", title="الحركة اليومية",
                 labels={"tx_date": "التاريخ", "amount": "المبلغ"}, color_discrete_map={"وارد": theme.GREEN, "منصرف": theme.GOLD})
    st.plotly_chart(theme.style_fig(fig), use_container_width=True)
    manual = led[led["source_type"] == "manual"]
    if not manual.empty:
        c1, c2 = st.columns([2, 1])
        tid = c1.selectbox("حذف قيد يدوي (رقم)", manual["id"], key=f"tr_del_{account}")
        if c2.button("حذف القيد", key=f"tr_delb_{account}"):
            try:
                treasury.delete_manual(int(tid), user, cid)
                st.rerun()
            except ValueError as e:
                st.error(str(e))


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("الخزينة والبنك", auth.client_name())
    bal = treasury.balances(cid)
    top = st.columns(2)
    top[0].metric("💵 الرصيد النقدي", f"{bal['cash']:,.2f}")
    top[1].metric("🏦 رصيد البنك", f"{bal['bank']:,.2f}")

    t1, t2, t3, t4, t5 = st.tabs(["💵 الخزينة النقدية", "🏦 حساب البنك", "تحويل بين الحسابين", "قيد يدوي", "الإعدادات"])
    with t1:
        _account_tab(cid, user, "cash")
    with t2:
        _account_tab(cid, user, "bank")

    with t3:
        st.caption("مثال: إيداع نقدية الخزينة في البنك، أو سحب من البنك للخزينة. تُسجَّل حركتان مرتبطتان في معاملة واحدة.")
        with st.form("tr_xfer", clear_on_submit=True):
            a, b, c = st.columns(3)
            src = a.selectbox("من", ["cash", "bank"], format_func=lambda x: treasury.ACCOUNT_AR[x])
            day = b.date_input("التاريخ", date.today())
            amt = c.number_input("المبلغ", min_value=0.0, step=100.0)
            note = st.text_input("البيان")
            if st.form_submit_button("تنفيذ التحويل"):
                try:
                    treasury.transfer_between(cid, day, src, amt, note, user)
                    st.success("تم التحويل.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with t4:
        acc = st.radio("الحساب", ["cash", "bank"], format_func=lambda x: treasury.ACCOUNT_AR[x], horizontal=True)
        direction = st.radio("نوع القيد", ["إيداع (وارد)", "سحب (منصرف)"], horizontal=True)
        d = "in" if direction.startswith("إيداع") else "out"
        with st.form("tr_manual", clear_on_submit=True):
            a, b3, c = st.columns(3)
            cat = a.selectbox("البند", treasury.MANUAL_IN if d == "in" else treasury.MANUAL_OUT)
            day = b3.date_input("التاريخ", date.today())
            amt = c.number_input("المبلغ", min_value=0.0, step=100.0)
            note = st.text_input("البيان")
            if st.form_submit_button("حفظ القيد"):
                try:
                    treasury.add_manual(cid, day, d, amt, cat, note, user, acc)
                    st.success("تم حفظ القيد.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        st.caption("المبيعات والمشتريات والمصروفات تصل تلقائيًا من شاشاتها حسب طريقة الدفع، ولا تحتاج قيدًا يدويًا.")

    with t5:
        c1, c2 = st.columns(2)
        new_cash = c1.number_input("الرصيد الافتتاحي للخزينة النقدية", value=float(treasury.opening_balance(cid, "cash")), step=100.0)
        new_bank = c2.number_input("الرصيد الافتتاحي لحساب البنك", value=float(treasury.opening_balance(cid, "bank")), step=100.0)
        if st.button("حفظ الأرصدة الافتتاحية"):
            try:
                treasury.set_opening(cid, new_cash, user, "cash")
                treasury.set_opening(cid, new_bank, user, "bank")
                st.success("تم الحفظ.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
