"""الهوية البصرية لسند للأعمال: ألوان الشعار، الوضع الفاتح/الداكن، الاتجاه (RTL/LTR)، التجاوب مع الجوال، والرسوم البيانية.

الوضع الداكن يُطبَّق لكل مستخدم على حدة (لا يغيّر إعداد الخادم لبقية المستخدمين): كل الألوان رموز (tokens) تُبنى منها
ورقة أنماط واحدة، والرسوم البيانية تأخذ لوحة ألوان مخصصة للخلفية الداكنة. الجداول (تُرسم على canvas) تُعكس ألوانها
بمرشح CSS حافظ للتدرج فتظهر داكنة بنفس التباين.
"""
import base64
import re
from pathlib import Path
import streamlit as st

from utils import i18n

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "assets" / "logo.png"

# ألوان الهوية (الفاتح)
NAVY = "#0A1F44"
GOLD = "#C8A24A"
GREEN = "#0F9D75"
GREEN_DARK = "#016A4C"
SAND = "#F5F1E6"
RED = "#B3402F"
CHART_COLORS = [NAVY, GREEN, GOLD, GREEN_DARK, "#5B6B8C", "#E3CD94"]

PALETTES = {
    "light": {"bg": "#FFFFFF", "surface": "#FFFFFF", "surface2": SAND, "border": "#E4DCC8", "text": NAVY, "muted": "#5B6B8C",
              "heading": NAVY, "accent": GOLD, "primary": GREEN, "primary_hover": GREEN_DARK, "on_primary": "#FFFFFF",
              "danger": RED, "positive": GREEN_DARK, "sidebar": NAVY, "sidebar_text": "#FFFFFF", "header": "#FFFFFFE6",
              "grid": "#EEEEEE", "chart": CHART_COLORS},
    # داكن: خلفية كحلية عميقة مشتقة من الشعار، نص عالي التباين، وألوان مميزة أفتح لتبقى مقروءة (تباين AA على الخلفية)
    "dark": {"bg": "#0B1220", "surface": "#111A2E", "surface2": "#17233D", "border": "#26324D", "text": "#E6EBF5", "muted": "#9AA7C2",
             "heading": "#F4F6FB", "accent": "#E0BE6A", "primary": "#1FB386", "primary_hover": "#27C997", "on_primary": "#04130D",
             "danger": "#F2705C", "positive": "#34D399", "sidebar": "#070D19", "sidebar_text": "#E6EBF5", "header": "#0B1220E6",
             "grid": "#22304D", "chart": ["#5B9CF6", "#1FB386", "#E0BE6A", "#F2705C", "#A78BFA", "#38BDF8", "#F59E0B", "#94A3B8"]},
}


def _logo_b64() -> str:
    return base64.b64encode(LOGO.read_bytes()).decode()


# ============================================================ المظهر (فاتح / داكن)
def appearance() -> str:
    v = st.session_state.get("appearance")
    if v is None:
        try:
            v = st.query_params.get("theme", "light")
        except Exception:
            v = "light"
    return v if v in PALETTES else "light"


def is_dark() -> bool:
    return appearance() == "dark"


def set_appearance(v: str) -> None:
    st.session_state["appearance"] = v if v in PALETTES else "light"
    if v == "dark":
        st.query_params["theme"] = "dark"
    else:
        st.query_params.pop("theme", None)


def P() -> dict:
    return PALETTES[appearance()]


# ============================================================ الجوال واللوحي
UI_MODES = {"auto": "تلقائي (حسب الجهاز)", "mobile": "📱 جوال / لوحي", "desktop": "🖥️ حاسوب"}
_MOBILE_UA = re.compile(r"Android|iPhone|iPad|iPod|Mobile|Tablet|Silk|Kindle|PlayBook|BlackBerry|Opera Mini|IEMobile", re.I)


def device_is_mobile() -> bool:
    """من User-Agent الطلب (هاتف أو لوحي). iPadOS الحديث يعرّف نفسه كجهاز Mac، فيُضبط يدويًا من القائمة الجانبية."""
    try:
        ua = st.context.headers.get("User-Agent", "") or ""
    except Exception:
        ua = ""
    return bool(_MOBILE_UA.search(ua))


