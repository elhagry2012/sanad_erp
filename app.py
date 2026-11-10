"""سند للأعمال - نقطة التشغيل. التشغيل: streamlit run app.py"""
import streamlit as st

from database.db import init_db, backup_db
from utils import theme, auth, security, config, controls

st.set_page_config(page_title="سند للأعمال", page_icon=str(theme.LOGO), layout="wide")


@st.cache_resource(ttl=3600, show_spinner=False)
def _bootstrap() -> bool:
    """تهيئة القاعدة والترقية والمدير الأول والنسخ الاحتياطي: مرة لكل عملية، وتتجدد كل ساعة."""
    init_db()
    security.ensure_admin()
    backup_db()
    return True


_bootstrap()
theme.inject_css()
auth.refresh_session()

user = auth.current_user()
if not user:
    auth.login_screen()
    st.stop()
if user.get("must_change"):
    auth.forced_password_change()
    st.stop()

from views import (dashboard, purchases, inventory, warehouses, treasury, production, sales, expenses, waste,
                   menu_engineering, clients, data_import, account, admin_users, productivity,
                   reports_center, notifications, admin_controls)

st.logo(str(theme.LOGO), size="large")
access = security.get_access(user["username"]) or {"admin": False, "all_modules": False, "modules": {}, "explicit": {},
                                                     "all_clients": False, "clients": set(), "active": False}


def perm(module):
    return security.module_access(access, module)


with st.sidebar:
    st.markdown(f"**{user['full_name']}**")
    st.caption("المدير العام" if auth.is_admin() else "شريك / فريق العمل")
    auth.sidebar_client_picker()
    if st.button("تسجيل الخروج", use_container_width=True):
        auth.logout()
    def _badge():
        """شارة الإشعارات: تُحدَّث تلقائيًا كل 20 ثانية وتُظهر تنبيهًا منبثقًا عند وصول إشعار جديد."""
        n = controls.unread_count(user["username"], auth.is_admin())
        seen = st.session_state.get("_notif_seen")
        if seen is not None and n > seen and hasattr(st, "toast"):
            st.toast(f"لديك {n - seen} إشعار جديد", icon="🔔")
        st.session_state["_notif_seen"] = n
        st.markdown(f"🔔 **{n}** إشعار غير مقروء" if n else "🔔 لا توجد إشعارات جديدة")
    _frag = getattr(st, "fragment", None)
    (_frag(run_every=20)(_badge) if _frag else _badge)()
    if config.IS_PROD is False:
        st.caption("بيئة تطوير")

CATALOG = [
    ("dashboard", dashboard.render, "لوحة التحكم", "📊", "dashboard"),
    ("purchases", purchases.render, "المشتريات والموردون", "🧾", "purchases"),
    ("inventory", inventory.render, "المخزن والأسعار والجرد", "📦", "inventory"),
    ("warehouses", warehouses.render, "المخازن وأذون التحويل", "🏬", "warehouses"),
    ("treasury", treasury.render, "الخزينة والبنك", "💰", "treasury"),
    ("production", production.render, "التصنيع والوصفات", "🍳", "production"),
    ("sales", sales.render, "مبيعات الكاشير", "🛒", "sales"),
    ("expenses", expenses.render, "المصروفات", "💸", "expenses"),
    ("waste", waste.render, "الهدر والتالف", "🗑️", "waste"),
    ("menu", menu_engineering.render, "هندسة القائمة", "🎯", "menu"),
    ("clients", clients.render, "العملاء (المطاعم)", "🏪", "clients"),
    ("import", data_import.render, "استيراد البيانات", "📥", "import"),
    ("reports", reports_center.render, "مركز التقارير", "🗂️", "reports"),
]
pages, module_of = [], {}
for key, fn, title, icon, path in CATALOG:
    if perm(key)[0]:
        pages.append(st.Page(fn, title=title, icon=icon, url_path=path, default=(key == "dashboard")))
        module_of[path] = key
if not pages:
    st.warning("لا توجد وحدات متاحة لحسابك. تواصل مع المدير العام.")
pages.append(st.Page(notifications.render, title="الإشعارات وطلبات التعديل", icon="🔔", url_path="inbox"))
pages.append(st.Page(account.render, title="حسابي", icon="🔑", url_path="account", default=not module_of.get("dashboard")))
# صفحات المدير العام: لا تُضاف لغيره، ولكل منها حراسة داخلية
if auth.is_admin():
    pages += [st.Page(productivity.render, title="إنتاجية الموظفين والأمان", icon="📈", url_path="productivity"),
              st.Page(admin_controls.render, title="الضوابط المالية", icon="⚖️", url_path="controls"),
              st.Page(admin_users.render, title="المستخدمون والصلاحيات", icon="🛡️", url_path="admin")]

current = st.navigation(pages)
mod = module_of.get(current.url_path)
if mod and not perm(mod)[1]:
    st.info("لديك صلاحية العرض فقط في هذه الوحدة. أي محاولة تعديل ستُرفض وتُسجَّل.")

try:
    current.run()
except security.PermissionDenied as e:      # أي رفض غير ملتقط داخل الصفحة يظهر كرسالة واضحة
    st.error(str(e))
