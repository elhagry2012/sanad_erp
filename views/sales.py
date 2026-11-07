from datetime import date, timedelta
import hashlib
import pandas as pd
import plotly.express as px
import streamlit as st

from database.db import query
from utils import theme, auth, services, reports, treasury

PAY_OPTS = {"cash": "💵 نقدي", "bank": "🏦 بنك / فيزا", "split": "مختلط (نقدي + بنك)", "none": "بدون تحصيل الآن (آجل)"}


def _options(key_prefix):
    """قناة الطلب وطريقة الدفع (مشتركة بين الإدخال اليدوي والاستيراد)."""
    c1, c2, c3 = st.columns(3)
    channel = c1.selectbox("قناة الطلب", list(services.CHANNELS), format_func=services.CHANNELS.get, key=f"{key_prefix}_ch")
    detail = c2.text_input("اسم التطبيق / التفاصيل", key=f"{key_prefix}_det",
                           placeholder="مثال: طلبات، مرسول" if channel == "aggregator" else "اختياري")
    pay = c3.selectbox("طريقة التحصيل", list(PAY_OPTS), format_func=PAY_OPTS.get, key=f"{key_prefix}_pay")
    bank_amt = 0.0
    if pay == "split":
        bank_amt = st.number_input("المبلغ المحصّل عبر البنك/الفيزا (الباقي نقدًا)", min_value=0.0, step=50.0, key=f"{key_prefix}_bank")
    return channel, detail, pay, bank_amt


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("مبيعات الكاشير", auth.client_name())
    dishes = query("SELECT id, name, price FROM dishes WHERE client_id=? ORDER BY name", (cid,))
    if dishes.empty:
        st.info("أنشئ الوجبات أولًا من صفحة «التصنيع والوصفات».")
        return
    ops = reports.warehouse_list(cid)
    ops_name = ops[ops["is_operations"] == 1]["name"].iloc[0] if (ops["is_operations"] == 1).any() else "—"
    st.caption(f"تُخصم مكونات الوصفات من مخزن التشغيل: **{ops_name}** (غيّره من صفحة المخازن). رصيد النقدية: "
               f"{treasury.balance(cid)['balance']:,.2f} — رصيد البنك: {treasury.balance(cid, 'bank')['balance']:,.2f}")
    t1, t2, t3, t4 = st.tabs(["إدخال المبيعات", "استيراد من ملف", "سجل المبيعات", "تحليل القنوات"])

    with t1:
        day = st.date_input("تاريخ المبيعات", date.today(), key="sale_day")
        channel, detail, pay, bank_amt = _options("man")
        grid = dishes[["id", "name", "price"]].rename(columns={"name": "الوجبة", "price": "السعر"}).copy()
        grid["الكمية المباعة"] = 0.0
        ed = st.data_editor(grid, hide_index=True, use_container_width=True, key="sales_grid",
                            disabled=["id", "الوجبة", "السعر"], column_config={"id": None})
        total = float((ed["السعر"] * ed["الكمية المباعة"]).sum())
        st.metric("إجمالي هذا الإدخال", f"{total:,.2f}")
        if st.button("تسجيل المبيعات وخصم المخزن", type="primary"):
            rows = [(day, int(r["id"]), r["الكمية المباعة"]) for _, r in ed.iterrows() if r["الكمية المباعة"] > 0]
            try:
                n, rev = services.record_sales(cid, rows, user, pay, bank_amt, channel, detail)
                st.success(f"تم تسجيل {n} سطر بإيراد {rev:,.2f} وخصم المكونات من مخزن التشغيل.")
            except ValueError as e:
                st.error(str(e))

    with t2:
        st.caption("ملف CSV أو Excel بأعمدة: الوجبة، الكمية، والتاريخ (اختياري). تنطبق القناة وطريقة التحصيل أدناه على كل الملف.")
        default_day = st.date_input("التاريخ الافتراضي", date.today(), key="imp_day")
        channel_i, detail_i, pay_i, bank_i = _options("imp")
        up = st.file_uploader("ملف المبيعات", type=["csv", "xlsx"])
        if up:
            raw = up.getvalue()
            try:
                df = pd.read_csv(up, encoding="utf-8-sig") if up.name.lower().endswith(".csv") else pd.read_excel(up)
                df.columns = [str(c).strip() for c in df.columns]
                ren = {"الوجبة": "dish", "dish": "dish", "الكمية": "qty", "qty": "qty", "التاريخ": "date", "date": "date"}
                df = df.rename(columns={c: ren[c] for c in df.columns if c in ren})
                if not {"dish", "qty"} <= set(df.columns):
                    st.error("لم أجد عمودي «الوجبة» و«الكمية».")
                    return
                df["dish"] = df["dish"].astype(str).str.strip()
                df["date"] = pd.to_datetime(df["date"]).dt.date if "date" in df.columns else default_day
                ids = dict(zip(dishes["name"], dishes["id"]))
                df["ok"] = df["dish"].isin(ids)
                bad = df[~df["ok"]]
                if not bad.empty:
                    st.warning("وجبات غير معروفة ستُتجاهل: " + "، ".join(sorted(set(bad["dish"]))))
                good = df[df["ok"]].groupby(["date", "dish"], as_index=False)["qty"].sum()
                st.dataframe(good, use_container_width=True, hide_index=True)
                sig = hashlib.md5(raw).hexdigest()
                if st.session_state.get("imported_sig") == sig:
                    st.info("تم استيراد هذا الملف في هذه الجلسة بالفعل.")
                elif st.button("اعتماد الاستيراد وخصم المخزن"):
                    rows = [(r["date"], ids[r["dish"]], r["qty"]) for _, r in good.iterrows()]
                    n, rev = services.record_sales(cid, rows, user, pay_i, bank_i, channel_i, detail_i)
                    st.session_state["imported_sig"] = sig
                    st.success(f"تم استيراد {n} سطر بإيراد {rev:,.2f}.")
            except ValueError as e:
                st.error(str(e))
            except Exception as e:  # ملف تالف أو تنسيق غير متوقع
                st.error(f"تعذّر قراءة الملف: {e}")

    with t3:
        a, b = st.columns(2)
        f = a.date_input("من", date.today() - timedelta(days=7), key="s_f")
        t = b.date_input("إلى", date.today(), key="s_t")
        log = query("""SELECT s.id, s.sale_date AS "التاريخ", d.name AS "الوجبة", s.qty AS "الكمية", s.unit_price AS "السعر",
                       s.qty*s.unit_price AS "الإيراد", COALESCE(s.channel,'') AS ch, COALESCE(s.channel_detail,'') AS "التطبيق",
                       COALESCE(s.cash_amount, CASE WHEN s.treasury_tx_id IS NOT NULL THEN s.qty*s.unit_price END) AS "نقدي",
                       s.bank_amount AS "بنك", s.created_by AS "المستخدم"
                       FROM pos_sales s JOIN dishes d ON d.id=s.dish_id
                       WHERE s.client_id=? AND s.sale_date BETWEEN ? AND ? ORDER BY s.sale_date DESC, s.id DESC""",
                    (cid, str(f), str(t)))
        log["القناة"] = log["ch"].map(services.CHANNELS).fillna("غير محدد")
        st.dataframe(log.drop(columns="ch"), use_container_width=True, hide_index=True, column_config={"id": "رقم"})
        if not log.empty:
            st.metric("إيراد الفترة", f"{log['الإيراد'].sum():,.0f}")
            c1, c2 = st.columns([2, 1])
            sid = c1.selectbox("إلغاء سطر (يعيد المكونات للمخزن ويعكس التحصيل)", log["id"], key="del_sale")
            if c2.button("إلغاء السطر"):
                try:
                    services.delete_sale(int(sid), user, cid)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with t4:
        a, b = st.columns(2)
        f = a.date_input("من", date.today() - timedelta(days=30), key="ch_f")
        t = b.date_input("إلى", date.today(), key="ch_t")
        ch = reports.channel_sales(cid, f, t)
        if ch.empty:
            st.info("لا توجد مبيعات في هذه الفترة.")
        else:
            ch["القناة"] = ch["channel"].map(services.CHANNELS).fillna("غير محدد (قبل تفعيل القنوات)")
            ch["التطبيق"] = ch["detail"]
            ch["الحصة %"] = (ch["revenue"] / ch["revenue"].sum() * 100).round(1)
            l, r = st.columns(2)
            by = ch.groupby("القناة", as_index=False)["revenue"].sum()
            l.plotly_chart(theme.style_fig(px.pie(by, names="القناة", values="revenue", hole=.5, title="حصة كل قناة من الإيراد")),
                           use_container_width=True)
            r.plotly_chart(theme.style_fig(px.bar(by, x="القناة", y="revenue", title="الإيراد حسب القناة", labels={"revenue": "الإيراد"})),
                           use_container_width=True)
            st.dataframe(ch[["القناة", "التطبيق", "qty", "revenue", "الحصة %"]].rename(columns={"qty": "الكمية", "revenue": "الإيراد"}).round(1),
                         use_container_width=True, hide_index=True)