def is_mobile() -> bool:
    """وضع العرض المختصر: تخطيط بطاقات بعناوين ظاهرة وأزرار كبيرة بدل الجداول الكثيفة."""
    mode = st.session_state.get("ui_mode")
    if mode is None:
        try:
            mode = st.query_params.get("ui", "auto")
        except Exception:
            mode = "auto"
    if mode == "mobile":
        return True
    if mode == "desktop":
        return False
    return device_is_mobile()


def mode_picker() -> None:
    """اختيار وضع العرض في القائمة الجانبية (يُحفظ في الرابط ليبقى بعد إعادة التحميل)."""
    qp = st.query_params.get("ui")
    if "ui_mode" not in st.session_state:
        st.session_state["ui_mode"] = qp if qp in UI_MODES else "auto"
    mode = st.selectbox("وضع العرض", list(UI_MODES), format_func=UI_MODES.get, key="ui_mode")
    if mode == "auto":
        st.query_params.pop("ui", None)
    else:
        st.query_params["ui"] = mode


_PWA_JS = """<script>
(function () {
  var d = document;
  d.documentElement.lang = "__LANG__";
  if (d.querySelector('link[rel="manifest"]')) return;
  var root = location.pathname.replace(/[^/]*$/, "");
  var base = root + "app/static/sanad-pwa/";
  function add(tag, attrs) { var e = d.createElement(tag); for (var k in attrs) e.setAttribute(k, attrs[k]); d.head.appendChild(e); }
  add("link", {rel: "manifest", href: base + "manifest.json?v=__V__"});
  add("link", {rel: "apple-touch-icon", href: base + "apple-touch-icon.png?v=__V__"});
  add("meta", {name: "theme-color", content: "#0A1F44"});
  add("meta", {name: "mobile-web-app-capable", content: "yes"});
  add("meta", {name: "apple-mobile-web-app-capable", content: "yes"});
  add("meta", {name: "apple-mobile-web-app-status-bar-style", content: "black-translucent"});
  add("meta", {name: "apple-mobile-web-app-title", content: "Sanad"});
  add("meta", {name: "application-name", content: "Sanad"});
  var vp = d.querySelector('meta[name="viewport"]');
  if (vp && vp.content.indexOf("viewport-fit") < 0) vp.content += ", viewport-fit=cover";
})();
</script>"""


def pwa_head(version: str = "1") -> None:
    """يضيف وسوم تطبيق الويب القابل للتثبيت إلى <head> الصفحة (النص ثابت لا يحتوي بيانات مستخدم)."""
    js = _PWA_JS.replace("__V__", re.sub(r"[^0-9a-f]", "", str(version))[:16] or "1").replace("__LANG__", i18n.lang())
    with st.sidebar:
        st.html(js, unsafe_allow_javascript=True)


def grid(n: int, per_row_mobile: int = 2):
    """أعمدة متجاوبة: n عمود على الحاسوب، و per_row_mobile في كل صف على الجوال (لصفوف المؤشرات والأزرار)."""
    if not is_mobile() or n <= per_row_mobile:
        return list(st.columns(n))
    out = []
    while len(out) < n:
        out += list(st.columns(min(per_row_mobile, n - len(out))))
    return out


# ============================================================ أزرار الشريط العلوي (المظهر واللغة)
def _toggle_theme():
    set_appearance("light" if is_dark() else "dark")


def _toggle_lang():
    i18n.set_lang("ar" if i18n.is_en() else "en")


def header_controls() -> None:
    """زرّا تبديل ثابتان في الشريط العلوي لكل الصفحات (وفي شاشة الدخول): الوضع الداكن/الفاتح، والعربية/English."""
    with st.container(key="sanad_topbar", horizontal=True, gap="small", width="content"):
        st.button("☀️" if is_dark() else "🌙", key="tb_theme", on_click=_toggle_theme,
                  help="الوضع الفاتح" if is_dark() else "الوضع الداكن")
        st.button("ع" if i18n.is_en() else "EN", key="tb_lang", on_click=_toggle_lang,
                  help="العربية" if i18n.is_en() else "English")


