"""الإشعارات وطلبات التعديل: صندوق وارد لكل مستخدم، وطلب تعديل مستند بموافقة المدير العام."""
import pandas as pd
import streamlit as st

from database.db import query
from utils import theme, auth, controls, documents

KIND_ICON = {"large_transaction": "💰", "edit_request": "✉️", "edit_decision": "✅"}


def _adjust_request_box(r, me):
    """طلب تعديل مباشر لفاتورة شراء: عرض الفروق، وإمكانية تعديل القيم قبل الاعتماد، ثم التطبيق التلقائي."""
    from views.purchases import adjust_preview
    proposed = controls.request_changes(r["payload"])
    cur = query("""SELECT l.id, i.name, COALESCE(l.unit_name, i.unit) AS unit FROM purchase_lines l JOIN items i ON i.id=l.item_id
                   WHERE l.invoice_id=?""", (int(r["doc_id"]),)).set_index("id")
    NQ, NP = "الكمية المعتمدة", "السعر المعتمد"
    grid = pd.DataFrame([{"line_id": p["line_id"], "الصنف": cur["name"].get(p["line_id"], "؟"), "الوحدة": cur["unit"].get(p["line_id"], ""),
                          "الكمية قبل": p.get("old_qty"), "السعر قبل": p.get("old_price"), NQ: p["qty"], NP: p["price"]} for p in proposed])
    if grid.empty:
        st.error("الطلب لا يحتوي أسطر تعديل.")
        return None
    st.caption("القيم المقترحة من الموظف (يمكنك تعديلها قبل الاعتماد):")
    ed = st.data_editor(grid, num_rows="fixed", hide_index=True, use_container_width=True, key=f"dq_ed_{r['id']}",
                        disabled=[c for c in grid.columns if c not in (NQ, NP)],
                        column_config={"line_id": None, NQ: st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
                                       NP: st.column_config.NumberColumn(min_value=0.0, format="%.2f")})
    changes = [{**p, "qty": float(e[NQ]) if pd.notna(e[NQ]) else p["qty"], "price": float(e[NP]) if pd.notna(e[NP]) else p["price"]}
               for p, (_, e) in zip(proposed, ed.iterrows())]
    try:
        adjust_preview(documents.preview_adjustment(int(r["client_id"]), int(r["doc_id"]), changes))
    except ValueError as e:
        st.error(f"لا يمكن تطبيق الطلب كما هو: {e}")
        return None
    return changes


def _decision_queue(me):
    st.subheader("طلبات التعديل المعلّقة")
    pend = controls.requests_df(status="pending")
    if pend.empty:
        st.success("لا توجد طلبات معلّقة.")
        return
    for _, r in pend.iterrows():
        adjust = r["request_kind"] == "adjust"
        with st.container(border=True):
            st.markdown(f"**{r['kind_label']} — {r['doc_label']} {r['doc_no']}** — {r['client']} — طلبها **{r['requested_by']}** في {r['created_at']}")
            st.write(f"السبب: {r['reason']}")
            changes = None
            if adjust:
                changes = _adjust_request_box(r, me)
            elif r["proposed"]:
                st.write(f"التعديل المطلوب: {r['proposed']}")
            note = st.text_input("ملاحظة القرار (تصل للموظف)", key=f"dn_{r['id']}")
            neg = st.checkbox("السماح بالرصيد السالب عند التنفيذ (إن لزم)", key=f"dg_{r['id']}")
            c1, c2, c3 = st.columns(3)
            label = "✅ قبول وتطبيق التعديل على الفاتورة" if adjust else "✅ قبول وتنفيذ الإلغاء/العكس الآن"
            if c1.button(label, key=f"dx_{r['id']}", type="primary", disabled=adjust and changes is None):
                try:
                    controls.decide_request(int(r["id"]), True, note, me, execute=True, allow_negative=neg, changes=changes)
                    st.rerun()
                except ValueError as e:
                    st.error(f"تعذّر التنفيذ (الطلب ما زال معلّقًا): {e}")
            if c2.button("👍 قبول فقط (سأعدّل بنفسي)", key=f"da_{r['id']}"):
                controls.decide_request(int(r["id"]), True, note, me)
                st.rerun()
            if c3.button("⛔ رفض", key=f"dr_{r['id']}"):
                controls.decide_request(int(r["id"]), False, note, me)
                st.rerun()


