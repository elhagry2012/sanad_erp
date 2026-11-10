"""مركز التقارير: قسم مستقل لكل التقارير المالية والضريبية والمخزنية والإدارية، مع صلاحية لكل فئة وتصدير Excel/PDF."""
from datetime import date, timedelta
import pandas as pd
import streamlit as st

from database.db import query
from utils import theme, auth, reports, services, treasury, security
from utils.costing import dish_costs, price_variance
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST

CATEGORIES = {"rep_financial": "💰 التقارير المالية", "rep_tax": "🧾 الضريبية والإقفال",
              "rep_inventory": "📦 المخزون والصلاحية", "rep_management": "🎯 الإدارية والتحليلية"}


def _period(key, default_days=30):
    a, b = st.columns(2)
    f = a.date_input("من", date.today() - timedelta(days=default_days), key=f"{key}_f")
    t = b.date_input("إلى", date.today(), key=f"{key}_t")
    return f, t


def _show(sections, title, filename, subtitle, key, note=""):
    """يعرض الأقسام ثم زري التصدير (قسم واحد أو أكثر)."""
    shown = False
    for sec in sections:
        if sec.df is None or sec.df.empty:
            continue
        shown = True
        if len(sections) > 1:
            st.markdown(f"**{sec.name}**")
        st.dataframe(sec.df.round(3), use_container_width=True, hide_index=True)
    if not shown:
        st.info("لا توجد بيانات لهذه الفترة.")
        return
    export_buttons(sections=sections, title=title, filename=filename, subtitle=subtitle, key=key, note=note)


# ---------------------------------------------------------------- المالية
def _income(cid):
    f, t = _period("inc")
    fs = reports.income_statement(cid, f, t)
    _show([Section("قائمة الدخل", fs["statement"], {"المبلغ": FMT_MONEY, "% من المبيعات": "0.0"}),
           Section("المصروفات حسب البند", fs["expenses"], {"المبلغ": FMT_MONEY}, ["المبلغ"]),
           Section("أرصدة الخزينة والبنك", fs["cash_bank"], {"الرصيد": FMT_MONEY}),
           Section("مستحقات الموردين", fs["payables"], {"الرصيد المستحق": FMT_MONEY}, ["الرصيد المستحق"])],
          "القوائم المالية", "financial_statements", f"{auth.client_name()} — من {f} إلى {t}", "rc_inc")


def _ledger(cid, account):
    f, t = _period(f"led_{account}")
    led, opening = treasury.ledger(cid, f, t, None, account)
    st.caption(f"الرصيد في بداية الفترة: {opening:,.2f}")
    if led.empty:
        st.info("لا توجد حركات.")
        return
    show = led.rename(columns={"id": "رقم", "tx_date": "التاريخ", "category": "البند", "description": "التفاصيل", "in_amt": "وارد",
                               "out_amt": "منصرف", "balance": "الرصيد", "source": "المصدر", "created_by": "المستخدم"})[
        ["رقم", "التاريخ", "البند", "التفاصيل", "وارد", "منصرف", "الرصيد", "المصدر", "المستخدم"]]
    _show([Section("الحركة", show, {"وارد": FMT_MONEY, "منصرف": FMT_MONEY, "الرصيد": FMT_MONEY}, ["وارد", "منصرف"])],
          f"كشف {treasury.ACCOUNT_AR[account]}", f"ledger_{account}", f"{auth.client_name()} — من {f} إلى {t} — رصيد أول الفترة {opening:,.2f}", f"rc_led_{account}")