# ============================================================ ورقة الأنماط
def _css(p: dict, rtl: bool, dark: bool) -> str:
    start = "right" if rtl else "left"
    font = "'Cairo', 'Tahoma', sans-serif" if rtl else "'Inter', 'Cairo', 'Segoe UI', sans-serif"
    css = f"""
    @import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700&family=Inter:wght@400;500;600;700&display=swap');
    :root {{ color-scheme: {'dark' if dark else 'light'}; }}
    html, body, [class*="css"], .stApp, button, input, textarea, select {{ font-family: {font} !important; }}
    .stApp {{ direction: {'rtl' if rtl else 'ltr'}; text-align: {start}; background: {p['bg']}; color: {p['text']}; }}
    header[data-testid="stHeader"] {{ background: {p['header']}; backdrop-filter: blur(6px); }}
    .stApp p, .stApp li, .stApp label, .stApp span, .stMarkdown, [data-testid="stWidgetLabel"] * {{ color: {p['text']}; }}
    [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {{ color: {p['muted']} !important; }}
    h1, h2, h3, h4, h5 {{ color: {p['heading']} !important; font-weight: 700; letter-spacing: {'0' if rtl else '-.01em'}; }}
    [data-testid="stSidebar"] {{ direction: {'rtl' if rtl else 'ltr'}; background: {p['sidebar']}; border-inline-end: 1px solid {p['border']}; }}
    [data-testid="stSidebar"] * {{ color: {p['sidebar_text']}; }}
    [data-testid="stSidebar"] hr {{ border-color: {p['accent']}55; }}
    [data-testid="stSidebar"] [data-baseweb="select"] *, [data-testid="stSidebar"] input {{ color: {NAVY if not dark else p['text']} !important; }}
    [data-testid="stSidebar"] .stButton > button {{ background: {p['accent']}; color: {NAVY}; }}
    [data-testid="stSidebarNav"] [data-testid="stNavSectionHeader"], [data-testid="stSidebarNav"] header {{ color: {p['accent']} !important; opacity: .95; }}
    div[data-testid="stMetric"] {{ background: {p['surface2']}; border-inline-start: 5px solid {p['accent']};
        padding: 14px 18px; border-radius: 8px; {'box-shadow: 0 1px 0 ' + p['border'] + ' inset;' if dark else ''} }}
    div[data-testid="stMetricValue"] {{ color: {p['heading']}; font-weight: 700; font-variant-numeric: tabular-nums; }}
    div[data-testid="stMetricLabel"] * {{ color: {p['muted']} !important; }}
    .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
        background: {p['primary']}; color: {p['on_primary']}; border: 0; border-radius: 8px; font-weight: 600; }}
    .stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{ background: {p['primary_hover']}; color: {p['on_primary']}; }}
    .stButton > button *, .stDownloadButton > button *, .stFormSubmitButton > button * {{ color: {p['on_primary']} !important; }}
    .stButton > button:focus-visible, .stDownloadButton > button:focus-visible {{ outline: 3px solid {p['accent']}; outline-offset: 2px; }}
    .sanad-header {{ display: flex; align-items: center; gap: 18px; border-bottom: 3px solid {p['accent']}; padding-bottom: 10px; margin-bottom: 18px; }}
    .sanad-header img {{ height: 64px; {'filter: drop-shadow(0 0 1px #fff8) brightness(1.08);' if dark else ''} }}
    .sanad-header .t {{ font-size: 1.5rem; font-weight: 700; color: {p['heading']}; line-height: 1.2; }}
    .sanad-header .s {{ color: {p['positive']}; font-size: .95rem; }}
    .alert-up {{ color: {p['danger']}; font-weight: 600; }}
    .alert-down {{ color: {p['positive']}; font-weight: 600; }}
    .sanad-card-title {{ font-weight: 700; color: {p['heading']}; margin-bottom: .25rem; }}
    [data-testid="stVerticalBlockBorderWrapper"], [data-testid="stForm"], [data-testid="stExpander"] details {{ border-color: {p['border']} !important; }}
    [data-baseweb="tab-list"] {{ overflow-x: auto; scrollbar-width: thin; flex-wrap: nowrap; }}
    [data-baseweb="tab"] {{ white-space: nowrap; }}
    [data-baseweb="tab"][aria-selected="true"] p {{ color: {p['accent'] if dark else p['heading']} !important; font-weight: 700; }}
    [data-baseweb="tab-highlight"] {{ background: {p['accent']} !important; }}
    @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}

    /* top bar: theme + language toggles, fixed in the app header on every page */
    .st-key-sanad_topbar {{ position: fixed; top: .55rem; left: 50%; transform: translateX(-50%); z-index: 1000001; width: auto !important; }}
    .st-key-sanad_topbar .stButton > button {{ min-height: 34px !important; min-width: 44px; padding: .15rem .7rem !important; border-radius: 999px;
        background: {p['surface2']}; color: {p['heading']}; border: 1px solid {p['border']}; font-weight: 700; box-shadow: 0 1px 3px #0002; }}
    .st-key-sanad_topbar .stButton > button * {{ color: {p['heading']} !important; }}
    .st-key-sanad_topbar .stButton > button:hover {{ border-color: {p['accent']}; background: {p['surface']}; }}

    /* responsive layout */
    .block-container {{ max-width: 100%; }}
    [data-testid="stDataFrame"], [data-testid="stDataEditor"] {{ max-width: 100%; overflow-x: auto; }}
    @media (max-width: 1024px) {{
        [data-testid="stHorizontalBlock"]:not(.st-key-pi_lines [data-testid="stHorizontalBlock"]) {{ flex-wrap: wrap; row-gap: .5rem; }}
        [data-testid="stHorizontalBlock"]:not(.st-key-pi_lines [data-testid="stHorizontalBlock"]) > [data-testid="stColumn"] {{
            min-width: calc(50% - 1rem) !important; flex: 1 1 calc(50% - 1rem) !important; }}
        .block-container {{ padding: 1.25rem 1rem 5rem; }}
    }}
    @media (max-width: 640px) {{
        [data-testid="stHorizontalBlock"]:not(.st-key-pi_lines [data-testid="stHorizontalBlock"]) > [data-testid="stColumn"] {{
            min-width: 100% !important; flex: 1 1 100% !important; }}
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) > [data-testid="stColumn"],
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] > div [data-testid="stButton"]:only-child) > [data-testid="stColumn"] {{
            min-width: calc(50% - .5rem) !important; flex: 1 1 calc(50% - .5rem) !important; }}
        .block-container {{ padding: .75rem .65rem 6rem; }}
        .sanad-header {{ gap: 10px; padding-bottom: 6px; margin-bottom: 10px; }}
        .sanad-header img {{ height: 40px; }}
        .sanad-header .t {{ font-size: 1.15rem; }}
        .sanad-header .s {{ font-size: .8rem; }}
        h1 {{ font-size: 1.45rem; }} h2 {{ font-size: 1.25rem; }} h3 {{ font-size: 1.1rem; }}
        div[data-testid="stMetric"] {{ padding: 8px 10px; }}
        div[data-testid="stMetricValue"] {{ font-size: 1.25rem; }}
        div[data-testid="stMetricLabel"] p {{ font-size: .8rem; white-space: normal; }}
        .st-key-sanad_topbar {{ top: .6rem; }}
    }}
    @media (pointer: coarse) {{
        .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button,
        [data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-primary"] {{ min-height: 46px; padding: .55rem 1rem; }}
        input, textarea, [data-baseweb="select"] > div, [data-baseweb="input"] > div {{ min-height: 46px; font-size: 16px !important; }}
        [data-testid="stNumberInput"] button {{ min-width: 44px; min-height: 46px; }}
        [data-baseweb="tab"] {{ min-height: 48px; padding: 0 14px; font-size: 1rem; }}
        [data-testid="stCheckbox"] label, [data-testid="stRadio"] label {{ min-height: 40px; display: flex; align-items: center; padding: 4px 0; }}
        [data-testid="stExpander"] summary {{ min-height: 48px; }}
        [data-testid="stSidebarNav"] a {{ min-height: 46px; display: flex; align-items: center; }}
        .st-key-sanad_topbar .stButton > button {{ min-height: 40px !important; min-width: 48px; }}
    }}
    @media (display-mode: standalone) {{
        .stApp {{ padding-bottom: env(safe-area-inset-bottom); }}
        header[data-testid="stHeader"] {{ padding-top: env(safe-area-inset-top); }}
    }}
    """
    if dark:
        css += f"""
    /* dark mode: form controls, menus, alerts, tables */
    [data-baseweb="input"] > div, [data-baseweb="base-input"], [data-baseweb="select"] > div, [data-baseweb="textarea"],
    .stNumberInput input, .stDateInput input, .stTextInput input, .stTextArea textarea, .stTimeInput input {{
        background: {p['surface2']} !important; color: {p['text']} !important; border-color: {p['border']} !important; }}
    input::placeholder, textarea::placeholder {{ color: {p['muted']} !important; opacity: 1; }}
    [data-testid="stNumberInput"] button {{ background: {p['surface']} !important; color: {p['text']} !important; border-color: {p['border']} !important; }}
    [data-baseweb="popover"] ul, [data-baseweb="popover"] [role="listbox"], [data-baseweb="menu"], [data-baseweb="calendar"],
    [data-baseweb="popover"] > div {{ background: {p['surface']} !important; color: {p['text']} !important; }}
    [data-baseweb="popover"] li, [data-baseweb="calendar"] * {{ color: {p['text']} !important; }}
    [data-baseweb="popover"] li:hover, [data-baseweb="popover"] li[aria-selected="true"] {{ background: {p['surface2']} !important; }}
    [data-baseweb="tag"] {{ background: {p['surface2']} !important; }}
    [data-testid="stExpander"] details, [data-testid="stExpander"] summary {{ background: {p['surface']}; }}
    [data-testid="stExpander"] summary:hover * {{ color: {p['accent']} !important; }}
    [data-testid="stFileUploaderDropzone"] {{ background: {p['surface2']} !important; border-color: {p['border']} !important; }}
    [data-testid="stAlert"] {{ border: 1px solid {p['border']}; }}
    [data-testid="stAlert"] * {{ color: {p['text']} !important; }}
    [data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {{ background: {p['surface']}; }}
    [data-testid="stTooltipContent"], [data-baseweb="tooltip"] * {{ background: {p['surface2']} !important; color: {p['text']} !important; }}
    hr {{ border-color: {p['border']} !important; }}
    code {{ background: {p['surface2']} !important; color: {p['accent']} !important; }}
    /* data grids are drawn on canvas with the light palette: invert lightness while keeping hues -> crisp dark tables */
    [data-testid="stDataFrame"] canvas, [data-testid="stDataEditor"] canvas,
    [data-testid="stDataFrameResizable"] canvas {{ filter: invert(.92) hue-rotate(180deg) contrast(1.05); }}
    [data-testid="stDataFrame"], [data-testid="stDataEditor"] {{ border: 1px solid {p['border']}; border-radius: 8px; }}
    [data-testid="stTable"] table, [data-testid="stTable"] th, [data-testid="stTable"] td {{ color: {p['text']} !important; border-color: {p['border']} !important; }}
    [data-testid="stTable"] th {{ background: {p['surface2']} !important; }}
    [data-testid="stPlotlyChart"] {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; padding: 4px; }}
    """
    return css


