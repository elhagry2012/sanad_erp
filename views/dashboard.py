from datetime import date, datetime, timedelta
import pandas as pd
import plotly.express as px
import streamlit as st

from database.db import query
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
from utils import theme, auth, reports, treasury as treasury_mod
from utils.costing import dish_costs, price_variance
from utils.services import CHANNELS


def _start_of(period):
    today = date.today()
    return {"آخر 30 يوم": today - timedelta(days=30), "آخر 90 يوم": today - timedelta(days=90),
            "الشهر الحالي": today.replace(day=1)}[period]


def _stock_alerts(cid, compact=False):
    """مؤشرات بصرية للخامات التي تقترب من النفاد حسب معدل الاستهلاك الفعلي."""
    ls = reports.low_stock(cid)
    if ls.empty:
        return 0
    alert = ls[ls["level"] >= 1].copy()
    crit = int((ls["level"] >= 2).sum())
    if compact:
        return crit
    st.subheader("تنبيهات المخزون (حسب معدل الاستهلاك)")
    if alert.empty:
        st.success("🟢 كل الأصناف بمستوى تغطية آمن.")
    else:
        m = st.columns(3)
        m[0].metric("🔴 نفد", int((ls["level"] == 3).sum()))
        m[1].metric("🟠 حرج", int((ls["level"] == 2).sum()))
        m[2].metric("🟡 يقترب", int((ls["level"] == 1).sum()))
        alert["days"] = alert["days_left"].replace(float("inf"), 999).clip(upper=14)
        st.dataframe(
            alert.rename(columns={"name": "الصنف", "unit": "الوحدة", "qty_on_hand": "الرصيد", "daily": "استهلاك يومي",
                                  "min_qty": "حد الطلب", "status": "الحالة"})[["الحالة", "الصنف", "الوحدة", "الرصيد", "استهلاك يومي", "days", "حد الطلب"]]
            .rename(columns={"days": "أيام التغطية (حتى 14)"}).round(2),
            use_container_width=True, hide_index=True,
            column_config={"أيام التغطية (حتى 14)": st.column_config.ProgressColumn(min_value=0, max_value=14, format="%.1f")})
    return crit


def _live_panel(cid):
    """مؤشرات اليوم الحية: مبيعات، مصروفات، حركة الخزينة والبنك، الربح التقديري، وآخر العمليات (تحديث تلقائي)."""
    st.markdown("### ⚡ اليوم لحظيًا")
    auto = st.checkbox("تحديث تلقائي كل 30 ثانية", value=True, key="live_auto")

    def body():
        lv = reports.live_today(cid)
        m = st.columns(5)
        m[0].metric("مبيعات اليوم", f"{lv['sales']:,.0f}", f"{lv['sales_lines']} سطر")
        m[1].metric("مصروفات اليوم", f"{lv['expenses']:,.0f}")
        m[2].metric("💵 حركة النقدية اليوم", f"{lv['cash_in'] - lv['cash_out']:,.0f}", f"+{lv['cash_in']:,.0f} / -{lv['cash_out']:,.0f}", delta_color="off")
        m[3].metric("🏦 حركة البنك اليوم", f"{lv['bank_in'] - lv['bank_out']:,.0f}", f"+{lv['bank_in']:,.0f} / -{lv['bank_out']:,.0f}", delta_color="off")
        m[4].metric("الربح التقديري اليوم", f"{lv['profit']:,.0f}", help="المبيعات - تكلفة البضاعة المباعة - المصروفات - الهدر")
        if not lv["recent"].empty:
            r = lv["recent"].rename(columns={"ts": "الوقت", "username": "المستخدم", "action": "العملية", "entity": "النوع", "details": "التفاصيل", "amount": "القيمة"})
            from utils.audit import ACTION_AR, ENTITY_AR
            r["العملية"] = r["العملية"].map(ACTION_AR).fillna(r["العملية"])
            r["النوع"] = r["النوع"].map(ENTITY_AR).fillna(r["النوع"])
            with st.expander("آخر العمليات في هذا المطعم", expanded=False):
                st.dataframe(r, use_container_width=True, hide_index=True)
        st.caption(f"آخر تحديث: {datetime.now():%H:%M:%S}")
    frag = getattr(st, "fragment", None)
    if frag:
        frag(run_every=30 if auto else None)(body)()
    else:
        body()


