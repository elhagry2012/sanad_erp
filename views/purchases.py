from datetime import date, timedelta
import pandas as pd
import streamlit as st

from database.db import query
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
from utils import theme, auth, services, reports, treasury, inventory, documents, controls

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
        if hdr.get("status") == "voided":
            st.error(f"🚫 فاتورة ملغاة بواسطة {hdr.get('voided_by')} في {hdr.get('voided_at')} — السبب: {hdr.get('void_reason') or '—'}. أثرها المخزني والمالي معكوس.")
        elif isinstance(hdr.get("replaces_id"), (int, float)) and hdr.get("replaces_id") == hdr.get("replaces_id"):
            st.info(f"فاتورة بديلة للفاتورة رقم {int(hdr['replaces_id'])} (بعد تعديل).")
        a, b, c, d = st.columns(4)
        a.write(f"**التاريخ:** {hdr['invoice_date']}")
        b.write(f"**رقم فاتورة المورد:** {hdr['invoice_no'] or '—'}")
        c.write(f"**المخزن:** {hdr.get('warehouse') or '—'}")
        d.write(f"**أدخلها:** {hdr['created_by'] or '—'}")
        method = hdr.get("pay_method")
        st.write(f"**طريقة الدفع:** {treasury.METHOD_AR.get(method, 'نقدي') if isinstance(method, str) else 'نقدي'}  |  **هاتف المورد:** {hdr['phone'] or '—'}")
        _tbl20 = lines
        st.dataframe(_tbl20.round(3), use_container_width=True, hide_index=True)
        export_buttons(_tbl20, f"فاتورة شراء رقم {hdr['id']}", "purchase_invoice", subtitle=f"{hdr['supplier']} — {hdr['invoice_date']} — {auth.client_name()}", formats={"الكمية": FMT_QTY, "سعر الوحدة": FMT_MONEY, "الإجمالي": FMT_MONEY, "ما يعادل": FMT_QTY}, totals=["الإجمالي"], key=f"invx_{hdr['id']}", note=f"قبل الضريبة {hdr['subtotal']:,.2f} | ضريبة القيمة المضافة {hdr['vat_amount']:,.2f} | ضريبة الخصم {hdr['wht_amount']:,.2f} | إجمالي الفاتورة {_num(hdr['total']):,.2f} | صافي المستحق للمورد {hdr['net_due']:,.2f} | المدفوع {_num(hdr['paid']):,.2f} | المتبقي {hdr['remaining']:,.2f}")
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
def _prefill_from_invoice(cid, inv_id):
    """يحمّل فاتورة قائمة في نموذج الإدخال لتعديلها (التعديل = إلغاء الأصل وإنشاء بديل عند الحفظ)."""
    hdr, _ = reports.get_invoice(cid, inv_id)
    raw = query("""SELECT i.name, COALESCE(l.unit_name, i.unit) AS unit, l.qty, l.unit_price, l.expiry_date FROM purchase_lines l
                   JOIN items i ON i.id=l.item_id WHERE l.invoice_id=? ORDER BY l.id""", (inv_id,))
    ss = st.session_state
    n = ss.get("pi_nonce", 0) + 1
    for k in [k for k in list(ss.keys()) if k.startswith("pi_")]:
        ss.pop(k)
    K = lambda *p: "pi_" + "_".join(str(x) for x in p) + f"_n{n}"          # noqa: E731
    ss["pi_nonce"], ss["pi_rows"], ss["pi_next"], ss["pi_replace"] = n, list(range(len(raw))), len(raw), int(inv_id)
    ss[K("sup")], ss[K("no")] = hdr["supplier"], hdr["invoice_no"] or ""
    ss[K("date")] = date.fromisoformat(hdr["invoice_date"])
    if hdr.get("warehouse"):
        ss[K("wh")] = hdr["warehouse"]
    for i, r in raw.iterrows():
        ss[K("sel", i)], ss[K("u", i, r["name"])] = r["name"], r["unit"]
        ss[K("q", i)], ss[K("p", i)] = float(r["qty"]), float(r["unit_price"])
        if isinstance(r["expiry_date"], str):
            ss[K("exp", i)] = date.fromisoformat(r["expiry_date"])
    vr, wr = _num(hdr.get("vat_rate")), _num(hdr.get("wht_rate"))
    ss[K("vat")], ss[K("wht")] = vr > 0, wr > 0
    ss[K("vatr")], ss[K("whtr")] = (vr if vr > 0 else 14.0), (wr if wr > 0 else 1.0)
    method = hdr.get("pay_method") if isinstance(hdr.get("pay_method"), str) else "cash"
    ss[K("method")] = method
    if method != "none" and _num(hdr.get("paid")) > 0:
        ss[K("paid")] = float(_num(hdr["paid"]))