def render():
    user = auth.username()
    admin = auth.is_admin()
    theme.header("الإشعارات وطلبات التعديل", f"المستخدم: {user}")
    tabs = ["🔔 الإشعارات", "✉️ طلبات التعديل"] + (["🛡️ قرارات المدير العام"] if admin else [])
    ts = st.tabs(tabs)

    with ts[0]:
        inbox = controls.inbox(user, admin)
        unread = int((inbox["is_read"] == 0).sum()) if not inbox.empty else 0
        c1, c2 = st.columns([3, 1])
        c1.metric("غير مقروء", unread)
        if c2.button("تعليم الكل كمقروء") and unread:
            controls.mark_read(user, admin)
            st.rerun()
        if inbox.empty:
            st.info("لا توجد إشعارات.")
        for _, n in inbox.head(60).iterrows():
            icon = KIND_ICON.get(n["kind"], "🔔")
            dot = "🔵 " if not n["is_read"] else ""
            with st.container(border=True):
                st.markdown(f"{dot}{icon} **{n['title']}**  \n<small>{n['created_at']}</small>", unsafe_allow_html=True)
                if n["body"]:
                    st.write(n["body"])

    with ts[1]:
        cid = auth.current_client_id()
        if cid and not admin:
            st.markdown("**طلب تعديل جديد**")
            st.caption("لتصحيح كمية أو سعر في فاتورة شراء استخدم «طلب تعديل مباشر» من شاشة الفاتورة (المشتريات ← الفواتير)؛ "
                       "يُطبَّق تلقائيًا عند اعتماده. هنا تُرسل طلبًا عامًا لأي مستند.")
            c1, c2 = st.columns(2)
            dt = c1.selectbox("نوع المستند", list(controls.DOC_TYPES), format_func=lambda k: controls.DOC_TYPES[k][0], key="rq_type")
            ref = c2.text_input("رقم المستند", key="rq_ref", placeholder="مثل PI-2026-00012 أو الرقم الداخلي")
            reason = st.text_area("سبب الطلب", key="rq_reason")
            proposed = st.text_input("التعديل المطلوب (اختياري)", key="rq_prop")
            if st.button("إرسال الطلب للمدير العام", type="primary"):
                try:
                    controls.create_edit_request(cid, dt, controls.resolve_doc(cid, dt, ref), reason, proposed, user)
                    st.success("تم الإرسال وإشعار المدير العام.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        mine = controls.requests_df(user=None if admin else user)
        if mine.empty:
            st.info("لا توجد طلبات.")
        else:
            show = mine.copy()
            show["الحالة"] = show["status"].map({"pending": "⏳ معلّق", "approved": "✅ مقبول", "rejected": "⛔ مرفوض"})
            cols = ["id", "created_at", "client", "kind_label", "doc_label", "doc_no", "requested_by", "reason", "proposed", "الحالة",
                    "decided_by", "decision_note"]
            st.dataframe(show[cols].rename(columns={"id": "رقم", "created_at": "التاريخ", "client": "المطعم", "kind_label": "نوع الطلب",
                                                    "doc_label": "المستند", "doc_no": "رقم المستند", "requested_by": "الطالب", "reason": "السبب",
                                                    "proposed": "التعديل المطلوب", "decided_by": "قرار بواسطة", "decision_note": "ملاحظة"}),
                         use_container_width=True, hide_index=True)

    if admin:
        with ts[2]:
            _decision_queue(user)
