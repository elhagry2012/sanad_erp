"""الجلسة وشاشة الدخول وحراسة الصفحات (واجهة Streamlit).

الجلسة: رمز عشوائي في session_state يقابله سجل في جدول sessions (خمول + عمر أقصى + إمكانية الإلغاء من المدير).
Streamlit يحمي رفع الملفات من CSRF عبر enableXsrfProtection (مفعّل في .streamlit/config.toml)،
ولا توجد نماذج HTML تقليدية تتأثر بـ CSRF لأن الاتصال عبر WebSocket بنفس الأصل.
"""
import streamlit as st

from database.db import query
from utils import security, theme

ALL_LABEL = "— كل المطاعم (نظرة عامة) —"
NO_CLIENT_LABEL = "— بدون مطعم (للمدير العام فقط) —"


# ------------------------------------------------------------------ الجلسة
def refresh_session() -> None:
    """يُستدعى في بداية كل تشغيل للصفحة: يتحقق من الرمز في قاعدة البيانات (خمول، عمر، تعطيل، إلغاء)."""
    token = st.session_state.get("sess_token")
    user = security.validate_session(token) if token else None
    if user:
        st.session_state["user"] = user
        return
    if token:
        st.session_state["session_msg"] = "انتهت جلستك (خمول أو إنهاء من المدير). سجّل الدخول من جديد."
    for k in ("user", "sess_token", "client_id", "client_name", "client_pick", "_login_client", "pi_rows",
              "pi_next", "pi_nonce", "last_invoice"):
        st.session_state.pop(k, None)


def current_user() -> dict | None:
    return st.session_state.get("user")


def username() -> str:
    u = current_user()
    return u["username"] if u else ""


def is_admin() -> bool:
    """المدير العام الوحيد هو momar بدور admin."""
    u = current_user()
    return bool(u and u["role"] == "admin" and u["username"] == security.ADMIN_USERNAME)


def require_admin() -> None:
    """حراسة داخل الصفحة نفسها، وأي محاولة بدون صلاحية تُسجَّل."""
    if not is_admin():
        security.log_event(username(), "access_denied", "permission", "محاولة دخول صفحة المدير")
        st.error("هذه الصفحة غير متاحة لحسابك.")
        st.stop()


def current_token() -> str | None:
    return st.session_state.get("sess_token")


ALL_CLIENTS_ERR = "ليس لديك صلاحية على هذا المطعم."


def current_client_id() -> int | None:
    return st.session_state.get("client_id")


def require_client() -> int:
    """للصفحات التشغيلية: تحتاج مطعمًا محددًا ومسموحًا للمستخدم."""
    cid = current_client_id()
    if not cid:
        if is_admin():
            st.warning("هذه الصفحة تعمل على مطعم واحد. اختر المطعم من القائمة الجانبية.")
        else:
            st.warning("لا يوجد مطعم محدد أو متاح لك. تواصل مع المدير العام.")
        st.stop()
    if not security.client_allowed(username(), cid):
        security.log_event(username(), "access_denied", "permission", "مطعم غير مسموح", int(cid))
        st.error(ALL_CLIENTS_ERR)
        st.stop()
    return int(cid)


def client_name() -> str:
    return st.session_state.get("client_name", "")


# ------------------------------------------------------------------ شاشات الدخول
def login_screen() -> None:
    clients = query("SELECT id, name FROM clients ORDER BY name")
    options = [NO_CLIENT_LABEL] + list(clients["name"])
    theme.login_css()
    theme.header_controls()
    theme.login_head()
    if st.session_state.get("session_msg"):
        st.warning(st.session_state.pop("session_msg"))
    with st.form("login"):
        pick = st.selectbox("المطعم", options)
        user = st.text_input("اسم المستخدم", autocomplete="username")
        pw = st.text_input("كلمة المرور", type="password", autocomplete="current-password")
        if st.form_submit_button("دخول", use_container_width=True):
            uname = (user or "").strip().lower()
            is_admin_name = uname == security.ADMIN_USERNAME
            if pick == NO_CLIENT_LABEL and not is_admin_name:
                st.error("اختر المطعم الذي ستعمل عليه.")
            else:
                info, msg = security.authenticate(user, pw)
                if not info:
                    st.error(msg)
                elif pick != NO_CLIENT_LABEL and not security.client_allowed(
                        info["username"], int(clients.loc[clients["name"] == pick, "id"].iloc[0])):
                    security.log_event(info["username"], "access_denied", "permission", f"محاولة دخول مطعم غير مصرح: {pick}")
                    st.error("ليس لديك صلاحية على هذا المطعم.")
                else:
                    st.session_state["sess_token"] = security.create_session(info["id"])
                    st.session_state["user"] = info
                    st.session_state["_login_client"] = ALL_LABEL if pick == NO_CLIENT_LABEL else pick
                    st.rerun()
    if clients.empty:
        st.caption("لا توجد مطاعم مسجلة بعد. المدير العام يستطيع الدخول وإضافة أول مطعم.")


def forced_password_change() -> None:
    """يظهر بدل التطبيق كله حتى يغيّر المستخدم كلمة المرور (مؤقتة، أو ضعيفة، أو أول دخول)."""
    theme.login_css()
    theme.header_controls()
    theme.login_head()
    st.markdown("#### يجب تغيير كلمة المرور قبل المتابعة")
    st.caption("كلمة المرور الحالية مؤقتة أو لا تحقق سياسة الأمان: 10 أحرف على الأقل، حروف وأرقام، وغير متوقعة.")
    with st.form("force_pw"):
        old = st.text_input("كلمة المرور الحالية", type="password")
        new = st.text_input("كلمة المرور الجديدة", type="password")
        new2 = st.text_input("تأكيد كلمة المرور الجديدة", type="password")
        if st.form_submit_button("تغيير ومتابعة", use_container_width=True):
            if new != new2:
                st.error("كلمتا المرور غير متطابقتين.")
            else:
                try:
                    security.change_own_password(username(), old, new, keep_token=current_token())
                    st.session_state["user"]["must_change"] = False
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    if st.button("تسجيل الخروج"):
        logout()


def logout() -> None:
    u, token = username(), current_token()
    if token:
        security.revoke_token(token)
    if u:
        security.log_logout(u)
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()


# ------------------------------------------------------------------ منتقي المطعم
def sidebar_client_picker() -> None:
    clients = query("SELECT id, name FROM clients ORDER BY name")
    allowed = security.allowed_client_ids(username())          # None = الكل
    if allowed is not None:
        clients = clients[clients["id"].isin(allowed)]
    names = list(clients["name"])
    options = ([ALL_LABEL] if is_admin() else []) + names
    if not options:
        st.session_state["client_id"] = None
        st.session_state["client_name"] = ""
        st.caption("لا توجد مطاعم متاحة لحسابك.")
        return
    init = st.session_state.pop("_login_client", None)
    if init in options:
        st.session_state["client_pick"] = init
    elif st.session_state.get("client_pick") not in options:
        st.session_state["client_pick"] = options[0]
    name = st.selectbox("المطعم", options, key="client_pick")
    if name == ALL_LABEL:
        st.session_state["client_id"] = None
        st.session_state["client_name"] = "كل المطاعم"
    else:
        st.session_state["client_id"] = int(clients.loc[clients["name"] == name, "id"].iloc[0])
        st.session_state["client_name"] = name