def inject_css() -> None:
    p, rtl, dark = P(), not i18n.is_en(), is_dark()
    st.markdown(f"<style>{_css(p, rtl, dark)}</style>", unsafe_allow_html=True)
    if not is_mobile():
        # أسطر فاتورة الشراء على الحاسوب: أعمدة ثابتة وتمرير أفقي على الشاشات الضيقة بدل الالتفاف
        st.markdown("""<style>
        .st-key-pi_lines { overflow-x: auto; }
        .st-key-pi_lines [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; min-width: 880px; }
        .st-key-pi_lines [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width: 0 !important; }
        </style>""", unsafe_allow_html=True)
    else:
        # وضع الجوال على شاشة واسعة (لوحي أفقي): التفاف الأعمدة وأهداف لمس كبيرة
        st.markdown("""<style>
        [data-testid="stHorizontalBlock"] { flex-wrap: wrap; row-gap: .5rem; }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width: calc(50% - 1rem) !important; flex: 1 1 calc(50% - 1rem) !important; }
        .stButton > button, .stDownloadButton > button { min-height: 46px; }
        input, [data-baseweb="select"] > div { min-height: 46px; font-size: 16px !important; }
        </style>""", unsafe_allow_html=True)


def login_css() -> None:
    """شاشة الدخول في شاشة واحدة بلا تمرير: شعار صغير، بطاقة مضغوطة في المنتصف، ومسافات رأسية محكمة."""
    p = P()
    st.markdown(f"""<style>
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {{ display: none; }}
    .block-container {{ max-width: 440px !important; padding: 3.2rem 1rem 1rem !important; }}
    .block-container [data-testid="stVerticalBlock"] {{ gap: .55rem; }}
    .sanad-login-head {{ text-align: center; margin: 0 0 .35rem; }}
    .sanad-login-head img {{ height: 72px; width: auto; {'filter: brightness(1.08);' if is_dark() else ''} }}
    .sanad-login-head .t {{ font-size: 1.2rem; font-weight: 700; color: {p['heading']}; margin-top: .35rem; line-height: 1.3; }}
    .sanad-login-head .s {{ font-size: .82rem; color: {p['muted']}; margin-top: .1rem; }}
    [data-testid="stForm"] {{ padding: 1rem 1rem .75rem; background: {p['surface']}; border-radius: 12px;
        box-shadow: 0 6px 24px {'#0008' if is_dark() else '#0A1F4414'}; }}
    [data-testid="stForm"] [data-testid="stVerticalBlock"] {{ gap: .45rem; }}
    [data-testid="stForm"] .stFormSubmitButton > button {{ min-height: 44px; margin-top: .25rem; font-size: 1rem; }}
    @media (max-height: 640px) {{
        .block-container {{ padding-top: 2.8rem !important; }}
        .sanad-login-head img {{ height: 52px; }}
        .sanad-login-head .s {{ display: none; }}
    }}
    </style>""", unsafe_allow_html=True)


