from datetime import date, timedelta
import pandas as pd
import streamlit as st

from database.db import query
from utils import theme, auth, services, reports, treasury, inventory

BASE_UNITS = ["كجم", "جرام", "لتر", "مل", "قطعة", "علبة", "كيس", "كرتونة"]
NEW_ITEM = "➕ صنف جديد…"


def _num(x) -> float:
    return reports._f(x)


# ======================================================== عرض الفاتورة
def show_invoice(cid, inv_id):
    hdr, lines = reports.get_invoice(cid, int(inv_id))
    if hdr is None:
        st.warning("الفاتورة غير موجودة.")
        return
    with st.container(border=True):
        st.markdown(f"### فاتورة شراء رقم {hdr['id']} — {hdr['supplier']}")
        a, b, c, d = st.columns(4)
        a.write(f"**التاريخ:** {hdr['invoice_date']}")
        b.write(f"**رقم فاتورة المورد:** {hdr['invoice_no'] or '—'}")
        c.write(f"**المخزن:** {hdr.get('warehouse') or '—'}")
        d.write(f"**أدخلها:** {hdr['created_by'] or '—'}")
        method = hdr.get("pay_method")
        st.write(f"**طريقة الدفع:** {treasury.METHOD_AR.get(method, 'نقدي') if isinstance(method, str) else 'نقدي'}  |  **هاتف المورد:** {hdr['phone'] or '—'}")
        st.dataframe(lines.round(3), use_container_width=True, hide_index=True)
        m = st.columns(4)
        m[0].metric("المجموع قبل الضريبة", f"{hdr['subtotal']:,.2f}")
        m[1].metric(f"ضريبة القيمة المضافة ({_num(hdr.get('vat_rate')):g}%)", f"{hdr['vat_amount']:,.2f}")
        m[2].metric(f"ضريبة الخصم تحت حساب الدفعات المقدمة ({_num(hdr.get('wht_rate')):g}%)", f"- {hdr['wht_amount']:,.2f}")
        m[3].metric("إجمالي الفاتورة", f"{_num(hdr['total']):,.2f}")
        n = st.columns(3)
        n[0].metric("صافي المستحق للمورد", f"{hdr['net_due']:,.2f}")
        n[1].metric("المدفوع", f"{_num(hdr['paid']):,.2f}")
        n[2].metric("المتبقي", f"{hdr['remaining']:,.2f}")
        img = reports.invoice_image_path(hdr.get("image_path"))
        if img:
            st.image(str(img), caption="صورة الفاتورة الورقية", use_container_width=True)
        else:
            st.caption("لا توجد صورة مرفقة بهذه الفاتورة.")