def _sales_daily(cid):
    f, t = _period("sd")
    d = query("""SELECT s.sale_date AS "التاريخ", SUM(s.qty) AS "الكمية", SUM(s.qty*s.unit_price) AS "المبيعات",
                 SUM(COALESCE(s.cash_amount,0)) AS "نقدي", SUM(COALESCE(s.bank_amount,0)) AS "بنك" FROM pos_sales s
                 WHERE s.client_id=? AND s.sale_date BETWEEN ? AND ? GROUP BY s.sale_date ORDER BY s.sale_date""", (cid, str(f), str(t)))
    by = query("""SELECT d.name AS "الوجبة", SUM(s.qty) AS "الكمية", SUM(s.qty*s.unit_price) AS "المبيعات" FROM pos_sales s
                  JOIN dishes d ON d.id=s.dish_id WHERE s.client_id=? AND s.sale_date BETWEEN ? AND ? GROUP BY d.id ORDER BY 3 DESC""", (cid, str(f), str(t)))
    ch = reports.channel_sales(cid, f, t)
    if not ch.empty:
        ch["القناة"] = ch["channel"].map({**services.CHANNELS, "unknown": "غير محدد"}).fillna("غير محدد")
        ch = ch[["القناة", "detail", "qty", "revenue"]].rename(columns={"detail": "التطبيق", "qty": "الكمية", "revenue": "المبيعات"})
    _show([Section("المبيعات اليومية", d, {"الكمية": FMT_QTY}, ["الكمية", "المبيعات", "نقدي", "بنك"]),
           Section("حسب الوجبة", by, {"الكمية": FMT_QTY}, ["الكمية", "المبيعات"]), Section("حسب قناة الطلب", ch, {"الكمية": FMT_QTY}, ["المبيعات"])],
          "تقرير المبيعات", "sales_report", f"{auth.client_name()} — من {f} إلى {t}", "rc_sales")


def _purchases(cid):
    f, t = _period("pu")
    by_sup = query("""SELECT s.name AS "المورد", COUNT(*) AS "عدد الفواتير", SUM(p.subtotal) AS "قبل الضريبة", SUM(p.vat_amount) AS "ضريبة القيمة المضافة",
                      SUM(p.wht_amount) AS "ضريبة الخصم", SUM(p.total) AS "الإجمالي", SUM(p.paid) AS "المدفوع"
                      FROM purchase_invoices p JOIN suppliers s ON s.id=p.supplier_id WHERE p.client_id=? AND COALESCE(p.status,'active')='active'
                      AND p.invoice_date BETWEEN ? AND ? GROUP BY s.id ORDER BY 6 DESC""", (cid, str(f), str(t)))
    by_item = query("""SELECT i.name AS "الصنف", SUM(l.qty*COALESCE(l.unit_factor,1)) AS "الكمية (وحدة الاستهلاك)", i.unit AS "الوحدة", SUM(l.qty*l.unit_price) AS "القيمة"
                       FROM purchase_lines l JOIN purchase_invoices p ON p.id=l.invoice_id JOIN items i ON i.id=l.item_id
                       WHERE p.client_id=? AND COALESCE(p.status,'active')='active' AND p.invoice_date BETWEEN ? AND ? GROUP BY i.id ORDER BY 4 DESC""", (cid, str(f), str(t)))
    _show([Section("حسب المورد", by_sup, {"عدد الفواتير": "#,##0"}, ["عدد الفواتير", "قبل الضريبة", "ضريبة القيمة المضافة", "ضريبة الخصم", "الإجمالي", "المدفوع"]),
           Section("حسب الصنف", by_item, {"الكمية (وحدة الاستهلاك)": FMT_QTY}, ["القيمة"]),
           Section("مرتجعات الفترة", reports.returns_list(cid, f, t), {}, ["قبل الضريبة", "الإجمالي", "صافي الإشعار الدائن", "المسترد"])],
          "تقرير المشتريات", "purchases_report", f"{auth.client_name()} — من {f} إلى {t}", "rc_pur")


