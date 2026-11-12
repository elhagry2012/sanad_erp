"""الضوابط المالية - للمدير العام: إقفال الفترات، حدود التنبيه للحركات الكبيرة، إعدادات الضريبة."""
from datetime import date
import streamlit as st

from database.db import query
from utils import theme, auth, controls, sequences, inventory


def render():
    auth.require_admin()
    me = auth.username()
    theme.header("الضوابط المالية", "إقفال الفترات • تنبيهات الحركات الكبيرة • إعدادات الضريبة • ترقيم المستندات (المدير العام فقط)")
    clients = query("SELECT id, name FROM clients ORDER BY name")
    if clients.empty:
        st.info("لا توجد مطاعم.")
        return
    cname = st.selectbox("المطعم", clients["name"], key="ctl_client")
    cid = int(clients.loc[clients["name"] == cname, "id"].iloc[0])
    t1, t2, t3, t4, t5 = st.tabs(["🔒 إقفال الفترات", "🚨 تنبيهات الحركات الكبيرة", "🧾 إعدادات الضريبة", "🔢 ترقيم المستندات",
                                  "📦 سياسة صرف المخزون"])

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

    with t4:
        _sequences_tab(cid, me)

    with t5:
        st.info("تحدد أي دفعة تُصرف أولًا عند البيع والإنتاج والتحويل والهدر. «الأقرب انتهاءً» (FEFO) هو الأنسب للأغذية: "
                "يقلل التالف لأن الدفعة التي ستنتهي أولًا تُستهلك أولًا (والدفعات بلا تاريخ صلاحية آخرًا). "
                "«الأقدم استلامًا» (FIFO) يصرف حسب تاريخ الاستلام بغض النظر عن الصلاحية. "
                "التغيير يسري على الصرف القادم فقط، والمرتجع المرتبط بفاتورة يُخصم من دفعة الفاتورة نفسها أولًا في الحالتين.")
        cur = str(controls.get_setting(None, cid, "issue_policy", "fefo") or "fefo")
        keys = list(inventory.ISSUE_POLICIES)
        pol = st.radio("سياسة الصرف", keys, index=keys.index(cur) if cur in keys else 0, format_func=inventory.ISSUE_POLICIES.get,
                       key="issue_pol")
        if st.button("حفظ سياسة الصرف", key="issue_pol_save"):
            controls.set_setting(cid, "issue_policy", pol, me)
            st.success("تم الحفظ.")


def _sequences_tab(cid, me):
    st.info("لكل نوع مستند تسلسل مستقل لهذا المطعم. الرقم يُحجز داخل نفس معاملة الحفظ فلا يتكرر حتى مع إدخال عدة مستخدمين في نفس اللحظة، "
            "ويُحسب من تاريخ المستند (سنويًا: PI-2026-00001، شهريًا: PI-202611-00001، أو تسلسل مستمر: PI-00001). "
            "تغيير الإعداد يؤثر على المستندات الجديدة فقط؛ المستندات السابقة تحتفظ بأرقامها.")
    df = sequences.settings_df(cid)
    st.dataframe(df.drop(columns="doc_type").assign(**{"إعادة البدء": df["إعادة البدء"].map(sequences.RESET_AR)}),
                 use_container_width=True, hide_index=True)
    st.markdown("**تعديل تسلسل**")
    t = st.selectbox("نوع المستند", list(df["doc_type"]), format_func=lambda k: sequences.DOC_SEQ[k][0], key="seq_t")
    row = df[df["doc_type"] == t].iloc[0]
    a, b, c = st.columns(3)
    prefix = a.text_input("البادئة", value=row["البادئة"], key=f"seq_p_{t}", help="حتى 12 حرفًا بدون مسافات، ويمكن تركها فارغة")
    padding = b.number_input("عدد الأرقام", 1, 10, int(row["عدد الأرقام"]), key=f"seq_w_{t}")
    resets = list(sequences.RESET_AR)
    reset = c.selectbox("إعادة البدء", resets, index=resets.index(row["إعادة البدء"]), format_func=sequences.RESET_AR.get, key=f"seq_r_{t}")
    change_start = st.checkbox("تحديد رقم بداية للمستند التالي في الفترة الحالية (مثلًا لاستكمال ترقيم دفتر ورقي)", key=f"seq_s_{t}")
    start = st.number_input("رقم المستند التالي", min_value=1, step=1, value=int(row["آخر رقم في الفترة الحالية"]) + 1,
                            key=f"seq_n_{t}") if change_start else None
    sample = sequences._render(prefix.strip(), reset, sequences._period(reset, date.today()),
                               int(start or row["آخر رقم في الفترة الحالية"] + 1), int(padding))
    st.caption(f"شكل الرقم التالي: **{sample}**")
    if st.button("حفظ إعداد الترقيم", type="primary", key="seq_save"):
        try:
            sequences.save_settings(cid, t, prefix, padding, reset, me, start_at=start)
            st.success("تم الحفظ.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))
