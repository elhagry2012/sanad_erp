from datetime import date
import pandas as pd
import streamlit as st

from database.db import query
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
from utils import theme, auth, inventory, reports, documents, controls


def _voucher_card(cid, v, user, can_act=True):
    with st.container(border=True):
        a, b, c = st.columns([2, 2, 1])
        a.markdown(f"**إذن تحويل رقم {int(v['id']):05d}** — {v['voucher_date']}")
        b.write(f"من **{v['from_name']}** إلى **{v['to_name']}**")
        c.write(inventory.STATUS_AR.get(v["status"], v["status"]) + (" — معكوس" if int(v.get("is_voided", 0) or 0) else ""))
        st.dataframe(inventory.voucher_lines(int(v["id"])).round(4), use_container_width=True, hide_index=True)
        st.caption(f"أنشأه: {v['created_by']}" + (f" — اعتمده: {v['approved_by']} ({v['approved_at']})" if v["approved_by"] else "") +
                   (f" — {v['notes']}" if v["notes"] else ""))
        if v["status"] == "pending" and can_act:
            x, y, z = st.columns([1.3, 1.3, 2])
            neg = z.checkbox("السماح بالعجز", key=f"neg_{v['id']}")
            if x.button("✅ اعتماد وتنفيذ", key=f"ap_{v['id']}"):
                try:
                    val = inventory.approve_voucher(cid, int(v["id"]), user, neg)
                    st.success(f"تم الاعتماد. قيمة المحوَّل {val:,.2f}")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if y.button("❌ إلغاء الإذن", key=f"cn_{v['id']}"):
                try:
                    inventory.cancel_voucher(cid, int(v["id"]), user)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("المخازن وأذون التحويل", auth.client_name())
    whs = reports.warehouse_list(cid, active_only=False)
    t1, t2, t3 = st.tabs(["المخازن", "أذون التحويل بين المخازن", "أرصدة المخازن (مجمّع)"])

    with t1:
        show = whs.copy()
        show["النوع"] = show["kind"].map(inventory.KIND_AR)
        show["مخزن التشغيل"] = show["is_operations"].map({1: "✔ تُخصم منه المبيعات والتصنيع", 0: ""})
        show["الحالة"] = show["is_active"].map({1: "مفعّل", 0: "معطّل"})
        st.dataframe(show[["name", "النوع", "مخزن التشغيل", "الحالة"]].rename(columns={"name": "المخزن"}), use_container_width=True, hide_index=True)
        with st.form("wh_new", clear_on_submit=True):
            a, b = st.columns(2)
            n = a.text_input("اسم مخزن جديد")
            k = b.selectbox("النوع", list(inventory.KIND_AR), format_func=inventory.KIND_AR.get)
            if st.form_submit_button("إضافة مخزن"):
                try:
                    inventory.create_warehouse(cid, n, k, user)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        active = whs[whs["is_active"] == 1]
        if not active.empty:
            st.subheader("إدارة مخزن")
            nm = st.selectbox("المخزن", active["name"], key="wh_manage")
            wid = int(active.loc[active["name"] == nm, "id"].iloc[0])
            c1, c2, c3 = st.columns(3)
            newname = c1.text_input("اسم جديد", key="wh_rename")
            if c1.button("إعادة التسمية"):
                try:
                    inventory.rename_warehouse(cid, wid, newname, user)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if c2.button("تعيينه مخزن التشغيل"):
                try:
                    inventory.set_operations_warehouse(cid, wid, user)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if c3.button("تعطيل المخزن"):
                try:
                    inventory.set_warehouse_active(cid, wid, False, user)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        st.caption("مخزن التشغيل هو الذي تُخصم منه مكونات المبيعات وتشغيلات الإنتاج. الأسلوب الموصى به: الشراء للمخزن الرئيسي، "
                   "ثم إذن تحويل للمخزن الإنتاجي، فيُصرف منه التصنيع والبيع.")

    with t2:
        active = whs[whs["is_active"] == 1]
        items = query("SELECT id, name FROM items WHERE client_id=? ORDER BY is_semi, name", (cid,))
        units = query("""SELECT DISTINCT u.unit_name FROM item_units u JOIN items i ON i.id=u.item_id WHERE i.client_id=?""", (cid,))
        base_units = query("SELECT DISTINCT unit FROM items WHERE client_id=?", (cid,))["unit"].tolist()
        unit_opts = sorted(set(units["unit_name"]) | set(base_units))
        if len(active) < 2 or items.empty:
            st.info("يلزم مخزنان مفعّلان وصنف واحد على الأقل لإنشاء إذن تحويل.")
        else:
            st.subheader("إذن تحويل جديد")
            with st.form("voucher_form"):
                a, b, c = st.columns(3)
                src = a.selectbox("من مخزن (المصدر)", active["name"])
                dst = b.selectbox("إلى مخزن (المستلم)", active["name"], index=min(1, len(active) - 1))
                day = c.date_input("تاريخ الإذن", date.today())
                grid = pd.DataFrame({"الصنف": [None] * 4, "الوحدة": [None] * 4, "الكمية": [0.0] * 4})
                ed = st.data_editor(grid, num_rows="dynamic", use_container_width=True, key="voucher_grid", column_config={
                    "الصنف": st.column_config.SelectboxColumn(options=list(items["name"]), required=True),
                    "الوحدة": st.column_config.SelectboxColumn(options=unit_opts, help="اتركها فارغة لوحدة الاستهلاك"),
                    "الكمية": st.column_config.NumberColumn(min_value=0.0, format="%.3f")})
                notes = st.text_input("ملاحظات")
                st.caption("الكمية تُحوَّل تلقائيًا لوحدة الاستهلاك (مثال: 1 كرتونة = 90 قطعة). الإذن يُنشأ «قيد الاعتماد» ولا يؤثر على الأرصدة قبل اعتماده.")
                if st.form_submit_button("إنشاء الإذن"):
                    ids = dict(zip(items["name"], items["id"]))
                    lines = [(int(ids[r["الصنف"]]), r["الكمية"], r["الوحدة"] if isinstance(r["الوحدة"], str) else None)
                             for _, r in ed.iterrows() if r["الصنف"] in ids and r["الكمية"] > 0]
                    try:
                        vid = inventory.create_voucher(cid, int(active.loc[active["name"] == src, "id"].iloc[0]),
                                                       int(active.loc[active["name"] == dst, "id"].iloc[0]), day, lines, notes, user)
                        st.success(f"تم إنشاء الإذن رقم {vid:05d} بحالة «قيد الاعتماد».")
                    except ValueError as e:
                        st.error(str(e))
        st.subheader("الأذون")
        status = st.radio("الحالة", ["الكل", "قيد الاعتماد", "معتمد", "ملغي"], horizontal=True)
        vs = inventory.vouchers(cid)
        key = {"قيد الاعتماد": "pending", "معتمد": "approved", "ملغي": "cancelled"}.get(status)
        if key:
            vs = vs[vs["status"] == key]
        if vs.empty:
            st.info("لا توجد أذون.")
        for _, v in vs.head(30).iterrows():
            _voucher_card(cid, v, user)
        if not vs.empty:
            pv = st.selectbox("طباعة / تصدير إذن", list(vs["id"]), format_func=lambda i: f"إذن رقم {int(i):05d}", key="vch_print")
            vrow = vs[vs["id"] == pv].iloc[0]
            export_buttons(inventory.voucher_lines(int(pv)), f"إذن تحويل بين المخازن رقم {int(pv):05d}", "transfer_voucher",
                           subtitle=f"من {vrow['from_name']} إلى {vrow['to_name']} — {vrow['voucher_date']} — {inventory.STATUS_AR.get(vrow['status'], '')}",
                           formats={"الكمية": FMT_QTY, "ما يعادل (وحدة الاستهلاك)": FMT_QTY}, key="vch_exp")
            _voided = bool(int(vrow.get("is_voided", 0) or 0))
            if vrow["status"] == "approved" and not _voided:
                if auth.is_admin():
                    with st.expander("🛠 أدوات المدير العام: إلغاء هذا الإذن المعتمد وعكس أثره"):
                        vr = st.text_input("سبب الإلغاء", key=f"vvr_{pv}")
                        vneg = st.checkbox("السماح بالرصيد السالب إن كان جزء من الكمية قد استُهلك", key=f"vvn_{pv}")
                        if st.button("🗑 إلغاء الإذن وإرجاع الكميات (بتواريخ صلاحيتها) للمخزن المصدر", key=f"vvb_{pv}"):
                            try:
                                documents.void_voucher(cid, int(pv), vr, user, vneg)
                                st.rerun()
                            except ValueError as ex:
                                st.error(str(ex))
                else:
                    with st.expander("✉️ طلب تعديل/إلغاء هذا الإذن (يصل المدير العام فورًا)"):
                        rr = st.text_area("السبب", key=f"vrq_{pv}")
                        if st.button("إرسال الطلب", key=f"vrqb_{pv}"):
                            try:
                                controls.create_edit_request(cid, "transfer_voucher", int(pv), rr, "", user)
                                st.success("تم إرسال الطلب للمدير العام.")
                            except ValueError as ex:
                                st.error(str(ex))
            elif _voided:
                st.error(f"إذن معكوس. السبب: {vrow.get('void_reason') or '—'}")

    with t3:
        mat = reports.warehouse_matrix(cid)
        if mat.empty:
            st.info("لا توجد أرصدة.")
        else:
            m = st.columns(2)
            m[0].metric("إجمالي قيمة المخزون (كل المخازن)", f"{mat['القيمة'].sum():,.2f}")
            m[1].metric("عدد الأصناف", len(mat))
            _tbl9 = mat
            st.dataframe(_tbl9.round(3), use_container_width=True, hide_index=True)
            export_buttons(_tbl9, "أرصدة المخازن المجمّعة (كل المخازن)", "warehouse_matrix", subtitle=auth.client_name(), formats={**{c: FMT_QTY for c in _tbl9.columns if c not in ("الصنف", "وحدة الاستهلاك", "تكلفة الوحدة", "القيمة")}, "تكلفة الوحدة": FMT_COST, "القيمة": FMT_MONEY}, totals=["القيمة"], key="wh_matrix")
            st.subheader("قيمة كل مخزن")
            vals = []
            for _, w in whs[whs["is_active"] == 1].iterrows():
                b = reports.warehouse_balances(cid, int(w["id"]))
                vals.append({"المخزن": w["name"], "عدد الأصناف": len(b), "القيمة": b["القيمة"].sum() if not b.empty else 0.0})
            _tbl8 = pd.DataFrame(vals)
            st.dataframe(_tbl8.round(2), use_container_width=True, hide_index=True)
            export_buttons(_tbl8, "قيمة المخزون لكل مخزن", "warehouse_values", subtitle=auth.client_name(), formats={"القيمة": FMT_MONEY, "عدد الأصناف": "#,##0"}, totals=["القيمة"], key="wh_vals")