def _expenses(cid):
    f, t = _period("ex", 60)
    d = query("""SELECT c.name AS "البند", CASE c.nature WHEN 'fixed' THEN 'ثابت' ELSE 'متغير' END AS "الطبيعة", COUNT(*) AS "العدد", SUM(e.amount) AS "المبلغ"
                 FROM expenses e JOIN expense_categories c ON c.id=e.category_id WHERE e.client_id=? AND e.exp_date BETWEEN ? AND ? GROUP BY c.id ORDER BY 4 DESC""", (cid, str(f), str(t)))
    m = query("""SELECT substr(e.exp_date,1,7) AS "الشهر", SUM(e.amount) AS "المبلغ" FROM expenses e WHERE e.client_id=? AND e.exp_date BETWEEN ? AND ?
                 GROUP BY 1 ORDER BY 1""", (cid, str(f), str(t)))
    _show([Section("حسب البند", d, {"العدد": "#,##0"}, ["العدد", "المبلغ"]), Section("حسب الشهر", m, {}, ["المبلغ"])],
          "تقرير المصروفات", "expenses_report", f"{auth.client_name()} — من {f} إلى {t}", "rc_exp")


def _supplier_balances(cid):
    b = services.supplier_balances(cid).drop(columns="id")
    _show([Section("أرصدة الموردين", b, {"الرصيد المستحق": FMT_MONEY}, ["الرصيد المستحق"])], "أرصدة الموردين", "supplier_balances",
          auth.client_name(), "rc_sb", "موجب = مستحق للمورد، سالب = رصيد لنا عند المورد.")


# ---------------------------------------------------------------- الضريبية
def _tax(cid):
    f, t = _period("tax")
    tx = reports.tax_summary(cid, f, t)
    st.caption(f"نسبة ضريبة المبيعات المضبوطة لهذا المطعم: {tx['rate']:g}% — أسعار البيع {'شاملة' if tx['includes_vat'] else 'غير شاملة'} للضريبة "
               "(تُضبط من «الضوابط المالية» للمدير العام).")
    st.warning("هذا ملخص عمل يساعد المحاسب على إعداد الإقرار، وليس إقرارًا رسميًا. راجعه مع المحاسب القانوني قبل التقديم.")
    _show([Section("الملخص الضريبي", tx["summary"], {"المبلغ": FMT_MONEY}),
           Section("ضريبة الخصم حسب المورد", tx["withholding"], {"قيمة المشتريات": FMT_MONEY, "ضريبة الخصم": FMT_MONEY}, ["قيمة المشتريات", "ضريبة الخصم"]),
           Section("تفصيل فواتير المشتريات (ضريبة المدخلات)", tx["invoices"], {}, ["قبل الضريبة", "ضريبة القيمة المضافة", "ضريبة الخصم", "الإجمالي"])],
          "الملخص الضريبي", "tax_summary", f"{auth.client_name()} — من {f} إلى {t}", "rc_tax")


def _closing(cid):
    y = st.number_input("السنة", min_value=2020, max_value=2100, value=date.today().year, step=1, key="cl_year")
    df = reports.monthly_closing(cid, int(y))
    fm = {c: FMT_MONEY for c in df.columns if c != "الشهر"}
    from utils import controls
    locks = set(controls.list_locks(cid)["period"])
    df.insert(1, "الحالة", df["الشهر"].map(lambda p: "🔒 مقفل" if p in locks else "مفتوح"))
    _show([Section(f"الإقفال الشهري {int(y)}", df, fm, ["المبيعات", "ضريبة المبيعات", "ضريبة المشتريات", "صافي ضريبة القيمة المضافة", "ضريبة الخصم", "المصروفات", "صافي الربح"])],
          f"الإقفال الشهري والسنوي {int(y)}", "monthly_closing", auth.client_name(), "rc_closing",
          note="الإقفال الفعلي (منع التعديل بتواريخ الشهر) يتم من «الضوابط المالية» للمدير العام.")