# ======================================================== فاتورة جديدة
def _invoice_form(cid, user):
    last = st.session_state.get("last_invoice")
    if last:
        st.success(f"تم حفظ الفاتورة رقم {last} وتحديث المخزن والأسعار والخزينة/البنك.")
        show_invoice(cid, last)
        if st.button("إخفاء وإدخال فاتورة جديدة"):
            st.session_state.pop("last_invoice")
            st.rerun()
        st.divider()

    sup = query("SELECT id, name FROM suppliers WHERE client_id=? ORDER BY name", (cid,))
    items = query("SELECT id, name, unit, last_price FROM items WHERE client_id=? AND is_semi=0 ORDER BY name", (cid,))
    units = query("""SELECT u.item_id, u.unit_name, u.factor, u.is_purchase_default FROM item_units u JOIN items i ON i.id=u.item_id
                     WHERE i.client_id=? ORDER BY u.is_purchase_default DESC, u.id""", (cid,))
    whs = reports.warehouse_list(cid)
    if sup.empty:
        st.info("أضف موردًا أولًا من تبويب «الموردون».")
        return
    if whs.empty:
        st.error("لا يوجد مخزن لهذا المطعم.")
        return

    n = st.session_state.setdefault("pi_nonce", 0)
    rows = st.session_state.setdefault("pi_rows", [0, 1, 2])
    nxt = st.session_state.setdefault("pi_next", 3)

    def K(*p):
        return "pi_" + "_".join(str(x) for x in p) + f"_n{n}"

    a, b, c, d = st.columns(4)
    sname = a.selectbox("المورد", sup["name"], key=K("sup"))
    inv_no = b.text_input("رقم فاتورة المورد", key=K("no"))
    inv_date = c.date_input("تاريخ الفاتورة", date.today(), key=K("date"))
    main_idx = int(whs.index[whs["kind"] == "main"][0]) if (whs["kind"] == "main").any() else 0
    wname = d.selectbox("مخزن الاستلام", list(whs["name"]), index=main_idx, key=K("wh"))

    st.markdown("**بنود الفاتورة** (الكمية والسعر بوحدة الشراء المختارة، وتُحوَّل تلقائيًا لوحدة الاستهلاك)")
    w = [2.6, 1.3, 1.1, 1.3, 1.3, 0.5]
    for col, t in zip(st.columns(w), ["الصنف", "الوحدة", "الكمية", "سعر الوحدة", "الإجمالي", ""]):
        col.markdown(f"<small><b>{t}</b></small>", unsafe_allow_html=True)

    opts = [NEW_ITEM] + list(items["name"])
    lines, remove = [], None
    for rid in rows:
        cc = st.columns(w)
        sel = cc[0].selectbox("الصنف", opts, index=None, placeholder="اختر صنفًا أو أضف صنفًا جديدًا",
                              key=K("sel", rid), label_visibility="collapsed")
        new_info = None
        if sel and sel != NEW_ITEM:
            r = items.loc[items["name"] == sel].iloc[0]
            u = units[units["item_id"] == r["id"]]
            uopts = list(u["unit_name"]) + [r["unit"]] if not u.empty else [r["unit"]]
            unit = cc[1].selectbox("الوحدة", uopts, key=K("u", rid, sel), label_visibility="collapsed")
            factor = float(u.loc[u["unit_name"] == unit, "factor"].iloc[0]) if unit in set(u["unit_name"]) else 1.0
        else:
            unit, factor, r = None, 1.0, None
            cc[1].markdown("<div style='padding-top:.5rem'>—</div>", unsafe_allow_html=True)
        qty = cc[2].number_input("الكمية", min_value=0.0, step=1.0, format="%.3f", key=K("q", rid), label_visibility="collapsed")
        price = cc[3].number_input("السعر", min_value=0.0, step=1.0, format="%.2f", key=K("p", rid), label_visibility="collapsed")
        cc[4].markdown(f"<div style='padding-top:.5rem'><b>{qty * price:,.2f}</b></div>", unsafe_allow_html=True)
        if cc[5].button("✖", key=K("x", rid), help="حذف السطر"):
            remove = rid
        if sel == NEW_ITEM:
            x, y, z, q2 = st.columns([2.2, 1.3, 1.5, 1.3])
            nn = x.text_input("اسم الصنف الجديد (يُضاف للمخزن تلقائيًا)", key=K("nn", rid))
            nu = y.selectbox("وحدة الاستهلاك", BASE_UNITS, key=K("nu", rid))
            npu = z.text_input("وحدة الشراء (اختياري)", key=K("npu", rid), placeholder="كرتونة، زجاجة...")
            nf = q2.number_input(f"1 وحدة شراء = كم {nu}", min_value=0.0, value=1.0, format="%.3f", key=K("nf", rid))
            lines.append({"new_name": nn, "new_unit": nu, "new_purchase_unit": npu, "new_factor": nf,
                          "unit": npu or None, "qty": qty, "price": price})
        elif r is not None:
            st.caption(f"1 {unit} = {factor:g} {r['unit']} — الكمية بوحدة الاستهلاك: {qty * factor:,.3f} {r['unit']} — "
                       f"آخر سعر للوحدة الأساسية: {_num(r['last_price']):,.4f}")
            lines.append({"item_id": int(r["id"]), "unit": unit, "qty": qty, "price": price})
    if remove is not None:
        rows.remove(remove)
        st.rerun()
    if st.button("➕ إضافة سطر"):
        rows.append(nxt)
        st.session_state["pi_next"] = nxt + 1
        st.rerun()

    valid = [l for l in lines if l["qty"] > 0]

    st.markdown("**الضرائب**")
    t1, t2 = st.columns(2)
    use_vat = t1.checkbox("إضافة ضريبة القيمة المضافة (المبيعات)", key=K("vat"))
    vat_rate = t1.number_input("نسبة ضريبة القيمة المضافة %", 0.0, 100.0, 14.0, step=0.5, disabled=not use_vat, key=K("vatr"))
    use_wht = t2.checkbox("خصم ضريبة الخصم تحت حساب الدفعات المقدمة", key=K("wht"))
    wht_rate = t2.number_input("نسبة ضريبة الخصم %", 0.0, 100.0, 1.0, step=0.5, disabled=not use_wht, key=K("whtr"))
    t = services.calc_invoice([(l["qty"], l["price"]) for l in valid], vat_rate if use_vat else 0, wht_rate if use_wht else 0)
    m = st.columns(5)
    m[0].metric("المجموع قبل الضريبة", f"{t['subtotal']:,.2f}")
    m[1].metric("ضريبة القيمة المضافة", f"{t['vat']:,.2f}")
    m[2].metric("ضريبة الخصم (تُخصم)", f"- {t['wht']:,.2f}")
    m[3].metric("إجمالي الفاتورة", f"{t['total']:,.2f}")
    m[4].metric("صافي المستحق للمورد", f"{t['net_due']:,.2f}")
    st.caption("المتوسط المرجح للتكلفة يُحسب لكل وحدة استهلاك بالسعر قبل الضريبة (ضريبة المشتريات قابلة للخصم).")

    st.markdown("**السداد والمرفقات**")
    p1, p2 = st.columns(2)
    method = p1.radio("طريقة الدفع", ["cash", "bank", "none"], format_func=treasury.METHOD_AR.get, horizontal=True, key=K("method"))
    if method == "none":
        paid = 0.0
        p2.caption("الفاتورة ستُسجَّل كمستحق على المورد بدون حركة نقدية.")
    else:
        full = p1.checkbox("مدفوعة بالكامل الآن", key=K("full"))
        paid = t["net_due"] if full else p2.number_input("المبلغ المدفوع الآن", min_value=0.0, step=100.0, key=K("paid"))
        bal = treasury.balance(cid, method)["balance"]
        p2.caption(f"رصيد {treasury.ACCOUNT_AR[method]} الحالي: {bal:,.2f}")
    up = st.file_uploader("صورة الفاتورة الورقية (اختياري)", type=["jpg", "jpeg", "png", "webp"], key=K("img"))
    if up:
        st.image(up, width=220)

    if st.button("💾 حفظ الفاتورة", type="primary", key=K("save")):
        try:
            sid = int(sup.loc[sup["name"] == sname, "id"].iloc[0])
            wid = int(whs.loc[whs["name"] == wname, "id"].iloc[0])
            invoice, _ = services.add_purchase(
                cid, sid, inv_no, inv_date, valid, paid, user, vat_rate=vat_rate if use_vat else 0,
                wht_rate=wht_rate if use_wht else 0, image=(up.getvalue(), up.name) if up else None,
                pay_method=method, warehouse_id=wid)
            st.session_state["last_invoice"] = invoice
            for k in ("pi_rows", "pi_next"):
                st.session_state.pop(k, None)
            st.session_state["pi_nonce"] = n + 1
            st.rerun()
        except ValueError as e:
            st.error(str(e))


