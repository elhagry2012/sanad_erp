"""تصدير التقارير إلى Excel (.xlsx) وPDF من DataFrames (منطق بدون واجهة، قابل للاختبار).

Excel: أرقام حقيقية بتنسيقات رقمية (لا نصوص)، تواريخ حقيقية، إجماليات بمعادلات SUM حية، تجميد الرأس وفلتر،
       واتجاه الورقة من اليمين لليسار. النصوص التي تبدأ بـ "=" تُكتب كنص حتى لا تُنفَّذ كمعادلات.
PDF:   ReportLab + خط DejaVu المرفق + تشكيل العربية (utils/arabic.py)، أعمدة بترتيب RTL، رأس مكرر، ترقيم صفحات.
"""
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from utils.arabic import visual

ROOT = Path(__file__).resolve().parent.parent
FONT_REG = ROOT / "assets" / "fonts" / "DejaVuSans.ttf"
FONT_BOLD = ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf"
LOGO = ROOT / "assets" / "logo.png"

NAVY, GOLD, GREEN, SAND, GRID = "0A1F44", "C8A24A", "0F9D75", "F5F1E6", "D9D4C5"
MAX_PDF_ROWS = 5000
MAX_XLSX_ROWS = 200_000
FMT_INT = "#,##0"
FMT_MONEY = "#,##0.00;[Red]-#,##0.00"
FMT_QTY = "#,##0.000;[Red]-#,##0.000"
FMT_COST = "#,##0.0000;[Red]-#,##0.0000"
_EMOJI = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$")


@dataclass
class Section:
    name: str
    df: pd.DataFrame
    formats: dict = field(default_factory=dict)     # اسم العمود -> تنسيق Excel (مثل FMT_QTY) أو "text"
    totals: list = field(default_factory=list)      # أعمدة تُجمع في صف الإجمالي
    note: str = ""


@dataclass
class Report:
    title: str
    subtitle: str = ""
    sections: list = field(default_factory=list)
    generated_by: str = ""
    generated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


# ===================================================================== مساعدات مشتركة
def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    return df.reset_index(drop=True)


def _col_kind(series: pd.Series) -> str:
    """date | datetime | int | float | text"""
    s = series.dropna()
    if pd.api.types.is_bool_dtype(series):
        return "text"
    if pd.api.types.is_integer_dtype(series):
        return "int"
    if pd.api.types.is_float_dtype(series):
        return "float"
    if s.empty:
        return "text"
    vals = s.astype(str)
    if all(_ISO_DATE.match(v) for v in vals):
        return "date"
    if all(_ISO_DT.match(v) for v in vals):
        return "datetime"
    return "text"


def _default_format(kind: str) -> str:
    return {"int": FMT_INT, "float": FMT_MONEY, "date": "yyyy-mm-dd", "datetime": "yyyy-mm-dd hh:mm"}.get(kind, "General")


def _decimals(fmt: str) -> int | None:
    """عدد المنازل العشرية المطلوبة من تنسيق Excel (للـ PDF)."""
    m = re.search(r"0\.(0+)", fmt or "")
    if m:
        return len(m.group(1))
    return 0 if fmt and "#,##0" in fmt else None