# ---------------------------------------------------------------- المخزون
def _valuation(cid):
    m = reports.warehouse_matrix(cid)
    if m.empty:
        st.info("لا توجد أرصدة.")
        return
    fm = {c: FMT_QTY for c in m.columns if c not in ("الصنف", "وحدة الاستهلاك", "تكلفة الوحدة", "القيمة")}
    fm.update({"تكلفة الوحدة": FMT_COST, "القيمة": FMT_MONEY})
    _show([Section("تقييم المخزون حسب المخزن", m, fm, ["القيمة"])], "تقييم المخزون", "stock_valuation", auth.client_name(), "rc_val")


def _expiry(cid):
    days = st.slider("إظهار ما تنتهي صلاحيته خلال (يوم)", 7, 90, 14, key="exp_days")
    df = reports.expiring_lots(cid, days)
    if df.empty:
        st.success("لا توجد دفعات تنتهي صلاحيتها في هذا النطاق.")
        return
    show = df.rename(columns={"status": "الحالة", "item": "الصنف", "unit": "الوحدة", "warehouse": "المخزن", "qty": "الرصيد",
                              "expiry_date": "تاريخ الصلاحية", "days_left": "الأيام المتبقية", "unit_cost": "تكلفة الوحدة", "value": "القيمة"})[
        ["الحالة", "الصنف", "الوحدة", "المخزن", "الرصيد", "تاريخ الصلاحية", "الأيام المتبقية", "تكلفة الوحدة", "القيمة"]]
    _show([Section("دفعات قاربت صلاحيتها", show, {"الرصيد": FMT_QTY, "تكلفة الوحدة": FMT_COST, "القيمة": FMT_MONEY, "الأيام المتبقية": "#,##0"}, ["القيمة"])],
          "الأصناف التي قاربت صلاحيتها", "expiry_report", f"{auth.client_name()} — خلال {days} يومًا", "rc_exp_lots")


def _lots(cid):
    df = reports.lots_df(cid)
    _show([Section("الدفعات وتواريخ الصلاحية", df, {"الرصيد": FMT_QTY, "تكلفة الوحدة": FMT_COST, "أيام للانتهاء": "#,##0"})],
          "الدفعات وتواريخ الصلاحية", "lots_report", auth.client_name(), "rc_lots")


def _dead(cid):
    days = st.slider("يُعدّ راكدًا إن لم يُستهلك منذ (يوم)", 30, 365, 60, step=10, key="dead_days")
    df = reports.dead_stock(cid, days)
    if df.empty:
        st.success("لا توجد أصناف راكدة بهذه المدة.")
        return
    show = df.rename(columns={"name": "الصنف", "unit": "الوحدة", "qty_on_hand": "الرصيد", "value": "قيمة الراكد", "last_out": "آخر استهلاك",
                              "last_in": "آخر توريد", "idle_days": "أيام الركود", "class": "التصنيف"})[
        ["الصنف", "الوحدة", "الرصيد", "قيمة الراكد", "آخر استهلاك", "آخر توريد", "أيام الركود", "التصنيف"]]
    show["أيام الركود"] = show["أيام الركود"].where(show["أيام الركود"] < 9999, None)
    st.metric("إجمالي قيمة المخزون الراكد", f"{show['قيمة الراكد'].sum():,.2f}")
    _show([Section("الأصناف الراكدة", show, {"الرصيد": FMT_QTY, "قيمة الراكد": FMT_MONEY, "أيام الركود": "#,##0"}, ["قيمة الراكد"])],
          "تقرير الأصناف الراكدة (Dead Stock)", "dead_stock", f"{auth.client_name()} — بلا استهلاك {days} يومًا فأكثر", "rc_dead",
          note="«آخر استهلاك» = آخر بيع/تصنيع/هدر. الصنف بلا أي استهلاك يُقاس من آخر توريد.")


