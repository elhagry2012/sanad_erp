"""سند للأعمال - نقطة التشغيل. التشغيل: streamlit run app.py"""
import streamlit as st

from database.db import init_db, backup_db, schema_version, ensure_schema
from utils import theme, auth, security, config, controls, i18n

i18n.install()                                  # ترجمة الواجهة عند اختيار English (مرة لكل عملية خادم؛ لا أثر في العربية)
st.set_page_config(page_title=i18n.t("سند للأعمال"), page_icon=str(theme.LOGO), layout="wide", initial_sidebar_state="auto")


@st.cache_resource(ttl=3600, show_spinner=False)
def _bootstrap(schema: int) -> bool:
    """تهيئة المخازن الافتراضية والمدير الأول والنسخ الاحتياطي. بصمة البنية جزء من مفتاح التخزين المؤقت،
    فتُعاد التهيئة فورًا بعد أي تحديث للكود (وترقية البنية نفسها تتم تلقائيًا عند كل اتصال بالقاعدة)."""
    init_db()
    security.ensure_admin()
    backup_db()
    return True


@st.cache_resource(show_spinner=False)
def _pwa_version() -> str:
    """يولّد ملفات تطبيق الويب (الأيقونات والـ manifest) في مجلد static داخل المشروع فقط، مرة لكل تشغيل للخادم."""
    try:
        from tools import pwa_setup
        return pwa_setup.build_app_static()
    except Exception:                          # لا يمنع تشغيل النظام؛ فقط لن يظهر خيار التثبيت
        return ""


ensure_schema()                                 # كل تحميل: القاعدة تُرحَّل تلقائيًا (بعد نسخة احتياطية) إن كانت أقدم من الكود
_bootstrap(schema_version())                    # schema بلا شرطة سفلية: جزء من مفتاح التخزين المؤقت فعلًا
theme.inject_css()
if _v := _pwa_version():
    theme.pwa_head(_v)
auth.refresh_session()

user = auth.current_user()
if user and not user.get("must_change"):
    theme.header_controls()                     # الوضع الداكن/الفاتح والعربية/English: ثابتان أعلى كل صفحة
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
    theme.mode_picker()
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

# (القسم، المفتاح، الدالة، العنوان، الأيقونة، المسار) — الأقسام تسهّل التنقل في القائمة المنزلقة على الجوال
CATALOG = [
    ("العمليات اليومية", "dashboard", dashboard.render, "لوحة التحكم", "📊", "dashboard"),
    ("العمليات اليومية", "sales", sales.render, "مبيعات الكاشير", "🛒", "sales"),
    ("العمليات اليومية", "purchases", purchases.render, "المشتريات والموردون", "🧾", "purchases"),
    ("العمليات اليومية", "inventory", inventory.render, "المخزن والأسعار والجرد", "📦", "inventory"),
    ("العمليات اليومية", "warehouses", warehouses.render, "المخازن وأذون التحويل", "🏬", "warehouses"),
    ("العمليات اليومية", "production", production.render, "التصنيع والوصفات", "🍳", "production"),
    ("العمليات اليومية", "waste", waste.render, "الهدر والتالف", "🗑️", "waste"),
    ("المالية", "treasury", treasury.render, "الخزينة والبنك", "💰", "treasury"),
    ("المالية", "expenses", expenses.render, "المصروفات", "💸", "expenses"),
    ("التحليل والتقارير", "reports", reports_center.render, "مركز التقارير", "🗂️", "reports"),
    ("التحليل والتقارير", "menu", menu_engineering.render, "هندسة القائمة", "🎯", "menu"),
    ("الإعداد", "clients", clients.render, "العملاء (المطاعم)", "🏪", "clients"),
]
sections, module_of = {}, {}
for section, key, fn, title, icon, path in CATALOG:
    if perm(key)[0]:
        sections.setdefault(i18n.t(section), []).append(st.Page(fn, title=i18n.t(title), icon=icon, url_path=path, default=(key == "dashboard")))
        module_of[path] = key
if not module_of:
    st.warning("لا توجد وحدات متاحة لحسابك. تواصل مع المدير العام.")
sections[i18n.t("حسابي")] = [st.Page(notifications.render, title=i18n.t("الإشعارات وطلبات التعديل"), icon="🔔", url_path="inbox"),
                             st.Page(account.render, title=i18n.t("حسابي"), icon="🔑", url_path="account", default=not module_of.get("dashboard"))]
# صفحات المدير العام: لا تُضاف لغيره، ولكل منها حراسة داخلية
if auth.is_admin():
    sections[i18n.t("المدير العام")] = [
        st.Page(productivity.render, title=i18n.t("إنتاجية الموظفين والأمان"), icon="📈", url_path="productivity"),
        st.Page(admin_controls.render, title=i18n.t("الضوابط المالية"), icon="⚖️", url_path="controls"),
        st.Page(admin_users.render, title=i18n.t("المستخدمون والصلاحيات"), icon="🛡️", url_path="admin"),
        st.Page(data_import.render, title=i18n.t("استيراد البيانات"), icon="📥", url_path="import")]

current = st.navigation(sections)
mod = module_of.get(current.url_path)
if mod and not perm(mod)[1]:
    st.info("لديك صلاحية العرض فقط في هذه الوحدة. أي محاولة تعديل ستُرفض وتُسجَّل.")

try:
    current.run()
except security.PermissionDenied as e:      # أي رفض غير ملتقط داخل الصفحة يظهر كرسالة واضحة
    st.error(str(e))