def _fmt_cell(v, kind: str, fmt: str) -> str:
    """نص الخلية في PDF بنفس تنسيق Excel."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if kind in ("int", "float"):
        d = _decimals(fmt)
        d = (0 if kind == "int" else 2) if d is None else d
        return f"{float(v):,.{d}f}"
    if kind == "datetime":
        return str(v).replace("T", " ")[:16]
    return str(v)


def _safe_sheet_name(name: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "-", str(name)).strip()[:31] or "Sheet"
    cand, i = base, 2
    while cand.lower() in used:
        suffix = f" ({i})"
        cand = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(cand.lower())
    return cand


# ===================================================================== Excel
def build_xlsx(report: Report) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    used: set = set()
    font = lambda **k: Font(name="Arial", size=k.pop("size", 10), **k)      # noqa: E731
    thin = Side(style="thin", color=GRID)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor=NAVY)
    zebra = PatternFill("solid", fgColor=SAND)
    total_fill = PatternFill("solid", fgColor="E8DFC4")

    for sec in report.sections or [Section("تقرير", pd.DataFrame())]:
        df = _clean_df(sec.df).head(MAX_XLSX_ROWS)
        ws = wb.create_sheet(_safe_sheet_name(sec.name, used))
        ws.sheet_view.rightToLeft = True
        ws.sheet_properties.tabColor = GREEN
        ws["A1"] = report.title if len(report.sections) <= 1 else f"{report.title} — {sec.name}"
        ws["A1"].font = font(size=14, bold=True, color=NAVY)
        ws["A2"] = report.subtitle
        ws["A2"].font = font(size=10, color="555555")
        ws["A3"] = f"تم التصدير في {report.generated_at}" + (f" بواسطة {report.generated_by}" if report.generated_by else "")
        ws["A3"].font = font(size=9, italic=True, color="777777")
        if sec.note:
            ws["A4"] = sec.note
            ws["A4"].font = font(size=9, color="555555")

        hdr_row = 6
        kinds = {c: _col_kind(df[c]) for c in df.columns}
        fmts = {c: sec.formats.get(c) or _default_format(kinds[c]) for c in df.columns}
        for j, col in enumerate(df.columns, 1):
            cell = ws.cell(row=hdr_row, column=j, value=col)
            cell.font = font(bold=True, color="FFFFFF")
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border
        ws.row_dimensions[hdr_row].height = 22

        widths = {c: max(8, min(45, len(c) * 1.2 + 3)) for c in df.columns}
        for i, rec in enumerate(df.itertuples(index=False), start=hdr_row + 1):
            for j, (col, v) in enumerate(zip(df.columns, rec), 1):
                kind = kinds[col]
                if v is None or (not isinstance(v, str) and pd.isna(v)):
                    val = None
                elif kind == "date":
                    val = date.fromisoformat(str(v))
                elif kind == "datetime":
                    val = datetime.fromisoformat(str(v).replace(" ", "T"))
                elif kind in ("int", "float"):
                    val = int(v) if kind == "int" else float(v)
                else:
                    val = v if isinstance(v, (int, float)) else str(v)
                cell = ws.cell(row=i, column=j, value=val)
                if isinstance(val, str):
                    if val[:1] in "=+-@":
                        cell.data_type = "s"                 # لا تُنفَّذ كمعادلة أبدًا
                    widths[col] = max(widths[col], min(45, len(val) * 1.15 + 2))
                else:
                    widths[col] = max(widths[col], min(45, len(f"{val:,.2f}" if isinstance(val, float) else str(val)) + 3))
                cell.font = font()
                cell.border = border
                if fmts[col] != "General" and kind != "text":
                    cell.number_format = fmts[col]
                elif sec.formats.get(col) and sec.formats[col] != "text" and isinstance(val, (int, float)):
                    cell.number_format = sec.formats[col]
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if (i - hdr_row) % 2 == 0:
                    cell.fill = zebra

        last = hdr_row + len(df)
        if df.empty:
            ws.cell(row=hdr_row + 1, column=1, value="لا توجد بيانات").font = font(italic=True, color="777777")
        else:
            ws.auto_filter.ref = f"A{hdr_row}:{get_column_letter(len(df.columns))}{last}"
        tcols = [c for c in sec.totals if c in df.columns]
        if tcols and not df.empty:
            tr = last + 1
            lab = ws.cell(row=tr, column=1, value="الإجمالي")
            lab.font = font(bold=True)
            for j, col in enumerate(df.columns, 1):
                cell = ws.cell(row=tr, column=j)
                cell.fill, cell.border = total_fill, border
                if j == 1:
                    cell.value, cell.font = "الإجمالي", font(bold=True)
                if col in tcols:
                    L = get_column_letter(j)
                    cell.value = f"=SUM({L}{hdr_row + 1}:{L}{last})"
                    cell.number_format = fmts[col] if fmts[col] != "General" else FMT_MONEY
                    cell.font = font(bold=True)
                    cell.alignment = Alignment(horizontal="right")
                    widths[col] = max(widths[col], 14)
        for j, col in enumerate(df.columns, 1):
            ws.column_dimensions[get_column_letter(j)].width = widths[col]
        ws.freeze_panes = ws.cell(row=hdr_row + 1, column=1)
        ws.page_setup.orientation = "landscape" if len(df.columns) > 7 else "portrait"
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.print_title_rows = f"{hdr_row}:{hdr_row}"
        ws.oddFooter.center.text = "صفحة &P من &N"
    wb.properties.title, wb.properties.creator = report.title, "Sanad Business Advisory"
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ===================================================================== PDF
_fonts_ready = False


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont("SanadAr", str(FONT_REG)))
    pdfmetrics.registerFont(TTFont("SanadAr-Bold", str(FONT_BOLD)))
    pdfmetrics.registerFontFamily("SanadAr", normal="SanadAr", bold="SanadAr-Bold")
    _fonts_ready = True


def _fit(text: str, width: float, font: str, size: float) -> str:
    """يقصّ النص (قبل التشكيل) حتى يتسع في عرض العمود، مع «…»."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    text = _EMOJI.sub("", text).strip()
    if stringWidth(visual(text), font, size) <= width:
        return text
    t = text
    while len(t) > 1 and stringWidth(visual(t + "…"), font, size) > width:
        t = t[:-1]
    return t + "…"