def _invoice_tools(cid, user, inv_id, voided):
    """المدير العام: إلغاء/تعديل. غيره: طلب تعديل يصل المدير العام فورًا."""
    if voided:
        return
    if auth.is_admin():
        with st.expander("🛠 أدوات المدير العام: إلغاء أو تعديل هذه الفاتورة"):
            reason = st.text_input("السبب (إلزامي للإلغاء)", key=f"vr_{inv_id}")
            neg = st.checkbox("السماح بالرصيد السالب إن كان جزء من الكمية قد استُهلك", key=f"vn_{inv_id}")
            c1, c2 = st.columns(2)
            if c1.button("🗑 إلغاء الفاتورة وعكس أثرها بالكامل", key=f"vb_{inv_id}"):
                try:
                    documents.void_purchase(cid, inv_id, reason, user, neg)
                    st.session_state.pop("last_invoice", None)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if c2.button("✏️ تعديل الفاتورة (إلغاء الأصل وإنشاء بديل)", key=f"eb_{inv_id}"):
                _prefill_from_invoice(cid, inv_id)
                st.session_state.pop("last_invoice", None)
                st.rerun()
            st.caption("الإلغاء يعكس المخزون والدفعات والمتوسط المرجح وسجل الأسعار والخزينة وحساب المورد، ويُبقي الفاتورة ظاهرة كملغاة مع حركة عكس بسجل الصنف.")
    else:
        with st.expander("✉️ طلب تعديل هذه الفاتورة (يصل المدير العام فورًا)"):
            reason = st.text_area("لماذا تحتاج التعديل؟", key=f"rq_{inv_id}")
            proposed = st.text_input("التعديل المطلوب (اختياري)", key=f"rqp_{inv_id}")
            if st.button("إرسال الطلب", key=f"rqb_{inv_id}"):
                try:
                    controls.create_edit_request(cid, "purchase_invoice", inv_id, reason, proposed, user)
                    st.success("تم إرسال الطلب وإشعار المدير العام. تابع حالته من «الإشعارات وطلبات التعديل».")
                except ValueError as e:
                    st.error(str(e))