def _expiry_panel(cid):
    """تنبيه حي بالأصناف التي قاربت صلاحيتها (أسبوعان) + ملخص الراكد."""
    ex = reports.expiring_lots(cid, 14)
    dead = reports.dead_stock(cid, 90)
    risk = reports.expiry_risk(cid, 30)
    at = risk[risk["معرّض للتلف"] > 1e-9] if not risk.empty else risk
    if ex.empty and dead.empty and at.empty:
        return
    st.subheader("⏳ تنبيهات الصلاحية والركود")
    if not at.empty:
        st.warning(f"📉 {len(at)} دفعة لن تُستهلك بالكامل قبل انتهاء صلاحيتها بمعدل استهلاك آخر 30 يومًا وحسب ترتيب الصرف — "
                   f"قيمة المعرّض للتلف {at['قيمة المعرّض للتلف'].sum():,.2f}. التفاصيل: المخزن ← الدفعات والصلاحية.")
    if not ex.empty:
        m = st.columns(3)
        m[0].metric("🔴 منتهية الصلاحية", int((ex["days_left"] < 0).sum()))
        m[1].metric("🟠 خلال أسبوع", int(((ex["days_left"] >= 0) & (ex["days_left"] <= 7)).sum()))
        m[2].metric("🟡 خلال أسبوعين", int((ex["days_left"] > 7).sum()))
        if (ex["days_left"] < 0).any():
            st.error("⚠️ توجد دفعات منتهية الصلاحية ما زالت بالمخزن. سجّلها كهدر من «الهدر والتالف» وأرجع المشتريات التالفة للمورد من «مرتجعات المشتريات».")
        show = ex.rename(columns={"status": "الحالة", "item": "الصنف", "unit": "الوحدة", "warehouse": "المخزن", "qty": "الرصيد", "expiry_date": "تاريخ الصلاحية",
                                  "days_left": "الأيام المتبقية", "value": "القيمة"})[["الحالة", "الصنف", "الوحدة", "المخزن", "الرصيد", "تاريخ الصلاحية", "الأيام المتبقية", "القيمة"]]
        st.dataframe(show.round(2), use_container_width=True, hide_index=True)
        export_buttons(show, "الأصناف التي قاربت صلاحيتها", "expiring_items", subtitle=auth.client_name(), formats={"الرصيد": FMT_QTY, "القيمة": FMT_MONEY}, totals=["القيمة"], key="dash_exp")
    if not dead.empty:
        st.info(f"📉 {len(dead)} صنف راكد (بلا استهلاك 90 يومًا فأكثر) بقيمة {dead['value'].sum():,.0f}. التفاصيل: مركز التقارير ← المخزون ← الأصناف الراكدة.")


