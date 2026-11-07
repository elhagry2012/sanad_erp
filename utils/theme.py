"""الهوية البصرية لسند للأعمال (ألوان مستخرجة من الشعار) + دعم RTL."""
import base64
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "assets" / "logo.png"

NAVY = "#0A1F44"        # الكحلي: النص والعناوين والقائمة الجانبية
GOLD = "#C8A24A"        # الذهبي: التمييز والحدود والأزرار الثانوية
GREEN = "#0F9D75"       # الأخضر: الأزرار الرئيسية والقيم الإيجابية
GREEN_DARK = "#016A4C"
SAND = "#F5F1E6"        # خلفية هادئة مشتقة من الذهبي
RED = "#B3402F"         # للعجز والخسائر فقط
CHART_COLORS = [NAVY, GREEN, GOLD, GREEN_DARK, "#5B6B8C", "#E3CD94"]


def _logo_b64() -> str:
    return base64.b64encode(LOGO.read_bytes()).decode()


def inject_css() -> None:
    st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700&display=swap');
    html, body, [class*="css"], .stApp, button, input, textarea {{
        font-family: 'Cairo', 'Tahoma', sans-serif !important;
    }}
    .stApp {{ direction: rtl; text-align: right; background: #FFFFFF; color: {NAVY}; }}
    [data-testid="stSidebar"] {{ direction: rtl; background: {NAVY}; }}
    [data-testid="stSidebar"] * {{ color: #FFFFFF; }}
    [data-testid="stSidebar"] hr {{ border-color: {GOLD}55; }}
    [data-testid="stSidebar"] [data-baseweb="select"] *,
    [data-testid="stSidebar"] input {{ color: {NAVY} !important; }}
    [data-testid="stSidebar"] .stButton > button {{ background: {GOLD}; color: {NAVY}; }}
    h1, h2, h3 {{ color: {NAVY}; font-weight: 700; }}
    div[data-testid="stMetric"] {{
        background: {SAND}; border-right: 5px solid {GOLD};
        padding: 14px 18px; border-radius: 6px;
    }}
    div[data-testid="stMetricValue"] {{ color: {NAVY}; font-weight: 700; }}
    .stButton > button, .stDownloadButton > button {{
        background: {GREEN}; color: #fff; border: 0; border-radius: 6px; font-weight: 600;
    }}
    .stButton > button:hover {{ background: {GREEN_DARK}; color: #fff; }}
    .stButton > button:focus-visible {{ outline: 3px solid {GOLD}; }}
    .sanad-header {{
        display: flex; align-items: center; gap: 18px;
        border-bottom: 3px solid {GOLD}; padding-bottom: 10px; margin-bottom: 18px;
    }}
    .sanad-header img {{ height: 76px; }}
    .sanad-header .t {{ font-size: 1.5rem; font-weight: 700; color: {NAVY}; line-height: 1.2; }}
    .sanad-header .s {{ color: {GREEN_DARK}; font-size: .95rem; }}
    .alert-up   {{ color: {RED}; font-weight: 600; }}
    .alert-down {{ color: {GREEN_DARK}; font-weight: 600; }}
    @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
    </style>
    """, unsafe_allow_html=True)


def header(title: str, subtitle: str = "") -> None:
    st.markdown(f"""
    <div class="sanad-header">
      <img src="data:image/png;base64,{_logo_b64()}" alt="شعار سند للأعمال">
      <div><div class="t">{title}</div><div class="s">{subtitle}</div></div>
    </div>
    """, unsafe_allow_html=True)


def style_fig(fig):
    fig.update_layout(
        font=dict(family="Cairo, Tahoma", color=NAVY),
        paper_bgcolor="white", plot_bgcolor="white",
        colorway=CHART_COLORS, margin=dict(l=10, r=10, t=50, b=10),
        legend=dict(orientation="h", y=-0.2),
    )
    fig.update_xaxes(gridcolor="#EEE")
    fig.update_yaxes(gridcolor="#EEE")
    return fig