def _invoice_form(cid, user):
    last = st.session_state.get("last_invoice")
    if last:
        st.success(f"تم حفظ الفاتورة رقم {last} وتحديث المخزن والأسعار والخزينة/البنك.")
        show_invoice(cid, last)
        _invoice_tools(cid, user, int(last), voided=False)
        if st.button("إخفاء وإدخال فاتورة جديدة"):
            st.session_state.pop("last_invoice")
            st.rerun()
        st.divider()

    rep_id = st.session_state.get("pi_replace")
    if rep_id:
        st.warning(f"⚠️ وضع تعديل الفاتورة رقم {rep_id}: عند الحفظ تُلغى الفاتورة الأصلية ويُعكس أثرها وتُنشأ فاتورة بديلة في معاملة واحدة (فإن فشل الحفظ لا يتغير شيء).")
        if st.button("إلغاء وضع التعديل والعودة لإدخال جديد"):
            for k in [k for k in list(st.session_state.keys()) if k.startswith("pi_")]:
                st.session_state.pop(k)
            st.session_state["pi_nonce"] = int(st.session_state.get("pi_nonce", 0)) + 1
            st.rerun()

    sup = query("SELECT id, name FROM suppliers WHERE client_id=? ORDER BY name", (cid,))
    items = query("SELECT id, name, unit, last_price, track_expiry, shelf_life_days FROM items WHERE client_id=? AND is_semi=0 ORDER BY name", (cid,))
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

    def ws(key, **kw):
        """قيمة افتراضية فقط إن لم تُضبط عبر session_state (تفاديًا لتحذير تعارض القيم عند تحميل فاتورة للتعديل)."""
        return {} if key in st.session_state else kw

    a, b, c, d = st.columns(4)
    sname = a.selectbox("المورد", sup["name"], key=K("sup"))
    inv_no = b.text_input("رقم فاتورة المورد", key=K("no"))
    inv_date = c.date_input("تاريخ الفاتورة", key=K("date"), **ws(K("date"), value=date.today()))
    main_idx = int(whs.index[whs["kind"] == "main"][0]) if (whs["kind"] == "main").any() else 0
    wname = d.selectbox("مخزن الاستلام", list(whs["name"]), key=K("wh"), **ws(K("wh"), index=main_idx))

    st.markdown("**بنود الفاتورة** (الكمية والسعر بوحدة الشراء المختارة، وتُحوَّل تلقائيًا لوحدة الاستهلاك)")
    w = [2.3, 1.2, 1.0, 1.2, 1.2, 1.4, 0.5]
    for col, t in zip(st.columns(w), ["الصنف", "الوحدة", "الكمية", "سعر الوحدة", "الإجمالي", "تاريخ الصلاحية", ""]):
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
        expiry = None
        if r is not None and int(r["track_expiry"] or 0):
            default_exp = date.today() + timedelta(days=int(r["shelf_life_days"] or 30))
            expiry = cc[5].date_input("الصلاحية", key=K("exp", rid), label_visibility="collapsed", **ws(K("exp", rid), value=default_exp))
        else:
            cc[5].markdown("<div style='padding-top:.5rem;color:#888'>—</div>", unsafe_allow_html=True)
        if cc[6].button("✖", key=K("x", rid), help="حذف السطر"):
            remove = rid
        if sel == NEW_ITEM:
            x, y, z, q2 = st.columns([2.2, 1.3, 1.5, 1.3])
            nn = x.text_input("اسم الصنف الجديد (يُضاف للمخزن تلقائيًا)", key=K("nn", rid))
            nu = y.selectbox("وحدة الاستهلاك", BASE_UNITS, key=K("nu", rid))
            npu = z.text_input("وحدة الشراء (اختياري)", key=K("npu", rid), placeholder="كرتونة، زجاجة...")
            nf = q2.number_input(f"1 وحدة شراء = كم {nu}", min_value=0.0, format="%.3f", key=K("nf", rid), **ws(K("nf", rid), value=1.0))
            e1, e2, e3 = st.columns([1.6, 1.6, 2.8])
            nte = e1.checkbox("له تاريخ صلاحية", key=K("nte", rid))
            nsl = e2.number_input("مدة الصلاحية الافتراضية (يوم)", min_value=0, step=1, key=K("nsl", rid), disabled=not nte, **ws(K("nsl", rid), value=30))
            nexp = e3.date_input("تاريخ صلاحية هذه الدفعة", key=K("nexp", rid), disabled=not nte,
                                 **ws(K("nexp", rid), value=date.today() + timedelta(days=30))) if nte else None
            lines.append({"new_name": nn, "new_unit": nu, "new_purchase_unit": npu, "new_factor": nf, "new_track_expiry": nte,
                          "new_shelf_life": int(nsl) if nte else None, "expiry": str(nexp) if nexp else None,
                          "unit": npu or None, "qty": qty, "price": price})
        elif r is not None:
            st.caption(f"1 {unit} = {factor:g} {r['unit']} — الكمية بوحدة الاستهلاك: {qty * factor:,.3f} {r['unit']} — "
                       f"آخر سعر للوحدة الأساسية: {_num(r['last_price']):,.4f}")
            lines.append({"item_id": int(r["id"]), "unit": unit, "qty": qty, "price": price, "expiry": str(expiry) if expiry else None})
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
    vat_rate = t1.number_input("نسبة ضريبة القيمة المضافة %", 0.0, 100.0, step=0.5, disabled=not use_vat, key=K("vatr"), **ws(K("vatr"), value=14.0))
    use_wht = t2.checkbox("خصم ضريبة الخصم تحت حساب الدفعات المقدمة", key=K("wht"))
    wht_rate = t2.number_input("نسبة ضريبة الخصم %", 0.0, 100.0, step=0.5, disabled=not use_wht, key=K("whtr"), **ws(K("whtr"), value=1.0))
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
            kw = dict(vat_rate=vat_rate if use_vat else 0, wht_rate=wht_rate if use_wht else 0,
                      image=(up.getvalue(), up.name) if up else None, pay_method=method, warehouse_id=wid)
            if rep_id:
                invoice, _ = documents.replace_purchase(cid, int(rep_id), sid, inv_no, inv_date, valid, paid, user, **kw)
            else:
                invoice, _ = services.add_purchase(cid, sid, inv_no, inv_date, valid, paid, user, **kw)
            st.session_state["last_invoice"] = invoice
            for k in [k for k in list(st.session_state.keys()) if k.startswith("pi_") and k != "pi_nonce"]:
                st.session_state.pop(k, None)
            st.session_state["pi_nonce"] = n + 1
            st.rerun()
        except ValueError as e:
            st.error(str(e))


