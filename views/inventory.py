from datetime import date, timedelta
import streamlit as st

from database.db import query
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
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


def _lots_tab(cid):
    """الرصيد المتبقي لكل تاريخ صلاحية + تنبيهات الصلاحية المبنية على ترتيب الصرف ومعدل الاستهلاك."""
    pol = reports._policy(cid)
    st.caption(f"كل كمية بالمخزن تتبع دفعة (سطر شراء، تشغيلة إنتاج، تحويل، تسوية جرد، أو رصيد افتتاحي). "
               f"سياسة الصرف الحالية: **{inventory.ISSUE_POLICIES[pol]}** — «ترتيب الصرف» يوضح أي دفعة تُصرف أولًا.")
    a, b = st.columns(2)
    with a:
        pick, wid = _wh_picker(cid, "lots_wh")
    names = query("SELECT id, name FROM items WHERE client_id=? ORDER BY is_semi, name", (cid,))
    iname = b.selectbox("الصنف", ["كل الأصناف"] + list(names["name"]), key="lots_item")
    iid = None if iname == "كل الأصناف" else int(names.loc[names["name"] == iname, "id"].iloc[0])

    st.subheader("⏳ تنبيهات الصلاحية (حسب ترتيب الصرف ومعدل الاستهلاك)")
    lb = st.slider("معدل الاستهلاك محسوب من آخر (يوم)", 7, 90, 30, step=1, key="lots_lb")
    risk = reports.expiry_risk(cid, lb, wid)
    if not risk.empty and iid:
        risk = risk[risk["الصنف"] == iname]
    if risk.empty:
        st.success("لا توجد دفعات ذات تاريخ صلاحية.")
    else:
        at = risk[risk["معرّض للتلف"] > 1e-9]
        m = st.columns(4)
        m[0].metric("🔴 دفعات منتهية بها رصيد", int((risk["الأيام المتبقية"] < 0).sum()))
        m[1].metric("🟠 تنتهي خلال 7 أيام", int(((risk["الأيام المتبقية"] >= 0) & (risk["الأيام المتبقية"] <= 7)).sum()))
        m[2].metric("دفعات لن تُستهلك بالكامل قبل انتهائها", len(at))
        m[3].metric("قيمة المعرّض للتلف", f"{at['قيمة المعرّض للتلف'].sum():,.2f}")
        if not at.empty:
            st.warning("هذه الدفعات لن تُستهلك بالكامل قبل انتهاء صلاحيتها بمعدل الاستهلاك الحالي — "
                       "استخدمها أولًا في الإنتاج/العروض، أو حوّلها لمخزن التشغيل، أو أرجعها للمورد:")
            st.dataframe(at.round(3), use_container_width=True, hide_index=True)
        with st.expander("كل الدفعات ذات تاريخ صلاحية مع توقع استهلاكها"):
            st.dataframe(risk.round(3), use_container_width=True, hide_index=True)
        export_buttons(risk, "توقع استهلاك الدفعات قبل انتهاء الصلاحية", "expiry_risk", subtitle=f"{auth.client_name()} — {pick}",
                       formats={"الرصيد": FMT_QTY, "يُستهلك قبل الانتهاء": FMT_QTY, "معرّض للتلف": FMT_QTY, "قيمة المعرّض للتلف": FMT_MONEY},
                       totals=["قيمة المعرّض للتلف"], key="lots_risk_exp")

    st.subheader("📦 الرصيد المتبقي حسب تاريخ الصلاحية")
    bd = reports.expiry_breakdown(cid, wid, iid)
    if bd.empty:
        st.info("لا يوجد رصيد.")
        return
    bad = bd[bd["تاريخ الصلاحية"] == "غير مطابق"]
    if not bad.empty:
        st.error("أرصدة لا تقابلها دفعات (عادة رصيد سالب بسبب صرف بدون رصيد مسجل): " + "، ".join(bad["الصنف"] + " / " + bad["المخزن"]) +
                 ". تُسوّى بالجرد أو بتسجيل المشتريات الناقصة.")
    if iid:
        tot = bd[bd["تاريخ الصلاحية"] != "غير مطابق"]
        cols = st.columns(min(len(tot), 6) or 1)
        for col, (_, r) in zip(cols, tot.head(6).iterrows()):
            col.metric(f"{r['تاريخ الصلاحية']} — {r['المخزن']}", f"{r['الكمية المتبقية']:,.3f} {r['الوحدة']}", r["الحالة"], delta_color="off")
    st.dataframe(bd, use_container_width=True, hide_index=True)
    export_buttons(bd, "الرصيد المتبقي حسب تاريخ الصلاحية", "expiry_breakdown", subtitle=f"{auth.client_name()} — {pick} — {iname}",
                   formats={"الكمية المتبقية": FMT_QTY, "القيمة": FMT_MONEY}, totals=["القيمة"], key="lots_bd_exp")


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("المخزن والأسعار والجرد", auth.client_name())
    t1, t6, t2, t3, t4, t5 = st.tabs(["أرصدة المخزون", "📅 الدفعات والصلاحية", "انحراف الأسعار", "كارت الصنف", "حركة المخزن", "جرد المخازن"])

    with t6:
        _lots_tab(cid)

    with t1:
        pick, wid = _wh_picker(cid, "bal_wh")
        eq = reports.item_unit_equivalents(cid)
        if wid:
            df = reports.warehouse_balances(cid, wid)
            if df.empty:
                st.info("لا توجد أرصدة في هذا المخزن.")
            else:
                st.metric(f"قيمة مخزون «{pick}»", f"{df['القيمة'].sum():,.2f}")
                _tbl7 = df
                st.dataframe(_tbl7.round(3), use_container_width=True, hide_index=True)
                export_buttons(_tbl7, f"أرصدة مخزن {pick}", "stock_wh", subtitle=f"{auth.client_name()} — {pick}", formats={"الرصيد": FMT_QTY, "تكلفة الوحدة": FMT_COST, "القيمة": FMT_MONEY}, totals=["القيمة"], key="inv_wh")
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
                _tbl6 = df.drop(columns="id")
                st.dataframe(_tbl6.round(3), use_container_width=True, hide_index=True)
                export_buttons(_tbl6, "الجرد المجمّع للمخزون (كل المخازن)", "inventory_consolidated", subtitle=f"{auth.client_name()} — كل المخازن", formats={"الرصيد": FMT_QTY, "متوسط التكلفة (للوحدة)": FMT_COST, "القيمة": FMT_MONEY, "آخر سعر": FMT_COST, "حد الطلب": FMT_QTY}, totals=["القيمة"], key="inv_all")

    with t2:
        pv = price_variance(cid)
        if pv.empty:
            st.info("يلزم فاتورتا شراء على الأقل لنفس الخامة.")
        else:
            st.subheader("تغير آخر سعر شراء عن السابق (لكل وحدة استهلاك)")
            _tbl5 = pv
            st.dataframe(_tbl5, use_container_width=True, hide_index=True)
            export_buttons(_tbl5, "انحراف أسعار الخامات", "price_variance", formats={"السعر السابق": FMT_COST, "السعر الحالي": FMT_COST, "التغير %": "0.0"}, key="inv_pv")
            st.subheader("أثر التغير على تكلفة الوجبة الواحدة")
            imp = price_impact(cid)
            if imp.empty:
                st.caption("لا توجد وجبات تستخدم الخامات المتغيرة.")
            else:
                _tbl4 = imp
                st.dataframe(_tbl4, use_container_width=True, hide_index=True)
                export_buttons(_tbl4, "أثر تغير الأسعار على تكلفة الوجبات", "price_impact", formats={"أثر التغير على تكلفة الوجبة": FMT_MONEY, "التغير %": "0.0"}, key="inv_pi")

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
                _tbl3 = card
                st.dataframe(_tbl3.round(4), use_container_width=True, hide_index=True)
                export_buttons(_tbl3, f"كارت صنف: {item['name']} ({item['unit']})", "item_card", subtitle=f"{auth.client_name()} — {pick} — من {f} إلى {t} — رصيد أول الفترة {opening:,.3f}", formats={"وارد": FMT_QTY, "صادر": FMT_QTY, "الرصيد": FMT_QTY, "تكلفة الوحدة": FMT_COST, "قيمة الحركة": FMT_MONEY}, totals=["وارد", "صادر", "قيمة الحركة"], key="inv_card")

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
        _tbl2 = mv.drop(columns="t")
        st.dataframe(_tbl2.round(4), use_container_width=True, hide_index=True)
        export_buttons(_tbl2, "حركة المخزن", "stock_movements", subtitle=f"{auth.client_name()} — من {f} إلى {t}", formats={"الكمية": FMT_QTY, "تكلفة الوحدة": FMT_COST, "القيمة": FMT_MONEY}, key="inv_mv")

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
            sheet = grid[["الصنف", "الوحدة", "رصيد النظام"]].assign(**{"الكمية الفعلية": None, "ملاحظات": None})
            st.caption("للجرد الورقي: صدّر ورقة الجرد، اطبعها، سجّل الكميات الفعلية، ثم أدخلها هنا.")
            export_buttons(sheet, f"ورقة جرد - {pick}", "count_sheet", subtitle=f"{auth.client_name()} — {pick} — {day}", formats={"رصيد النظام": FMT_QTY}, key="cnt_sheet")
            with st.expander("📅 الدفعات وتواريخ الصلاحية في هذا المخزن (للمطابقة أثناء الجرد)"):
                lt = reports.lots_df(cid, wid)
                if lt.empty:
                    st.caption("لا توجد دفعات بها رصيد في هذا المخزن.")
                else:
                    st.dataframe(lt.round(3), use_container_width=True, hide_index=True)
                    gone = lt[(lt["أيام للانتهاء"].notna()) & (lt["أيام للانتهاء"] < 0)]
                    if not gone.empty:
                        st.error("⚠️ دفعات منتهية الصلاحية ما زالت بالمخزن: " + "، ".join(gone["الصنف"]) + " — سجّلها من «الهدر والتالف».")
                    export_buttons(lt, f"الدفعات وتواريخ الصلاحية - {pick}", "lots_report", subtitle=f"{auth.client_name()} — {pick}",
                                   formats={"الرصيد": FMT_QTY, "تكلفة الوحدة": FMT_COST}, key="lots_exp")
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
                        _tbl1 = bad
                        st.dataframe(_tbl1, use_container_width=True, hide_index=True)
                        export_buttons(_tbl1, f"فروق الجرد - {pick}", "stocktake_variance", subtitle=f"{auth.client_name()} — {pick} — {day}", formats={"رصيد النظام": FMT_QTY, "الكمية الفعلية": FMT_QTY, "الفرق": FMT_QTY}, key="inv_var")
                except ValueError as e:
                    st.error(str(e))

        st.subheader("سجل عمليات الجرد")
        hist = reports.stocktake_history(cid)
        if hist.empty:
            st.caption("لا توجد عمليات جرد محفوظة.")
        else:
            labels = [f"#{r['id']} — {r['count_date']} — {r['warehouse']} ({r['diff_count']} فرق من {r['lines_count']})" for _, r in hist.iterrows()]
            pick_h = st.selectbox("اختر جردًا", labels, key="hist_pick")
            hid = int(hist.iloc[labels.index(pick_h)]["id"])
            hl = reports.stocktake_lines(hid)
            st.dataframe(hl.round(3), use_container_width=True, hide_index=True)
            export_buttons(hl, "تقرير الجرد", "stocktake_report", subtitle=f"{auth.client_name()} — {pick_h}",
                           formats={"رصيد النظام": FMT_QTY, "الكمية الفعلية": FMT_QTY, "الفرق": FMT_QTY, "قيمة الفرق": FMT_MONEY},
                           totals=["قيمة الفرق"], key="hist_exp", note="قيمة الفرق بتكلفة الوحدة الحالية (تقريبية).")