def _reorder(cid):
    ls = reports.low_stock(cid)
    if ls.empty:
        st.info("لا توجد أصناف.")
        return
    show = ls.rename(columns={"status": "الحالة", "name": "الصنف", "unit": "الوحدة", "qty_on_hand": "الرصيد", "daily": "استهلاك يومي",
                              "days_left": "أيام التغطية", "min_qty": "حد الطلب"})[["الحالة", "الصنف", "الوحدة", "الرصيد", "استهلاك يومي", "أيام التغطية", "حد الطلب"]]
    show["أيام التغطية"] = show["أيام التغطية"].replace(float("inf"), None)
    _show([Section("حالة المخزون حسب الاستهلاك", show, {"الرصيد": FMT_QTY, "استهلاك يومي": FMT_QTY, "حد الطلب": FMT_QTY, "أيام التغطية": "#,##0.0"})],
          "تقرير حد الطلب والتغطية", "reorder_report", auth.client_name(), "rc_reorder")


def _movements(cid):
    f, t = _period("mv", 14)
    mv = query("""SELECT m.move_date AS "التاريخ", i.name AS "الصنف", m.move_type AS t, COALESCE(w.name,'') AS "المخزن", m.qty AS "الكمية",
                  m.unit_cost AS "تكلفة الوحدة", m.created_by AS "المستخدم" FROM stock_movements m JOIN items i ON i.id=m.item_id
                  LEFT JOIN warehouses w ON w.id=m.warehouse_id WHERE m.client_id=? AND m.move_date BETWEEN ? AND ? ORDER BY m.id DESC""", (cid, str(f), str(t)))
    mv["نوع الحركة"] = mv["t"].map(reports.MOVE_AR)
    _show([Section("حركة المخزن", mv.drop(columns="t"), {"الكمية": FMT_QTY, "تكلفة الوحدة": FMT_COST})], "حركة المخزن", "movements",
          f"{auth.client_name()} — من {f} إلى {t}", "rc_mv")


def _stocktakes(cid):
    h = reports.stocktake_history(cid)
    if h.empty:
        st.info("لا توجد عمليات جرد.")
        return
    labels = [f"#{r['id']} — {r['count_date']} — {r['warehouse']} ({r['diff_count']} فرق من {r['lines_count']})" for _, r in h.iterrows()]
    pick = st.selectbox("اختر جردًا", labels, key="rc_st_pick")
    lines = reports.stocktake_lines(int(h.iloc[labels.index(pick)]["id"]))
    _show([Section("بنود الجرد", lines, {"رصيد النظام": FMT_QTY, "الكمية الفعلية": FMT_QTY, "الفرق": FMT_QTY, "قيمة الفرق": FMT_MONEY}, ["قيمة الفرق"])],
          "تقرير الجرد", "stocktake_report", f"{auth.client_name()} — {pick}", "rc_st")


# ---------------------------------------------------------------- الإدارية
def _menu(cid):
    f, t = _period("me")
    df, meta = reports.menu_engineering(cid, f, t)
    if df.empty or meta.get("total_qty", 0) == 0:
        st.info("لا توجد مبيعات في الفترة.")
        return
    show = df.rename(columns={"name": "الوجبة", "class": "الفئة", "price": "السعر", "cost": "التكلفة", "cm": "هامش الوحدة", "qty": "الكمية",
                              "mix_pct": "الشعبية %", "total_cm": "إجمالي الهامش", "advice": "التوصية"})[
        ["الوجبة", "الفئة", "السعر", "التكلفة", "هامش الوحدة", "الكمية", "الشعبية %", "إجمالي الهامش", "التوصية"]]
    _show([Section("هندسة القائمة", show, {"الكمية": FMT_QTY, "الشعبية %": "0.0"}, ["إجمالي الهامش"])], "هندسة القائمة", "menu_engineering",
          f"{auth.client_name()} — من {f} إلى {t}", "rc_menu")