def _overview(period):
    """نظرة المدير العام: كل المطاعم مجمّعة ثم كل مطعم على حدة (مع رصيد الخزينة والبنك)."""
    theme.header("نظرة عامة على كل المطاعم", period)
    clients = query("SELECT id, name FROM clients ORDER BY name")
    if clients.empty:
        st.info("لا توجد مطاعم بعد. أضف أول مطعم من صفحة «العملاء».")
        return
    start = _start_of(period)
    rows = []
    for _, c in clients.iterrows():
        m = reports.client_summary(int(c["id"]), start)
        m["alerts"] = _stock_alerts(int(c["id"]), compact=True)
        m["exp"] = int(len(reports.expiring_lots(int(c["id"]), 14)))
        rows.append({"المطعم": c["name"], **m})
    df = pd.DataFrame(rows)

    st.subheader("الإجمالي لكل المطاعم")
    t = st.columns(7)
    t[0].metric("المبيعات", f"{df['revenue'].sum():,.0f}")
    t[1].metric("تكلفة البضاعة", f"{df['cogs'].sum():,.0f}")
    t[2].metric("مجمل الربح", f"{df['gross'].sum():,.0f}")
    t[3].metric("المصروفات والهدر", f"{(df['opex'] + df['waste']).sum():,.0f}")
    t[4].metric("صافي الربح", f"{df['net'].sum():,.0f}")
    t[5].metric("💵 الرصيد النقدي", f"{df['cash'].sum():,.0f}")
    t[6].metric("🏦 رصيد البنك", f"{df['bank'].sum():,.0f}")

    long = df.melt(id_vars="المطعم", value_vars=["revenue", "gross", "net"], var_name="البند", value_name="القيمة")
    long["البند"] = long["البند"].map({"revenue": "المبيعات", "gross": "مجمل الربح", "net": "صافي الربح"})
    st.plotly_chart(theme.style_fig(px.bar(long, x="المطعم", y="القيمة", color="البند", barmode="group", title="مقارنة المطاعم")),
                    use_container_width=True)

    st.subheader("كل مطعم على حدة")
    for _, r in df.iterrows():
        with st.container(border=True):
            st.markdown(f"**{r['المطعم']}**" + (f"  —  🔴 {int(r['alerts'])} صنف مخزون حرج" if r["alerts"] else "") +
                        (f"  —  ⏳ {int(r['exp'])} دفعة قاربت الصلاحية" if r["exp"] else ""))
            k = st.columns(6)
            k[0].metric("المبيعات", f"{r['revenue']:,.0f}")
            k[1].metric("مجمل الربح", f"{r['gross']:,.0f}")
            k[2].metric("صافي الربح", f"{r['net']:,.0f}")
            k[3].metric("💵 الرصيد النقدي", f"{r['cash']:,.0f}")
            k[4].metric("🏦 رصيد البنك", f"{r['bank']:,.0f}")
            k[5].metric("مستحق للموردين", f"{r['payables']:,.0f}")
            if r["cash"] < 0 or r["bank"] < 0:
                st.warning("رصيد سالب: راجع الرصيد الافتتاحي أو القيود.")
    _tbl1 = df.drop(columns=["alerts", "exp"]).rename(columns={
        "revenue": "المبيعات", "cogs": "تكلفة البضاعة", "gross": "مجمل الربح", "opex": "المصروفات", "waste": "الهدر",
        "net": "صافي الربح", "cash": "النقدية", "bank": "البنك", "stock_value": "قيمة المخزون", "payables": "مستحق للموردين"})
    st.dataframe(_tbl1.round(0), use_container_width=True, hide_index=True)
    export_buttons(_tbl1, "ملخص أداء كل المطاعم", "all_restaurants_summary", subtitle=f"{period} (من {start})", formats={c: FMT_MONEY for c in ["المبيعات", "تكلفة البضاعة", "مجمل الربح", "المصروفات", "الهدر", "صافي الربح", "النقدية", "البنك", "قيمة المخزون", "مستحق للموردين"]}, totals=["المبيعات", "تكلفة البضاعة", "مجمل الربح", "المصروفات", "الهدر", "صافي الربح", "النقدية", "البنك"], key="ov_exp")