def build_pdf(report: Report) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    _register_fonts()
    sections = [Section(s.name, _clean_df(s.df).head(MAX_PDF_ROWS), s.formats, s.totals, s.note)
                for s in (report.sections or [Section("تقرير", pd.DataFrame())])]
    wide = max((len(s.df.columns) for s in sections), default=0) > 6
    page = landscape(A4) if wide else A4
    margin = 12 * mm
    avail = page[0] - 2 * margin
    navy, gold, sand = colors.HexColor("#" + NAVY), colors.HexColor("#" + GOLD), colors.HexColor("#" + SAND)

    P = lambda text, **k: Paragraph(escape(visual(_EMOJI.sub("", str(text)).strip())), ParagraphStyle("p", fontName=k.pop("font", "SanadAr"), **k))  # noqa: E731
    story = []

    # --- رأس التقرير: الشعار يمين، العنوان وسط، بيانات التصدير يسار
    logo = Image(str(LOGO), width=18 * mm, height=18 * mm) if LOGO.exists() else ""
    meta = P(f"{report.generated_at}" + (f"  |  {report.generated_by}" if report.generated_by else ""),
             fontSize=7.5, leading=10, textColor=colors.HexColor("#777777"), alignment=0)
    title = [P(report.title, font="SanadAr-Bold", fontSize=15, leading=19, textColor=navy, alignment=TA_CENTER)]
    if report.subtitle:
        title.append(P(report.subtitle, fontSize=9, leading=12, textColor=colors.HexColor("#555555"), alignment=TA_CENTER))
    head = Table([[meta, title, logo]], colWidths=[avail * 0.25, avail * 0.55, avail * 0.2])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (2, 0), (2, 0), "RIGHT"),
                              ("LINEBELOW", (0, 0), (-1, 0), 1.5, gold), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [head, Spacer(1, 8)]

    for si, sec in enumerate(sections):
        df = sec.df
        if len(sections) > 1 or sec.name:
            story.append(P(sec.name, font="SanadAr-Bold", fontSize=11.5, leading=15, textColor=navy, alignment=TA_RIGHT))
        if sec.note:
            story.append(P(sec.note, fontSize=8, leading=11, textColor=colors.HexColor("#666666"), alignment=TA_RIGHT))
        story.append(Spacer(1, 3))
        if df.empty:
            story += [P("لا توجد بيانات", fontSize=9, textColor=colors.HexColor("#777777"), alignment=TA_RIGHT), Spacer(1, 10)]
            continue
        cols = list(df.columns)
        kinds = {c: _col_kind(df[c]) for c in cols}
        fmts = {c: sec.formats.get(c) or _default_format(kinds[c]) for c in cols}
        size = 9 if len(cols) <= 5 else 8 if len(cols) <= 8 else 7
        pad = 5
        body = [[_fmt_cell(v, kinds[c], fmts[c]) for c, v in zip(cols, rec)] for rec in df.itertuples(index=False)]
        tcols = [c for c in sec.totals if c in cols]
        total_row = None
        if tcols:
            total_row = ["" for _ in cols]
            total_row[0] = "الإجمالي"
            for j, c in enumerate(cols):
                if c in tcols:
                    total_row[j] = _fmt_cell(pd.to_numeric(df[c], errors="coerce").sum(),
                                             "int" if kinds[c] == "int" else "float", fmts[c])
        # عرض الأعمدة: حسب المحتوى ثم تحجيم ليملأ الصفحة، والنص الأطول يُقصّ بـ …
        sample = body[:300] + ([total_row] if total_row else [])
        natural = [max([stringWidth(visual(str(c)), "SanadAr-Bold", size)] +
                       [stringWidth(visual(r[j]), "SanadAr", size) for r in sample]) + 2 * pad for j, c in enumerate(cols)]
        scale = avail / sum(natural)
        if scale < 1:
            floor = 22
            widths = [max(floor, w * scale) for w in natural]
            over = sum(widths) / avail
            widths = [w / over for w in widths] if over > 1 else widths
        else:
            widths = [w * min(scale, 1.35) for w in natural]
        widths = [min(w, avail) for w in widths]

        def cell(text, w, font="SanadAr"):
            return visual(_fit(text, w - 2 * pad, font, size))

        header = [cell(str(c), widths[j], "SanadAr-Bold") for j, c in enumerate(cols)]
        rows = [header] + [[cell(r[j], widths[j]) for j in range(len(cols))] for r in body]
        if total_row:
            rows.append([cell(total_row[j], widths[j], "SanadAr-Bold") for j in range(len(cols))])
        # RTL: أول عمود منطقي يظهر في أقصى اليمين
        rows = [list(reversed(r)) for r in rows]
        widths = list(reversed(widths))
        t = Table(rows, colWidths=widths, repeatRows=1, hAlign="RIGHT")
        style = [("FONTNAME", (0, 0), (-1, -1), "SanadAr"), ("FONTSIZE", (0, 0), (-1, -1), size),
                 ("FONTNAME", (0, 0), (-1, 0), "SanadAr-Bold"), ("BACKGROUND", (0, 0), (-1, 0), navy),
                 ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                 ("ALIGN", (0, 1), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("TOPPADDING", (0, 0), (-1, -1), 2.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
                 ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
                 ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#" + GRID)),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1 if not total_row else -2), [colors.white, sand])]
        if total_row:
            style += [("FONTNAME", (0, -1), (-1, -1), "SanadAr-Bold"), ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8DFC4")),
                      ("LINEABOVE", (0, -1), (-1, -1), 1, navy)]
        t.setStyle(TableStyle(style))
        story += [t, Spacer(1, 14)]
        if len(sec.df) >= MAX_PDF_ROWS:
            story.append(P(f"تم عرض أول {MAX_PDF_ROWS} صف فقط. استخدم تصدير Excel للبيانات الكاملة.", fontSize=8,
                           textColor=colors.HexColor("#B3402F"), alignment=TA_RIGHT))

    class Numbered(rl_canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._pages)
            for st in self._pages:
                self.__dict__.update(st)
                self.setFont("SanadAr", 7.5)
                self.setFillColor(colors.HexColor("#777777"))
                self.drawCentredString(page[0] / 2, 7 * mm, visual(f"صفحة {self._pageNumber} من {n}"))
                self.drawString(margin, 7 * mm, "Sanad Business Advisory")
                super().showPage()
            super().save()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=14 * mm,
                            title=report.title, author="Sanad Business Advisory")
    doc.build(story, canvasmaker=Numbered)
    return buf.getvalue()