def _waste(cid):
    f, t = _period("wa")
    d = query("""SELECT w.waste_date AS "التاريخ", COALESCE(i.name, 'وجبة: ' || dd.name) AS "البند", w.qty AS "الكمية", w.reason AS "السبب", w.cost AS "القيمة"
                 FROM waste_log w LEFT JOIN items i ON i.id=w.item_id LEFT JOIN dishes dd ON dd.id=w.dish_id
                 WHERE w.client_id=? AND w.waste_date BETWEEN ? AND ? ORDER BY w.waste_date DESC""", (cid, str(f), str(t)))
    r = query("""SELECT reason AS "السبب", COUNT(*) AS "العدد", SUM(cost) AS "القيمة" FROM waste_log WHERE client_id=? AND waste_date BETWEEN ? AND ?
                 GROUP BY reason ORDER BY 3 DESC""", (cid, str(f), str(t)))
    _show([Section("حسب السبب", r, {"العدد": "#,##0"}, ["العدد", "القيمة"]), Section("التفصيل", d, {"الكمية": FMT_QTY}, ["القيمة"])],
          "تقرير الهدر والتالف", "waste_report", f"{auth.client_name()} — من {f} إلى {t}", "rc_waste")


def _dish_costs(cid):
    dc = dish_costs(cid)
    if dc.empty:
        st.info("لا توجد وجبات.")
        return
    dc["الهامش"] = dc["price"] - dc["cost"]
    dc["نسبة التكلفة %"] = (dc["cost"] / dc["price"] * 100).round(1)
    show = dc.rename(columns={"name": "الوجبة", "category": "التصنيف", "price": "السعر", "cost": "التكلفة", "missing_cost": "مكونات بلا تكلفة"}).drop(columns="dish_id")
    _show([Section("تكلفة الوجبات", show, {"نسبة التكلفة %": "0.0"})], "تكلفة الوجبات", "dish_costs", auth.client_name(), "rc_dc")


def _price_var(cid):
    pv = price_variance(cid)
    _show([Section("انحراف الأسعار", pv, {"السعر السابق": FMT_COST, "السعر الحالي": FMT_COST, "التغير %": "0.0"})], "انحراف أسعار الخامات", "price_variance", auth.client_name(), "rc_pv")


REPORTS = {
    "rep_financial": [("القوائم المالية (قائمة الدخل)", _income), ("كشف الخزينة النقدية", lambda c: _ledger(c, "cash")),
                      ("كشف حساب البنك", lambda c: _ledger(c, "bank")), ("المبيعات (يومي / وجبة / قناة)", _sales_daily),
                      ("المشتريات والمرتجعات", _purchases), ("المصروفات", _expenses), ("أرصدة الموردين", _supplier_balances)],
    "rep_tax": [("الملخص الضريبي للفترة", _tax), ("الإقفال الشهري والسنوي", _closing)],
    "rep_inventory": [("تقييم المخزون (كل المخازن)", _valuation), ("الأصناف التي قاربت صلاحيتها", _expiry), ("الدفعات وتواريخ الصلاحية", _lots),
                      ("الأصناف الراكدة (Dead Stock)", _dead), ("حد الطلب والتغطية", _reorder), ("حركة المخزن", _movements), ("سجل الجرد", _stocktakes)],
    "rep_management": [("هندسة القائمة", _menu), ("الهدر والتالف", _waste), ("تكلفة الوجبات", _dish_costs), ("انحراف أسعار الخامات", _price_var)],
}


def render():
    cid = auth.require_client()
    theme.header("مركز التقارير", auth.client_name())
    user = auth.username()
    allowed = [k for k in CATEGORIES if security.can_view(user, k)]
    if not allowed:
        st.warning("لا توجد فئات تقارير متاحة لحسابك. تواصل مع المدير العام.")
        return
    cat = st.radio("الفئة", allowed, format_func=CATEGORIES.get, horizontal=True, key="rc_cat")
    names = [n for n, _ in REPORTS[cat]]
    pick = st.selectbox("التقرير", names, key=f"rc_pick_{cat}")
    st.divider()
    dict(REPORTS[cat])[pick](cid)