def login_head() -> None:
    st.markdown(f"""<div class="sanad-login-head">
      <img src="data:image/png;base64,{_logo_b64()}" alt="{i18n.t('شعار سند للأعمال')}">
      <div class="t">{i18n.t('تسجيل الدخول')} — {i18n.t('سند للأعمال')}</div>
      <div class="s">{i18n.t('نظام إدارة المطاعم: المشتريات، المخزون بالدفعات، التصنيع، المبيعات والخزينة')}</div>
    </div>""", unsafe_allow_html=True)


def header(title: str, subtitle: str = "") -> None:
    st.markdown(f"""
    <div class="sanad-header">
      <img src="data:image/png;base64,{_logo_b64()}" alt="{i18n.t('شعار سند للأعمال')}">
      <div><div class="t">{i18n.t(title)}</div><div class="s">{i18n.t(subtitle)}</div></div>
    </div>
    """, unsafe_allow_html=True)


def style_fig(fig):
    """تنسيق موحّد لكل الرسوم: خط واضح، لوحة ألوان مناسبة للوضع الحالي، شبكة خافتة، وترجمة العناوين."""
    p, dark = P(), is_dark()
    font = "Cairo, Tahoma" if not i18n.is_en() else "Inter, Cairo, Segoe UI"
    fig.update_layout(
        font=dict(family=font, color=p["text"], size=13),
        title=dict(font=dict(color=p["heading"], size=16)),
        paper_bgcolor="rgba(0,0,0,0)" if dark else "white", plot_bgcolor="rgba(0,0,0,0)" if dark else "white",
        colorway=p["chart"], margin=dict(l=10, r=10, t=50, b=10),
        legend=dict(orientation="h", y=-0.2, font=dict(color=p["text"]), bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor=p["surface2"], bordercolor=p["border"], font=dict(color=p["text"], family=font)),
    )
    fig.update_xaxes(gridcolor=p["grid"], zerolinecolor=p["grid"], linecolor=p["border"], tickfont=dict(color=p["muted"]),
                     title_font=dict(color=p["muted"]))
    fig.update_yaxes(gridcolor=p["grid"], zerolinecolor=p["grid"], linecolor=p["border"], tickfont=dict(color=p["muted"]),
                     title_font=dict(color=p["muted"]))
    if dark:
        # ألوان صريحة مثبتة على المخطط (color_discrete_map، ألوان الدونات) تُستبدل بنظيرتها الداكنة لتبقى متناسقة
        swap = dict(zip(CHART_COLORS, p["chart"]))
        for tr in fig.data:
            m = getattr(tr, "marker", None)
            if m is not None and isinstance(getattr(m, "color", None), str) and m.color in swap:
                m.color = swap[m.color]
            if getattr(tr, "type", "") == "pie":
                tr.marker.line = dict(color=p["bg"], width=2)
    return i18n.figure(fig)
