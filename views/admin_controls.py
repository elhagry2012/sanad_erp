"""الضوابط المالية - للمدير العام: إقفال الفترات، حدود التنبيه للحركات الكبيرة، إعدادات الضريبة."""
from datetime import date
import streamlit as st

from database.db import query
from utils import theme, auth, controls


def render():
    auth.require_admin()
    me = auth.username()
    theme.header("الضوابط المالية", "إقفال الفترات • تنبيهات الحركات الكبيرة • إعدادات الضريبة (المدير العام فقط)")
    clients = query("SELECT id, name FROM clients ORDER BY name")
    if clients.empty:
        st.info("لا توجد مطاعم.")
        return
    cname = st.selectbox("المطعم", clients["name"], key="ctl_client")
    cid = int(clients.loc[clients["name"] == cname, "id"].iloc[0])
    t1, t2, t3 = st.tabs(["🔒 إقفال الفترات", "🚨 تنبيهات الحركات الكبيرة", "🧾 إعدادات الضريبة"])

    with t1:
        st.info("الفترة المقفلة تمنع إضافة أو تعديل أو إلغاء أي مستند بتاريخ داخلها (مشتريات، مبيعات، مصروفات، هدر، تحويلات، قيود خزينة، جرد، مرتجعات)، "
                "بما فيها عمليات الإلغاء. افتحها مؤقتًا إن احتجت تصحيحًا ثم أعد إقفالها. كل إقفال وفتح يُسجَّل في سجل الأمان.")
        c1, c2 = st.columns(2)
        today = date.today()
        year = c1.number_input("السنة", 2020, 2100, today.year, step=1, key="lk_y")
        month = c2.number_input("الشهر", 1, 12, today.month, step=1, key="lk_m")
        note = st.text_input("ملاحظة الإقفال (اختياري)", key="lk_note")
        period = f"{int(year)}-{int(month):02d}"
        if st.button(f"🔒 إقفال {period}", type="primary"):
            try:
                controls.lock_period(cid, period, me, note)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        locks = controls.list_locks(cid)
        if locks.empty:
            st.caption("لا توجد فترات مقفلة.")
        else:
            st.dataframe(locks.rename(columns={"period": "الفترة", "locked_by": "أقفلها", "locked_at": "وقت الإقفال", "note": "ملاحظة"}),
                         use_container_width=True, hide_index=True)
            pick = st.selectbox("فتح فترة", locks["period"], key="ul_pick")
            if st.button("🔓 فتح الفترة المحددة"):
                try:
                    controls.unlock_period(cid, pick, me)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with t2:
        st.caption("عند تسجيل حركة تساوي أو تتجاوز الحد المضبوط يصلك إشعار فوري (0 = معطّل).")
        vals = {}
        for key, label in controls.THRESHOLDS.items():
            cur = float(controls.get_setting(None, cid, key, 0) or 0)
            vals[key] = st.number_input(f"الحد الأدنى للتنبيه: {label}", min_value=0.0, value=cur, step=100.0, key=f"thr_{key}")
        if st.button("حفظ الحدود"):
            for k, v in vals.items():
                controls.set_setting(cid, k, v, me)
            st.success("تم الحفظ.")

    with t3:
        rate = float(controls.get_setting(None, cid, "vat_rate", 14) or 14)
        incl = str(controls.get_setting(None, cid, "sales_prices_include_vat", "1")) == "1"
        nr = st.number_input("نسبة ضريبة القيمة المضافة على المبيعات %", 0.0, 100.0, rate, step=0.5, key="vat_r")
        ni = st.checkbox("أسعار بيع الوجبات شاملة الضريبة", value=incl, key="vat_i")
        if st.button("حفظ إعدادات الضريبة"):
            controls.set_setting(cid, "vat_rate", nr, me)
            controls.set_setting(cid, "sales_prices_include_vat", "1" if ni else "0", me)
            st.success("تم الحفظ. تعتمد عليها التقارير الضريبية.")