def render():
    period = st.radio("الفترة", ["آخر 30 يوم", "آخر 90 يوم", "الشهر الحالي"], horizontal=True)
    if auth.is_admin() and not auth.current_client_id():
        _overview(period)
        return
    cid = auth.require_client()
    theme.header(f"لوحة التحكم - {auth.client_name()}", "ملخص الأداء المالي")
    start = _start_of(period)
    m = reports.client_summary(cid, start)

    a = st.columns(5)
    a[0].metric("إجمالي المبيعات", f"{m['revenue']:,.0f}")
    a[1].metric("تكلفة البضاعة المباعة", f"{m['cogs']:,.0f}")
    a[2].metric("مجمل الربح", f"{m['gross']:,.0f}", f"{m['gross'] / m['revenue'] * 100:.1f}%" if m["revenue"] else None)
    a[3].metric("المصروفات والهدر", f"{m['opex'] + m['waste']:,.0f}")
    a[4].metric("صافي الربح", f"{m['net']:,.0f}", f"{m['net'] / m['revenue'] * 100:.1f}%" if m["revenue"] else None)
    b = st.columns(4)
    b[0].metric("💵 الرصيد النقدي (الخزينة)", f"{m['cash']:,.2f}")
    b[1].metric("🏦 رصيد البنك", f"{m['bank']:,.2f}")
    b[2].metric("قيمة المخزون", f"{m['stock_value']:,.0f}")
    b[3].metric("مستحق للموردين", f"{m['payables']:,.0f}")
    if m["cash"] < 0 or m["bank"] < 0:
        st.warning("رصيد سالب في الخزينة أو البنك. راجع الرصيد الافتتاحي والقيود من صفحة «الخزينة والبنك».")

    cw = reports.cost_warnings(cid)
    if not cw.empty:
        st.warning("⚠️ أصناف داخلة في وصفات بدون أي تكلفة (تكلفة الوجبات أقل من الحقيقة): " + "، ".join(cw["الصنف"]) +
                   ". سجّل لها فاتورة شراء.")

    _live_panel(cid)
    _expiry_panel(cid)
    _stock_alerts(cid)
    with st.expander("📑 تصدير القوائم المالية (قائمة الدخل • المصروفات • الخزينة والبنك • مستحقات الموردين)"):
        fs = reports.income_statement(cid, start, date.today())
        export_buttons(sections=[
            Section("قائمة الدخل", fs["statement"], {"المبلغ": FMT_MONEY, "% من المبيعات": "0.0"}),
            Section("المصروفات حسب البند", fs["expenses"], {"المبلغ": FMT_MONEY}, ["المبلغ"]),
            Section("أرصدة الخزينة والبنك", fs["cash_bank"], {"الرصيد": FMT_MONEY}),
            Section("مستحقات الموردين", fs["payables"], {"الرصيد المستحق": FMT_MONEY}, ["الرصيد المستحق"])],
            title="القوائم المالية", filename="financial_statements", subtitle=f"{auth.client_name()} — {period} (من {start} إلى {date.today()})", key="fin_stmt")

    sales = query("""SELECT s.sale_date, s.qty, s.unit_price, d.id AS dish_id, d.name, COALESCE(s.channel,'unknown') AS channel
                     FROM pos_sales s JOIN dishes d ON d.id=s.dish_id WHERE s.client_id=? AND s.sale_date>=?""", (cid, str(start)))
    if sales.empty:
        st.info("لا توجد مبيعات في هذه الفترة. سجّلها من صفحة «مبيعات الكاشير».")
    else:
        costs = dish_costs(cid).set_index("dish_id")["cost"]
        sales["cost"] = sales["dish_id"].map(costs).fillna(0) * sales["qty"]
        sales["revenue"] = sales["qty"] * sales["unit_price"]
        l, r = st.columns(2)
        daily = sales.groupby("sale_date", as_index=False)[["revenue", "cost"]].sum().rename(columns={"revenue": "المبيعات", "cost": "التكلفة"})
        l.plotly_chart(theme.style_fig(px.line(daily, x="sale_date", y=["المبيعات", "التكلفة"], title="المبيعات مقابل التكلفة يوميًا",
                       labels={"sale_date": "التاريخ", "value": "المبلغ", "variable": ""})), use_container_width=True)
        ch = sales.groupby("channel", as_index=False)["revenue"].sum()
        ch["القناة"] = ch["channel"].map({**CHANNELS, "unknown": "غير محدد"})
        r.plotly_chart(theme.style_fig(px.pie(ch, names="القناة", values="revenue", hole=.5, title="المبيعات حسب قناة الطلب")),
                       use_container_width=True)

    st.subheader("تكلفة الوجبات والهامش")
    dc = dish_costs(cid)
    if dc.empty:
        st.info("لا توجد وجبات بعد. أنشئها من صفحة «التصنيع والوصفات».")
    else:
        dc["هامش الوجبة"] = dc["price"] - dc["cost"]
        dc["نسبة التكلفة %"] = (dc["cost"] / dc["price"] * 100).round(1)
        st.dataframe(dc.rename(columns={"name": "الوجبة", "category": "التصنيف", "price": "سعر البيع", "cost": "التكلفة",
                                        "missing_cost": "مكونات بلا تكلفة"}).drop(columns="dish_id").round(2),
                     use_container_width=True, hide_index=True)

    st.subheader("انحراف أسعار الخامات (لكل وحدة استهلاك)")
    pv = price_variance(cid)
    if pv.empty:
        st.info("يلزم فاتورتا شراء على الأقل لنفس الصنف لإظهار الانحراف.")
    else:
        st.dataframe(pv, use_container_width=True, hide_index=True)
        if pv.iloc[0]["التغير %"] > 5:
            st.warning(f"ارتفع سعر {pv.iloc[0]['الصنف']} بنسبة {pv.iloc[0]['التغير %']}% عن الشراء السابق.")

    exp = query("""SELECT c.name, e.amount FROM expenses e JOIN expense_categories c ON c.id=e.category_id
                   WHERE e.client_id=? AND e.exp_date>=?""", (cid, str(start)))
    if not exp.empty:
        e = exp.groupby("name", as_index=False)["amount"].sum()
        st.plotly_chart(theme.style_fig(px.pie(e, names="name", values="amount", hole=.5, title="توزيع المصروفات")),
                        use_container_width=True)