# ======================================================== الصفحة
def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("المشتريات والموردون", auth.client_name())
    t1, t2, t3, t4, t5, t6 = st.tabs(["فاتورة شراء جديدة", "الفواتير", "كشف حساب مورد", "الموردون", "الأصناف والوحدات", "المدفوعات"])

    with t1:
        _invoice_form(cid, user)

    with t2:
        d1, d2 = st.columns(2)
        f = d1.date_input("من", date.today() - timedelta(days=90), key="inv_from")
        t = d2.date_input("إلى", date.today(), key="inv_to")
        inv = query("""SELECT p.id AS "رقم", p.invoice_date AS "التاريخ", s.name AS "المورد", p.invoice_no AS "رقم المورد",
                       COALESCE(p.subtotal, p.total) AS "قبل الضريبة", p.total AS "الإجمالي", COALESCE(p.wht_amount,0) AS "ضريبة الخصم",
                       p.total - COALESCE(p.wht_amount,0) AS "صافي المستحق", p.paid AS "المدفوع",
                       p.total - COALESCE(p.wht_amount,0) - p.paid AS "المتبقي", COALESCE(w.name,'') AS "المخزن",
                       CASE WHEN p.image_path IS NULL OR p.image_path='' THEN '' ELSE '📎' END AS "صورة", p.created_by AS "أدخلها"
                       FROM purchase_invoices p JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN warehouses w ON w.id=p.warehouse_id
                       WHERE p.client_id=? AND p.invoice_date BETWEEN ? AND ? ORDER BY p.invoice_date DESC, p.id DESC""",
                    (cid, str(f), str(t)))
        st.dataframe(inv, use_container_width=True, hide_index=True)
        if not inv.empty:
            pick = st.selectbox("عرض فاتورة (الرقم)", inv["رقم"], key="inv_pick")
            show_invoice(cid, pick)

    with t3:
        sup = query("SELECT id, name FROM suppliers WHERE client_id=? ORDER BY name", (cid,))
        if sup.empty:
            st.info("لا يوجد موردون.")
        else:
            a, b, c = st.columns(3)
            sname = a.selectbox("المورد", sup["name"], key="st_sup")
            f = b.date_input("من", date(date.today().year, 1, 1), key="st_f")
            t = c.date_input("إلى", date.today(), key="st_t")
            info, df, opening, closing = reports.supplier_statement(cid, int(sup.loc[sup["name"] == sname, "id"].iloc[0]), f, t)
            st.markdown(f"#### كشف حساب: {info['name']}" + (f" — هاتف: {info['phone']}" if info["phone"] else ""))
            st.caption(f"عن الفترة من {f} إلى {t} — {auth.client_name()}")
            m = st.columns(4)
            m[0].metric("رصيد أول الفترة", f"{opening:,.2f}")
            m[1].metric("مشتريات الفترة", f"{df['مشتريات (مستحق للمورد)'].sum() if not df.empty else 0:,.2f}")
            m[2].metric("مدفوعات الفترة", f"{df['مدفوعات'].sum() if not df.empty else 0:,.2f}")
            m[3].metric("الرصيد المستحق للمورد", f"{closing:,.2f}")
            if df.empty:
                st.info("لا توجد حركات في هذه الفترة.")
            else:
                st.dataframe(df.round(2), use_container_width=True, hide_index=True)
                st.download_button("تنزيل الكشف CSV", df.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"statement_{info['name']}.csv", mime="text/csv")

    with t4:
        with st.form("sup_form", clear_on_submit=True):
            a, b, c = st.columns(3)
            n, ph, ob = a.text_input("اسم المورد"), b.text_input("الهاتف"), c.number_input("رصيد أول المدة", step=100.0)
            if st.form_submit_button("إضافة مورد"):
                try:
                    services.add_supplier(cid, n, ph, ob, user)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        df = query("SELECT id, name, phone, opening_balance FROM suppliers WHERE client_id=? ORDER BY name", (cid,))
        if not df.empty:
            ed = st.data_editor(df, hide_index=True, use_container_width=True, key="ed_sup",
                                column_config={"id": None, "name": "المورد", "phone": "الهاتف", "opening_balance": "رصيد أول المدة"})
            if st.button("حفظ تعديلات الموردين"):
                try:
                    k = services.apply_edits("suppliers", df, ed, user, cid)
                    st.success(f"تم حفظ {k} تعديل.") if k else st.info("لا توجد تعديلات.")
                except ValueError as e:
                    st.error(str(e))
            st.subheader("أرصدة الموردين")
            st.dataframe(services.supplier_balances(cid).drop(columns="id"), use_container_width=True, hide_index=True)

    with t5:
        st.caption("وحدة الاستهلاك هي الوحدة التي تُستخدم بها الخامة في الوصفات وتُحسب بها التكلفة (جرام، مل، قطعة). "
                   "وحدة الشراء (كرتونة، زجاجة) تُحوَّل لها تلقائيًا بمعامل التحويل.")
        with st.form("item_form", clear_on_submit=True):
            a, b, c, d = st.columns(4)
            n = a.text_input("اسم الخامة")
            u = b.selectbox("وحدة الاستهلاك", BASE_UNITS)
            pu = c.text_input("وحدة الشراء (اختياري)", placeholder="كرتونة")
            fac = d.number_input("1 وحدة شراء = كم وحدة استهلاك", min_value=0.0, value=1.0, format="%.3f")
            m = st.number_input("حد إعادة الطلب (بوحدة الاستهلاك)", min_value=0.0, step=1.0)
            if st.form_submit_button("إضافة خامة"):
                try:
                    services.add_item(cid, n, u, m, user, purchase_unit=pu, factor=fac)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        df = query("SELECT id, name, unit, min_qty FROM items WHERE client_id=? AND is_semi=0 ORDER BY name", (cid,))
        if not df.empty:
            ed = st.data_editor(df, hide_index=True, use_container_width=True, key="ed_items",
                                column_config={"id": None, "name": "الخامة", "unit": "وحدة الاستهلاك", "min_qty": "حد إعادة الطلب"})
            if st.button("حفظ تعديلات الخامات"):
                try:
                    k = services.apply_edits("items", df, ed, user, cid)
                    st.success(f"تم حفظ {k} تعديل.") if k else st.info("لا توجد تعديلات.")
                except ValueError as e:
                    st.error(str(e))
            st.subheader("وحدات التحويل لكل خامة")
            nm = st.selectbox("الخامة", df["name"], key="units_item")
            row = df[df["name"] == nm].iloc[0]
            cur = query("SELECT unit_name AS \"الوحدة\", factor AS \"المعامل\", is_purchase_default AS \"افتراضية للشراء\" FROM item_units WHERE item_id=? ORDER BY id",
                        (int(row["id"]),))
            cur["افتراضية للشراء"] = cur["افتراضية للشراء"].astype(bool)
            st.caption(f"كل صف: 1 وحدة = المعامل × {row['unit']}. مثال: كرتونة = 90، كيس = 15.")
            ue = st.data_editor(cur, num_rows="dynamic", use_container_width=True, key=f"ue_{row['id']}",
                                column_config={"المعامل": st.column_config.NumberColumn(min_value=0.0, format="%.4f"),
                                               "افتراضية للشراء": st.column_config.CheckboxColumn()})
            if st.button("حفظ الوحدات"):
                try:
                    clean = [(r["الوحدة"], r["المعامل"], bool(r["افتراضية للشراء"])) for _, r in ue.iterrows()
                             if isinstance(r["الوحدة"], str) and r["الوحدة"].strip()]
                    inventory.set_item_units(cid, int(row["id"]), clean, user)
                    st.success("تم حفظ الوحدات.")
                except ValueError as e:
                    st.error(str(e))

    with t6:
        sup = query("SELECT id, name FROM suppliers WHERE client_id=? ORDER BY name", (cid,))
        if sup.empty:
            st.info("أضف موردًا أولًا.")
        else:
            bal = treasury.balances(cid)
            st.caption(f"رصيد النقدية: {bal['cash']:,.2f} — رصيد البنك: {bal['bank']:,.2f}")
            with st.form("pay_form", clear_on_submit=True):
                a, b, c, d = st.columns(4)
                sname = a.selectbox("المورد", sup["name"])
                day = b.date_input("التاريخ", date.today())
                amt = c.number_input("المبلغ", min_value=0.0, step=100.0)
                note = d.text_input("ملاحظة")
                method = st.radio("طريقة الدفع", ["cash", "bank", "none"], format_func=treasury.METHOD_AR.get, horizontal=True)
                if st.form_submit_button("تسجيل دفعة"):
                    try:
                        services.add_payment(cid, int(sup.loc[sup["name"] == sname, "id"].iloc[0]), day, amt, note, user, method)
                        st.success("تم تسجيل الدفعة.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
            st.dataframe(query("""SELECT p.pay_date AS "التاريخ", s.name AS "المورد", p.amount AS "المبلغ", p.note AS "ملاحظة",
                                  COALESCE(p.pay_method,'cash') AS "الدفع", p.created_by AS "سجّلها" FROM supplier_payments p
                                  JOIN suppliers s ON s.id=p.supplier_id WHERE p.client_id=? ORDER BY p.pay_date DESC, p.id DESC""", (cid,)),
                         use_container_width=True, hide_index=True)