# ======================================================== الصفحة
def _returns_tab(cid, user):
    sup = query("SELECT id, name FROM suppliers WHERE client_id=? ORDER BY name", (cid,))
    whs = reports.warehouse_list(cid)
    if sup.empty:
        st.info("أضف موردًا أولًا.")
        return
    last = st.session_state.get("last_return")
    if last:
        st.success(f"تم حفظ المرتجع رقم {last}: خرجت الكميات من المخزن، وصُحِّح متوسط التكلفة، وخُفِّض حساب المورد.")
        st.dataframe(reports.return_lines(int(last)).round(3), use_container_width=True, hide_index=True)
        if st.button("إخفاء", key="hide_ret"):
            st.session_state.pop("last_return")
            st.rerun()
        st.divider()
    st.markdown("**مرتجع جديد للمورد** (للمشتريات التالفة أو المنتهية الصلاحية أو المخالفة)")
    a, b, c = st.columns(3)
    sname = a.selectbox("المورد", sup["name"], key="ret_sup")
    sid = int(sup.loc[sup["name"] == sname, "id"].iloc[0])
    invs = query("""SELECT id, invoice_no, invoice_date, total FROM purchase_invoices WHERE client_id=? AND supplier_id=?
                    AND COALESCE(status,'active')='active' ORDER BY id DESC""", (cid, sid))
    opts = {"بدون ربط بفاتورة": None}
    for _, r in invs.iterrows():
        opts[f"#{r['id']} — {r['invoice_date']} — {r['invoice_no'] or ''} ({r['total']:,.0f})"] = int(r["id"])
    pick = b.selectbox("الفاتورة الأصلية (تحدّد الأسعار والحد الأقصى للإرجاع)", list(opts), key="ret_inv")
    inv_id = opts[pick]
    day = c.date_input("تاريخ المرتجع", date.today(), key="ret_date")
    d, e, f = st.columns(3)
    reason = d.selectbox("السبب", documents.RETURN_REASONS, key="ret_reason")
    main_idx = int(whs.index[whs["kind"] == "main"][0]) if (whs["kind"] == "main").any() else 0
    wname = e.selectbox("يُخصم من مخزن", list(whs["name"]), index=main_idx, key="ret_wh")
    method = f.radio("الاسترداد", ["none", "cash", "bank"], horizontal=True, key="ret_method",
                     format_func={"none": "خصم من حساب المورد", "cash": "استرداد نقدي", "bank": "استرداد بنكي"}.get)
    if inv_id:
        names = list(query("SELECT DISTINCT i.name FROM purchase_lines l JOIN items i ON i.id=l.item_id WHERE l.invoice_id=? ORDER BY i.name", (inv_id,))["name"])
    else:
        names = list(query("SELECT name FROM items WHERE client_id=? AND is_semi=0 ORDER BY name", (cid,))["name"])
    unit_opts = sorted(set(query("SELECT unit FROM items WHERE client_id=?", (cid,))["unit"]) |
                       set(query("SELECT u.unit_name FROM item_units u JOIN items i ON i.id=u.item_id WHERE i.client_id=?", (cid,))["unit_name"]))
    grid = pd.DataFrame({"الصنف": [None] * 3, "الوحدة": [None] * 3, "الكمية": [0.0] * 3, "السعر (اختياري مع الفاتورة)": [None] * 3})
    ed = st.data_editor(grid, num_rows="dynamic", use_container_width=True, key=f"ret_grid_{inv_id}", column_config={
        "الصنف": st.column_config.SelectboxColumn(options=names, required=True),
        "الوحدة": st.column_config.SelectboxColumn(options=unit_opts, help="فارغة = وحدة الاستهلاك"),
        "الكمية": st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
        "السعر (اختياري مع الفاتورة)": st.column_config.NumberColumn(min_value=0.0, format="%.2f")})
    refund = 0.0
    if method != "none":
        refund = st.number_input("المبلغ المسترد من المورد", min_value=0.0, step=50.0, key="ret_refund")
    notes = st.text_input("ملاحظات", key="ret_notes")
    st.caption("يُخصم الصنف من الدفعة الأقرب انتهاءً (FEFO)، وتُعكس ضريبة القيمة المضافة وضريبة الخصم بنسبة الفاتورة الأصلية.")
    if st.button("💾 حفظ المرتجع", type="primary", key="ret_save"):
        ids = dict(query("SELECT name, id FROM items WHERE client_id=?", (cid,)).values)
        lines = []
        for _, r in ed.iterrows():
            if r["الصنف"] in ids and r["الكمية"] and r["الكمية"] > 0:
                pr = r["السعر (اختياري مع الفاتورة)"]
                lines.append({"item_id": int(ids[r["الصنف"]]), "unit": r["الوحدة"] if isinstance(r["الوحدة"], str) else None,
                              "qty": float(r["الكمية"]), "price": None if pd.isna(pr) else float(pr)})
        try:
            rid, tot = documents.create_return(cid, sid, day, lines, reason, user, invoice_id=inv_id,
                                               warehouse_id=int(whs.loc[whs["name"] == wname, "id"].iloc[0]),
                                               refund_method=method, refund_amount=refund, notes=notes)
            st.session_state["last_return"] = rid
            st.rerun()
        except ValueError as ex:
            st.error(str(ex))

    st.divider()
    st.markdown("**سجل المرتجعات**")
    g1, g2 = st.columns(2)
    f1 = g1.date_input("من", date.today() - timedelta(days=90), key="rl_f")
    t1 = g2.date_input("إلى", date.today(), key="rl_t")
    rl = reports.returns_list(cid, f1, t1)
    if rl.empty:
        st.info("لا توجد مرتجعات في هذه الفترة.")
        return
    st.dataframe(rl, use_container_width=True, hide_index=True)
    export_buttons(rl, "مرتجعات المشتريات", "purchase_returns", subtitle=f"{auth.client_name()} — من {f1} إلى {t1}",
                   totals=["قبل الضريبة", "الضريبة", "الإجمالي", "صافي الإشعار الدائن", "المسترد"], key="ret_exp")
    pk = st.selectbox("عرض مرتجع", rl["رقم"], key="ret_pick")
    st.dataframe(reports.return_lines(int(pk)).round(3), use_container_width=True, hide_index=True)
    voided = rl.loc[rl["رقم"] == pk, "الحالة"].iloc[0] == "ملغي"
    if not voided:
        if auth.is_admin():
            rs = st.text_input("سبب إلغاء المرتجع", key=f"rvr_{pk}")
            if st.button("🗑 إلغاء المرتجع وعكس أثره", key=f"rvb_{pk}"):
                try:
                    documents.void_return(cid, int(pk), rs or "إلغاء", user)
                    st.rerun()
                except ValueError as ex:
                    st.error(str(ex))
        else:
            with st.expander("✉️ طلب تعديل/إلغاء هذا المرتجع"):
                rs = st.text_area("السبب", key=f"rqr_{pk}")
                if st.button("إرسال الطلب", key=f"rqrb_{pk}"):
                    try:
                        controls.create_edit_request(cid, "purchase_return", int(pk), rs, "", user)
                        st.success("تم إرسال الطلب للمدير العام.")
                    except ValueError as ex:
                        st.error(str(ex))


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("المشتريات والموردون", auth.client_name())
    t1, t2, t3, t4, t5, t6, t7 = st.tabs(["فاتورة شراء جديدة", "الفواتير", "كشف حساب مورد", "الموردون", "الأصناف والوحدات", "المدفوعات", "↩️ مرتجعات المشتريات"])

    with t1:
        _invoice_form(cid, user)

    with t2:
        d1, d2 = st.columns(2)
        f = d1.date_input("من", date.today() - timedelta(days=90), key="inv_from")
        t = d2.date_input("إلى", date.today(), key="inv_to")
        show_void = st.checkbox("إظهار الفواتير الملغاة", key="inv_show_void")
        inv = query("""SELECT p.id AS "رقم", p.invoice_date AS "التاريخ", s.name AS "المورد", p.invoice_no AS "رقم المورد",
                       COALESCE(p.subtotal, p.total) AS "قبل الضريبة", p.total AS "الإجمالي", COALESCE(p.wht_amount,0) AS "ضريبة الخصم",
                       p.total - COALESCE(p.wht_amount,0) AS "صافي المستحق", p.paid AS "المدفوع",
                       p.total - COALESCE(p.wht_amount,0) - p.paid AS "المتبقي", COALESCE(w.name,'') AS "المخزن",
                       CASE WHEN p.image_path IS NULL OR p.image_path='' THEN '' ELSE '📎' END AS "صورة",
                       CASE WHEN COALESCE(p.status,'active')='voided' THEN 'ملغاة' ELSE '' END AS "الحالة", p.created_by AS "أدخلها"
                       FROM purchase_invoices p JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN warehouses w ON w.id=p.warehouse_id
                       WHERE p.client_id=? AND p.invoice_date BETWEEN ? AND ? AND (? OR COALESCE(p.status,'active')='active')
                       ORDER BY p.invoice_date DESC, p.id DESC""", (cid, str(f), str(t), 1 if show_void else 0))
        _tbl19 = inv
        st.dataframe(_tbl19, use_container_width=True, hide_index=True)
        export_buttons(_tbl19, "فواتير المشتريات", "purchase_invoices", subtitle=f"{auth.client_name()} — من {f} إلى {t}", totals=["قبل الضريبة", "الإجمالي", "ضريبة الخصم", "صافي المستحق", "المدفوع", "المتبقي"], key="pur_list")
        if not inv.empty:
            pick = st.selectbox("عرض فاتورة (الرقم)", inv["رقم"], key="inv_pick")
            show_invoice(cid, pick)
            _invoice_tools(cid, user, int(pick), voided=(inv.loc[inv["رقم"] == pick, "الحالة"].iloc[0] == "ملغاة"))

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
                _tbl18 = df
                st.dataframe(_tbl18.round(2), use_container_width=True, hide_index=True)
                export_buttons(_tbl18, f"كشف حساب مورد: {info['name']}", "supplier_statement", subtitle=f"{auth.client_name()} — من {f} إلى {t} — رصيد أول الفترة {opening:,.2f} — الرصيد المستحق {closing:,.2f}", totals=["مشتريات (مستحق للمورد)", "مدفوعات"], key="pur_stmt")

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
            _tbl17 = services.supplier_balances(cid).drop(columns="id")
            st.dataframe(_tbl17, use_container_width=True, hide_index=True)
            export_buttons(_tbl17, "أرصدة الموردين", "supplier_balances", subtitle=auth.client_name(), totals=["الرصيد المستحق"], key="pur_bal")

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
            t_a, t_b = st.columns(2)
            track = t_a.checkbox("يتتبع تاريخ الصلاحية (يُطلب التاريخ عند التوريد وتُتتبع الدفعات)")
            shelf = t_b.number_input("مدة الصلاحية الافتراضية (يوم)", min_value=0, value=30, step=1)
            if st.form_submit_button("إضافة خامة"):
                try:
                    services.add_item(cid, n, u, m, user, purchase_unit=pu, factor=fac, track_expiry=track, shelf_life_days=int(shelf) if track else None)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        df = query("SELECT id, name, unit, min_qty, track_expiry, shelf_life_days FROM items WHERE client_id=? AND is_semi=0 ORDER BY name", (cid,))
        df["track_expiry"] = df["track_expiry"].fillna(0).astype(bool)
        if not df.empty:
            ed = st.data_editor(df, hide_index=True, use_container_width=True, key="ed_items",
                                column_config={"id": None, "name": "الخامة", "unit": "وحدة الاستهلاك", "min_qty": "حد إعادة الطلب",
                                               "track_expiry": st.column_config.CheckboxColumn("يتتبع الصلاحية"),
                                               "shelf_life_days": st.column_config.NumberColumn("مدة الصلاحية (يوم)", min_value=0, step=1)})
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
            _tbl16 = query("""SELECT p.pay_date AS "التاريخ", s.name AS "المورد", p.amount AS "المبلغ", p.note AS "ملاحظة",
                                  COALESCE(p.pay_method,'cash') AS "الدفع", p.created_by AS "سجّلها" FROM supplier_payments p
                                  JOIN suppliers s ON s.id=p.supplier_id WHERE p.client_id=? ORDER BY p.pay_date DESC, p.id DESC""", (cid,))
            st.dataframe(_tbl16, use_container_width=True, hide_index=True)
            export_buttons(_tbl16, "مدفوعات الموردين", "supplier_payments", subtitle=auth.client_name(), totals=["المبلغ"], key="pur_pay")

    with t7:
        _returns_tab(cid, user)
