"""أزرار «تصدير Excel / PDF» الموحدة لكل الجداول والتقارير (واجهة Streamlit).

- الملفات تُبنى عند الحاجة وتُخزَّن مؤقتًا (cache) لتفادي إعادة بنائها مع كل نقرة.
- جداول كبيرة (> 600 صف) تُجهَّز بنقرة واحدة بدل بنائها تلقائيًا في كل مرة.
- كل تنزيل يُسجَّل في audit_log (من صدّر ماذا ومتى) لتتبع خروج البيانات.
- الاسم الظاهر للمستخدم عربي، واسم الملف لاتيني آمن.
"""
import re
from datetime import date

import pandas as pd
import streamlit as st

from utils import auth, security
from utils import exporter as ex

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LAZY_ROWS = 600
Section = ex.Section
FMT_INT, FMT_MONEY, FMT_QTY, FMT_COST = ex.FMT_INT, ex.FMT_MONEY, ex.FMT_QTY, ex.FMT_COST


@st.cache_data(max_entries=48, ttl=300, show_spinner=False)
def _build(kind: str, title: str, subtitle: str, user: str, sections: tuple) -> bytes:
    rep = ex.Report(title, subtitle, [ex.Section(n, df, dict(f), list(t), note) for n, df, f, t, note in sections], user)
    return ex.build_xlsx(rep) if kind == "xlsx" else ex.build_pdf(rep)


def _log_export(user, cid, fmt, title, rows):
    security.log_event(user, "export", "export", f"{title} ({fmt}، {rows} صف)", cid)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "report"


def export_buttons(df: pd.DataFrame | None = None, title: str = "", filename: str = "report", *, subtitle: str = "",
                   formats: dict | None = None, totals: list | None = None, sections: list | None = None,
                   key: str | None = None, note: str = "") -> None:
    """يعرض زري Excel وPDF. مرّر df واحدًا أو sections=[Section(...)] لتقرير متعدد الأقسام."""
    secs = sections or [Section(title, df if df is not None else pd.DataFrame(), formats or {}, totals or [], note)]
    rows = sum(len(s.df) for s in secs)
    key = key or filename
    if rows == 0:
        return
    user, cid = auth.username(), auth.current_client_id()
    client = auth.client_name()
    sub = subtitle or (client if client and client != "كل المطاعم" else "")
    packed = tuple((s.name, s.df, tuple(sorted(s.formats.items())), tuple(s.totals), s.note) for s in secs)
    fname = f"{_slug(filename)}_{date.today().isoformat()}"
    if rows > LAZY_ROWS and not st.session_state.get(f"prep_{key}"):
        if st.button(f"⚙️ تجهيز ملفات التصدير ({rows:,} صف)", key=f"prepbtn_{key}"):
            st.session_state[f"prep_{key}"] = True
            st.rerun()
        return
    c1, c2, _ = st.columns([1.1, 1.1, 4])
    c1.download_button("📥 تصدير Excel", _build("xlsx", title, sub, user, packed), file_name=f"{fname}.xlsx", mime=XLSX_MIME,
                       key=f"x_{key}", on_click=_log_export, args=(user, cid, "Excel", title, rows))
    c2.download_button("📄 تصدير PDF", _build("pdf", title, sub, user, packed), file_name=f"{fname}.pdf", mime="application/pdf",
                       key=f"p_{key}", on_click=_log_export, args=(user, cid, "PDF", title, rows))
