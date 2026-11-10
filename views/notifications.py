"""الإشعارات وطلبات التعديل: صندوق وارد لكل مستخدم، وطلب تعديل مستند بموافقة المدير العام."""
import pandas as pd
import streamlit as st

from database.db import query
from utils import theme, auth, controls

KIND_ICON = {"large_transaction": "💰", "edit_request": "✉️", "edit_decision": "✅"}


def _decision_queue(me):
    st.subheader("طلبات التعديل المعلّقة")
    pend = controls.requests_df(status="pending")
    if pend.empty:
        st.success("لا توجد طلبات معلّقة.")
        return
    for _, r in pend.iterrows():
        with st.container(border=True):
            st.markdown(f"**{r['doc_label']} #{r['doc_id']}** — {r['client']} — طلبها **{r['requested_by']}** في {r['created_at']}")
            st.write(f"السبب: {r['reason']}")
            if r["proposed"]:
                st.write(f"التعديل المطلوب: {r['proposed']}")
            note = st.text_input("ملاحظة القرار (تصل للموظف)", key=f"dn_{r['id']}")
            neg = st.checkbox("السماح بالرصيد السالب عند التنفيذ (إن لزم)", key=f"dg_{r['id']}")
            c1, c2, c3 = st.columns(3)
            if c1.button("✅ قبول وتنفيذ الإلغاء/العكس الآن", key=f"dx_{r['id']}", type="primary"):
                try:
                    controls.decide_request(int(r["id"]), True, note, me, execute=True, allow_negative=neg)
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
            st.caption("يمكنك أيضًا إرسال الطلب مباشرة من شاشة المستند (زر «طلب تعديل»).")
            c1, c2 = st.columns(2)
            dt = c1.selectbox("نوع المستند", list(controls.DOC_TYPES), format_func=lambda k: controls.DOC_TYPES[k][0], key="rq_type")
            did = c2.number_input("رقم المستند", min_value=1, step=1, key="rq_id")
            reason = st.text_area("سبب الطلب", key="rq_reason")
            proposed = st.text_input("التعديل المطلوب (اختياري)", key="rq_prop")
            if st.button("إرسال الطلب للمدير العام", type="primary"):
                try:
                    controls.create_edit_request(cid, dt, int(did), reason, proposed, user)
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
            cols = ["id", "created_at", "client", "doc_label", "doc_id", "requested_by", "reason", "الحالة", "decided_by", "decision_note"]
            st.dataframe(show[cols].rename(columns={"id": "رقم", "created_at": "التاريخ", "client": "المطعم", "doc_label": "المستند", "doc_id": "رقم المستند",
                                                    "requested_by": "الطالب", "reason": "السبب", "decided_by": "قرار بواسطة", "decision_note": "ملاحظة"}),
                         use_container_width=True, hide_index=True)

    if admin:
        with ts[2]:
            _decision_queue(user)
