"""الاستيراد الجماعي من Excel/CSV: قوالب جاهزة، تحقق دقيق بأرقام الصفوف، وإدخال ذري (كل شيء أو لا شيء).

المراحل: (1) قراءة آمنة للملف (حجم/صيغة/ضغط/عدد صفوف)  (2) تحقق كامل قبل أي كتابة يجمع كل الأخطاء برقم الصف
(3) إن وُجد خطأ واحد لا يُكتب شيء  (4) الكتابة كلها في معاملة واحدة، وأي استثناء يلغيها بالكامل.
أرقام الصفوف في التقارير = أرقام الصفوف في Excel (الصف 1 هو العناوين).
"""
import csv
import hashlib
import io
import math
import re
import unicodedata
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from database.db import get_conn, tx, query
from utils import inventory as inv, services
from utils.audit import log
from utils.security import guard

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED = 60 * 1024 * 1024
MAX_ROWS = 5000
EXAMPLE_PREFIX = "مثال"

# ----------------------------------------------------------------- تعريف الأعمدة
# (المفتاح، العنوان في القالب، مطلوب؟، مثال، شرح)
MATERIAL_COLUMNS = [
    ("name", "اسم الصنف", True, "مثال: دقيق فاخر", "اسم الخامة (غير مكرر)."),
    ("unit", "وحدة الاستهلاك", True, "كجم", "الوحدة التي تُستخدم بها الخامة في الوصفات وتُحسب بها التكلفة (جرام، مل، قطعة، كجم...)."),
    ("purchase_unit", "وحدة الشراء", False, "شيكارة", "الوحدة التي تشتري بها (كرتونة، شيكارة، زجاجة...). اتركها فارغة إن كانت نفس وحدة الاستهلاك."),
    ("factor", "معامل التحويل", False, 50, "كم وحدة استهلاك في وحدة شراء واحدة (1 شيكارة = 50 كجم). مطلوب عند تحديد وحدة الشراء."),
    ("min_qty", "حد إعادة الطلب", False, 25, "بوحدة الاستهلاك. يظهر تنبيه عند الوصول إليه."),
    ("warehouse", "المخزن", False, "المخزن الرئيسي", "اسم مخزن قائم لهذا المطعم. إن تُرك فارغًا يُستخدم المخزن الرئيسي."),
    ("opening_qty", "الرصيد الافتتاحي", False, 120, "بوحدة الاستهلاك (وليس وحدة الشراء)."),
    ("unit_cost", "تكلفة الوحدة (WAC)", False, 20.5, "متوسط تكلفة وحدة الاستهلاك الواحدة. مطلوبة إن وُجد رصيد افتتاحي."),
]
MATERIAL_ALIASES = {
    "name": ["الصنف", "اسم الخامة", "name", "item", "item_name"],
    "unit": ["وحدة الاستهلاك (الاساسيه)", "الوحدة الاساسية", "الوحدة", "unit", "base_unit", "consumption_unit"],
    "purchase_unit": ["purchase_unit"],
    "factor": ["معامل التحويل (1 وحدة شراء = كم وحدة استهلاك)", "factor", "conversion_factor"],
    "min_qty": ["min_qty", "reorder_level", "حد الطلب"],
    "warehouse": ["warehouse", "اسم المخزن"],
    "opening_qty": ["الرصيد الافتتاحي (بوحدة الاستهلاك)", "رصيد افتتاحي", "opening_qty", "opening_balance", "الكمية"],
    "unit_cost": ["تكلفة وحدة الاستهلاك", "تكلفة الوحدة", "wac", "unit_cost", "cost", "التكلفة"],
}
RECIPE_COLUMNS = [
    ("recipe", "اسم الوصفة", True, "مثال: كريب دجاج", "اسم الوجبة أو المنتج النصف مصنع. كرّره في كل سطر مكوّن."),
    ("kind", "نوع الوصفة", True, "وجبة", "«وجبة» أو «نصف مصنع»."),
    ("category", "تصنيف الوجبة", False, "كريب", "للوجبات فقط."),
    ("price", "سعر البيع", False, 95, "مطلوب للوجبات."),
    ("out_unit", "وحدة الناتج", False, "", "للنصف مصنع: وحدة ما تنتجه التشغيلة (كجم، لتر...)."),
    ("yield", "كمية ناتج التشغيلة", False, "", "للنصف مصنع: كم وحدة تنتج التشغيلة الواحدة."),
    ("ingredient", "المكوّن", True, "عجينة كريب", "اسم صنف قائم بالمخزن، أو منتج نصف مصنع معرّف في نفس الملف."),
    ("qty", "الكمية", True, 0.12, "كمية المكوّن في وجبة واحدة (أو تشغيلة واحدة للنصف مصنع)."),
    ("unit", "الوحدة", False, "كجم", "وحدة الكمية (وحدة الاستهلاك أو أي وحدة معرّفة للصنف). فارغة = وحدة الاستهلاك."),
]
RECIPE_ALIASES = {
    "recipe": ["الوصفة", "اسم الوجبة", "الوجبة", "recipe", "dish", "name"],
    "kind": ["النوع", "نوع", "kind", "type"],
    "category": ["التصنيف", "category"],
    "price": ["السعر", "سعر", "price"],
    "out_unit": ["وحدة الانتاج", "out_unit", "output_unit"],
    "yield": ["ناتج التشغيلة", "كمية الناتج", "yield", "yield_qty"],
    "ingredient": ["المكون", "الصنف", "ingredient", "item"],
    "qty": ["كمية", "qty", "quantity"],
    "unit": ["الوحدة", "unit"],
}
KINDS = {"وجبه": "dish", "dish": "dish", "meal": "dish", "نصف مصنع": "semi", "نصف مصنّع": "semi", "semi": "semi",
         "وصفه فرعيه": "semi", "منتج نصف مصنع": "semi", "sub": "semi"}
_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


# ----------------------------------------------------------------- أخطاء وتقارير
class ImportFileError(ValueError):
    """مشكلة في الملف نفسه (صيغة/حجم/أعمدة ناقصة)."""


class ImportValidationError(ValueError):
    """أخطاء في محتوى الصفوف؛ لم يُكتب شيء. التقرير في .report"""

    def __init__(self, report):
        super().__init__(f"{report.error_count} خطأ في الملف. لم يُستورد شيء.")
        self.report = report


@dataclass
class Issue:
    row: int | None
    column: str
    message: str
    level: str = "error"          # error | warning


@dataclass
class ImportReport:
    kind: str
    filename: str = ""
    rows_total: int = 0
    issues: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    created: int = 0
    skipped: int = 0
    file_sha: str = ""
    batch_id: int | None = None
    total_value: float = 0.0

    @property
    def error_count(self):
        return sum(1 for i in self.issues if i.level == "error")

    @property
    def warning_count(self):
        return sum(1 for i in self.issues if i.level == "warning")

    def issues_df(self) -> pd.DataFrame:
        return pd.DataFrame([{"الصف": i.row if i.row else "—", "العمود": i.column,
                              "النوع": "خطأ" if i.level == "error" else "تنبيه", "الرسالة": i.message} for i in self.issues],
                            columns=["الصف", "العمود", "النوع", "الرسالة"])


class _Issues:
    def __init__(self):
        self.items = []

    def err(self, row, col, msg):
        self.items.append(Issue(row, col, msg, "error"))

    def warn(self, row, col, msg):
        self.items.append(Issue(row, col, msg, "warning"))


# ----------------------------------------------------------------- مساعدات
def norm(s) -> str:
    s = unicodedata.normalize("NFKC", "" if s is None else str(s))
    s = re.sub("[\u064B-\u065F\u0670\u0640]", "", s).translate(_AR)
    return re.sub(r"\s+", " ", s).strip().casefold()


def text(v, maxlen=None) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(v))
    return re.sub(r"\s+", " ", s).strip()


def to_number(v):
    """يحوّل لرقم (يقبل أرقامًا عربية وفاصلة عشرية/آلاف). يرجع None للفارغ، ويرفع ValueError لغير الرقمي."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError
    if isinstance(v, (int, float)):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            raise ValueError
        return f
    s = str(v).strip().translate(_DIGITS).replace("٫", ".").replace("٬", ",").replace(" ", "").replace("\u00a0", "")
    if s == "":
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        elif re.fullmatch(r"\d+,\d+", s):
            s = s.replace(",", ".")
        else:
            raise ValueError
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        raise ValueError
    return float(s)


def _is_example(r: dict) -> bool:
    first = next((v for v in r.values() if v not in (None, "")), None)
    return isinstance(first, str) and first.strip().startswith(EXAMPLE_PREFIX)


# ----------------------------------------------------------------- قراءة الملف
def _check_xlsx_zip(data: bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        if "xl/workbook.xml" not in names:
            raise ImportFileError("الملف ليس مصنف Excel (.xlsx) صالحًا.")
        if any(n.endswith("vbaProject.bin") for n in names):
            raise ImportFileError("ملفات الماكرو غير مسموحة. احفظ الملف بصيغة .xlsx عادية.")
        if sum(i.file_size for i in z.infolist()) > MAX_UNCOMPRESSED:
            raise ImportFileError("حجم محتوى الملف بعد فك الضغط كبير جدًا.")
    except zipfile.BadZipFile:
        raise ImportFileError("الملف تالف أو ليس ملف Excel (.xlsx) حقيقيًا.")


def _decode_csv(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportFileError("تعذّر قراءة ترميز ملف CSV. احفظه بصيغة UTF-8.")


def read_table(data: bytes, filename: str):
    """يرجع (العناوين، [(رقم الصف، {العنوان: القيمة})]). أرقام الصفوف مطابقة لـ Excel."""
    if not data:
        raise ImportFileError("الملف فارغ.")
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError(f"حجم الملف أكبر من {MAX_FILE_BYTES // 1024 // 1024} ميجابايت.")
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    raw_rows: list = []
    formula_cells: dict = {}
    if ext == "xlsx":
        _check_xlsx_zip(data)
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            for i, row in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1):
                raw_rows.append((i, list(row)))
                if i > MAX_ROWS + 5:
                    raise ImportFileError(f"عدد الصفوف يتجاوز الحد الأقصى ({MAX_ROWS}).")
            wb.close()
            # خلايا بمعادلة بلا قيمة محفوظة: تُكتشف ليظهر خطأ واضح بدل «فارغ» مضلل
            wb2 = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            for i, row in enumerate(wb2[wb2.sheetnames[0]].iter_rows(min_row=1, values_only=True), start=1):
                if i > MAX_ROWS + 5:
                    break
                for j, v in enumerate(row):
                    if isinstance(v, str) and v.startswith("=") and j < len(raw_rows[i - 1][1]) and raw_rows[i - 1][1][j] is None:
                        formula_cells[(i, j)] = v
            wb2.close()
        except ImportFileError:
            raise
        except Exception:
            raise ImportFileError("تعذّر قراءة ملف Excel. تأكد أنه غير تالف وبصيغة .xlsx.")
    elif ext == "csv":
        content = _decode_csv(data)
        sample = content[:2048]
        delim = max([",", ";", "\t"], key=sample.count)
        for i, row in enumerate(csv.reader(io.StringIO(content), delimiter=delim), start=1):
            raw_rows.append((i, [c if c != "" else None for c in row]))
            if i > MAX_ROWS + 5:
                raise ImportFileError(f"عدد الصفوف يتجاوز الحد الأقصى ({MAX_ROWS}).")
    else:
        raise ImportFileError("الصيغ المدعومة: .xlsx أو .csv فقط.")
    header_idx = next((k for k, (_, r) in enumerate(raw_rows) if any(c not in (None, "") for c in r)), None)
    if header_idx is None:
        raise ImportFileError("الملف لا يحتوي على بيانات.")
    headers = [text(h) for h in raw_rows[header_idx][1]]
    rows = []
    for rn, cells in raw_rows[header_idx + 1:]:
        if all(c in (None, "") or (isinstance(c, str) and not c.strip()) for c in cells):
            continue
        rows.append((rn, {h: (c.strip() if isinstance(c, str) else c) for h, c in zip(headers, cells) if h},
                     {h: formula_cells.get((rn, j)) for j, h in enumerate(headers) if h and (rn, j) in formula_cells}))
    if len(rows) > MAX_ROWS:
        raise ImportFileError(f"عدد الصفوف ({len(rows)}) يتجاوز الحد الأقصى ({MAX_ROWS}).")
    return headers, rows


def _map_headers(headers, columns, aliases, issues_list=None):
    """يطابق عناوين الملف مع الأعمدة المعرّفة (بعد التطبيع). يرفع ImportFileError عند نقص عمود مطلوب."""
    lookup = {}
    for key, title, *_ in columns:
        for a in [title] + aliases.get(key, []):
            lookup.setdefault(norm(a), key)
    mapping, seen = {}, {}
    for h in headers:
        key = lookup.get(norm(h))
        if key:
            if key in seen:
                raise ImportFileError(f"العمود «{h}» مكرر في الملف (يطابق «{seen[key]}»).")
            seen[key] = h
            mapping[h] = key
    missing = [title for key, title, req, *_ in columns if req and key not in seen]
    if missing:
        raise ImportFileError("أعمدة مطلوبة غير موجودة: " + "، ".join(missing) +
                              ". الأعمدة الموجودة في ملفك: " + (" | ".join(h for h in headers if h) or "—") +
                              ". حمّل القالب واستخدم عناوينه كما هي.")
    unknown = [h for h in headers if h and h not in mapping]
    return mapping, unknown


def _records(rows, mapping):
    """[(row_no, {key: value}, {key: formula})] مع تجاهل صفوف المثال."""
    out = []
    for rn, cells, formulas in rows:
        rec = {mapping[h]: v for h, v in cells.items() if h in mapping}
        frm = {mapping[h]: f for h, f in formulas.items() if h in mapping}
        if _is_example(rec):
            continue
        out.append((rn, rec, frm))
    return out


def _col_title(columns, key):
    return next(t for k, t, *_ in columns if k == key)


# ================================================================= الخامات والرصيد الافتتاحي
def _material_ctx(conn, cid):
    items = {norm(n): (i, n, s) for i, n, s in conn.execute("SELECT id, name, is_semi FROM items WHERE client_id=?", (cid,)).fetchall()}
    whs = {norm(n): (i, n) for i, n in conn.execute("SELECT id, name FROM warehouses WHERE client_id=? AND is_active=1", (cid,)).fetchall()}
    return {"items": items, "whs": whs, "default_wh": inv.default_wh(conn, cid, "purchase"),
            "wh_names": [n for _, n in whs.values()]}


def _validate_materials(records, ctx, on_existing) -> tuple[list, list]:
    iss, plan, seen = _Issues(), [], {}
    T = lambda k: _col_title(MATERIAL_COLUMNS, k)       # noqa: E731
    for rn, r, frm in records:
        before = len(iss.items)
        for k, f in frm.items():
            iss.err(rn, T(k), f"الخلية تحتوي معادلة بلا قيمة محفوظة ({f[:30]}). افتح الملف في Excel واحفظه، أو الصق القيمة بدل المعادلة.")
        name, unit = text(r.get("name")), text(r.get("unit"))
        pu = text(r.get("purchase_unit"))
        wname = text(r.get("warehouse"))
        if not name:
            iss.err(rn, T("name"), "اسم الصنف مطلوب.")
        elif len(name) > 100:
            iss.err(rn, T("name"), "اسم الصنف أطول من 100 حرف.")
        if not unit:
            iss.err(rn, T("unit"), "وحدة الاستهلاك مطلوبة.")
        elif len(unit) > 30:
            iss.err(rn, T("unit"), "اسم الوحدة أطول من 30 حرفًا.")

        def num(key, minimum=0.0, strictly_positive=False):
            try:
                v = to_number(r.get(key))
            except ValueError:
                iss.err(rn, T(key), f"قيمة غير رقمية «{text(r.get(key))}».")
                return None
            if v is None:
                return None
            if v < minimum or (strictly_positive and v <= 0):
                iss.err(rn, T(key), "يجب أن تكون القيمة " + ("أكبر من صفر." if strictly_positive else "صفرًا أو أكبر."))
                return None
            if v > 1e9:
                iss.err(rn, T(key), "قيمة كبيرة بشكل غير منطقي.")
                return None
            return v
        factor, min_qty = num("factor", strictly_positive=True), num("min_qty")
        qty, cost = num("opening_qty"), num("unit_cost")
        if pu and norm(pu) == norm(unit):
            iss.warn(rn, T("purchase_unit"), "وحدة الشراء مطابقة لوحدة الاستهلاك فتم تجاهلها.")
            pu, factor = "", None
        if pu and factor is None and not any(i.row == rn and i.column == T("factor") for i in iss.items):
            iss.err(rn, T("factor"), f"معامل التحويل مطلوب عند تحديد وحدة الشراء «{pu}».")
        if not pu and factor not in (None, 1.0):
            iss.warn(rn, T("factor"), "تم تجاهل معامل التحويل لعدم وجود وحدة شراء.")
        wh_id, wh_name = ctx["default_wh"], None
        if wname:
            hit = ctx["whs"].get(norm(wname))
            if not hit:
                iss.err(rn, T("warehouse"), f"المخزن «{wname}» غير موجود. المتاح: {'، '.join(ctx['wh_names'])}.")
            else:
                wh_id, wh_name = hit
        if qty and qty > 0:
            if cost is None or cost <= 0:
                iss.err(rn, T("unit_cost"), "تكلفة الوحدة مطلوبة (وأكبر من صفر) عند وجود رصيد افتتاحي، حتى لا تظهر الوجبات بتكلفة صفرية.")
            if unit and unit.lower() in {u.lower() for u in inv.DISCRETE_UNITS} and abs(qty - round(qty)) > 1e-6:
                iss.err(rn, T("opening_qty"), f"الرصيد {qty:g} كسري لصنف بوحدة غير قابلة للتجزئة ({unit}).")
        if name:
            key = norm(name)
            if key in seen:
                iss.err(rn, T("name"), f"الصنف «{name}» مكرر (ظهر أول مرة في الصف {seen[key]}).")
            else:
                seen[key] = rn
            action = "create"
            if key in ctx["items"]:
                if on_existing == "skip":
                    action = "skip"
                    iss.warn(rn, T("name"), f"الصنف «{name}» موجود فتم تخطيه.")
                else:
                    iss.err(rn, T("name"), f"الصنف «{name}» موجود بالفعل بالمخزن. فعّل «تخطي الأصناف الموجودة» أو احذف الصف.")
        if len(iss.items) == before or all(i.level == "warning" for i in iss.items[before:]):
            plan.append({"row": rn, "name": name, "unit": unit, "purchase_unit": pu, "factor": factor if pu else None,
                         "min_qty": min_qty or 0.0, "wh_id": wh_id, "wh_name": wh_name or "", "opening_qty": qty or 0.0,
                         "unit_cost": cost, "action": action})
    return plan, iss.items


def _write_materials(c, cid, plan, user, opening_date, batch_id) -> float:
    total = 0.0
    for p in plan:
        if p["action"] != "create":
            continue
        iid = c.execute("INSERT INTO items(client_id,name,unit,is_semi,min_qty,created_by) VALUES(?,?,?,0,?,?)",
                        (cid, p["name"], p["unit"], p["min_qty"], user)).lastrowid
        if p["purchase_unit"]:
            c.execute("INSERT INTO item_units(item_id,unit_name,factor,is_purchase_default) VALUES(?,?,?,1)",
                      (iid, p["purchase_unit"], inv.rnd(p["factor"])))
        if p["unit_cost"] is not None:
            c.execute("UPDATE items SET last_price=? WHERE id=?", (inv.rnd(p["unit_cost"]), iid))
        if p["opening_qty"] > 0:
            qty, cost = inv.rnd(p["opening_qty"]), inv.rnd(p["unit_cost"])
            inv.adjust(c, cid, iid, p["wh_id"], qty)
            c.execute("UPDATE items SET avg_cost=?, last_price=? WHERE id=?", (cost, cost, iid))
            inv.move(c, cid, iid, p["wh_id"], opening_date, "opening", qty, cost, batch_id, user)
            c.execute("INSERT INTO price_history(item_id,price_date,unit_price) VALUES(?,?,?)", (iid, str(opening_date), cost))
            total += qty * cost
        log(c, user, "create", "item", iid, f"{p['name']} (استيراد جماعي #{batch_id})", p["opening_qty"] * (p["unit_cost"] or 0), cid)
    return total


# ================================================================= الوصفات (BOM)
def _recipe_ctx(conn, cid):
    items = {}
    for i, n, u, s in conn.execute("SELECT id, name, unit, is_semi FROM items WHERE client_id=?", (cid,)).fetchall():
        items[norm(n)] = {"id": i, "name": n, "unit": u, "semi": bool(s), "units": {norm(u): u}}
    by_id = {v["id"]: v for v in items.values()}
    for iid, un in conn.execute("SELECT u.item_id, u.unit_name FROM item_units u JOIN items i ON i.id=u.item_id WHERE i.client_id=?", (cid,)).fetchall():
        if iid in by_id:
            by_id[iid]["units"][norm(un)] = un
    dishes = {norm(n) for (n,) in conn.execute("SELECT name FROM dishes WHERE client_id=?", (cid,)).fetchall()}
    semi_with_recipe = {i for (i,) in conn.execute("SELECT output_item_id FROM recipes WHERE client_id=? AND kind='semi' AND output_item_id IS NOT NULL", (cid,)).fetchall()}
    return {"items": items, "dishes": dishes, "semi_with_recipe": semi_with_recipe}


def _validate_recipes(records, ctx, on_existing):
    iss = _Issues()
    T = lambda k: _col_title(RECIPE_COLUMNS, k)         # noqa: E731
    groups: "OrderedDict[str, dict]" = OrderedDict()
    # ---- المرحلة 1: تجميع الصفوف حسب الوصفة والتحقق من خصائص الوصفة
    for rn, r, frm in records:
        for k, f in frm.items():
            iss.err(rn, T(k), f"الخلية تحتوي معادلة بلا قيمة محفوظة ({f[:30]}). افتح الملف في Excel واحفظه، أو الصق القيمة بدل المعادلة.")
        name = text(r.get("recipe"))
        if not name:
            iss.err(rn, T("recipe"), "اسم الوصفة مطلوب.")
            continue
        g = groups.setdefault(norm(name), {"name": name, "first": rn, "kind": None, "category": "", "price": None,
                                           "out_unit": "", "yield": None, "rows": []})
        kraw = text(r.get("kind"))
        if kraw:
            k = KINDS.get(norm(kraw))
            if not k:
                iss.err(rn, T("kind"), f"نوع غير معروف «{kraw}». المسموح: وجبة، نصف مصنع.")
            elif g["kind"] and g["kind"] != k:
                iss.err(rn, T("kind"), f"نوع الوصفة «{name}» يختلف عن الصف {g['first']}.")
            else:
                g["kind"] = g["kind"] or k
        elif g["kind"] is None and g["first"] == rn:
            iss.err(rn, T("kind"), "نوع الوصفة مطلوب (وجبة أو نصف مصنع).")
        for key, parse in (("price", True), ("yield", True)):
            try:
                v = to_number(r.get(key))
            except ValueError:
                iss.err(rn, T(key), f"قيمة غير رقمية «{text(r.get(key))}».")
                continue
            if v is not None:
                if v <= 0:
                    iss.err(rn, T(key), "يجب أن تكون القيمة أكبر من صفر.")
                elif g[key] is not None and abs(g[key] - v) > 1e-9:
                    iss.err(rn, T(key), f"قيمة «{T(key)}» تختلف عن الصف {g['first']} لنفس الوصفة.")
                else:
                    g[key] = g[key] if g[key] is not None else v
        for key in ("category", "out_unit"):
            v = text(r.get(key))
            if v:
                if g[key] and norm(g[key]) != norm(v):
                    iss.err(rn, T(key), f"قيمة «{T(key)}» تختلف عن الصف {g['first']} لنفس الوصفة.")
                else:
                    g[key] = g[key] or v
        g["rows"].append((rn, r))
    # ---- المرحلة 2: تحقق كل وصفة وحل المكونات
    new_semis = {k: g for k, g in groups.items() if g["kind"] == "semi"}
    plan = []
    for key, g in groups.items():
        errs_before = sum(1 for i in iss.items if i.level == "error")
        name, kind = g["name"], g["kind"]
        action = "create"
        if kind == "dish":
            if g["price"] is None:
                iss.err(g["first"], T("price"), f"سعر البيع مطلوب للوجبة «{name}».")
            if key in ctx["dishes"]:
                action = "skip" if on_existing == "skip" else "exists"
        elif kind == "semi":
            if not g["out_unit"]:
                iss.err(g["first"], T("out_unit"), f"وحدة الناتج مطلوبة للمنتج النصف مصنع «{name}».")
            if g["yield"] is None:
                iss.err(g["first"], T("yield"), f"كمية ناتج التشغيلة مطلوبة للمنتج النصف مصنع «{name}».")
            ex = ctx["items"].get(key)
            if ex and not ex["semi"]:
                iss.err(g["first"], T("recipe"), f"يوجد صنف خام باسم «{name}». اختر اسمًا مختلفًا للمنتج النصف مصنع.")
            elif ex and ex["id"] in ctx["semi_with_recipe"]:
                action = "skip" if on_existing == "skip" else "exists"
            elif ex and g["out_unit"] and norm(ex["unit"]) != norm(g["out_unit"]):
                iss.err(g["first"], T("out_unit"), f"وحدة «{name}» الحالية «{ex['unit']}» تختلف عن «{g['out_unit']}».")
        if action == "exists":
            iss.err(g["first"], T("recipe"), f"الوصفة «{name}» موجودة بالفعل. فعّل «تخطي الموجود» أو احذفها من الملف.")
        elif action == "skip":
            iss.warn(g["first"], T("recipe"), f"الوصفة «{name}» موجودة فتم تخطيها.")
        lines, seen_ing = [], {}
        if action == "create":
            for rn, r in g["rows"]:
                ing = text(r.get("ingredient"))
                if not ing:
                    iss.err(rn, T("ingredient"), "المكوّن مطلوب.")
                    continue
                try:
                    q = to_number(r.get("qty"))
                except ValueError:
                    iss.err(rn, T("qty"), f"قيمة غير رقمية «{text(r.get('qty'))}».")
                    continue
                if q is None or q <= 0:
                    iss.err(rn, T("qty"), "الكمية مطلوبة ويجب أن تكون أكبر من صفر.")
                    continue
                ik = norm(ing)
                if ik == key and kind == "semi":
                    iss.err(rn, T("ingredient"), f"«{name}» لا يمكن أن يكون أحد مكونات نفسه.")
                    continue
                unit_in = text(r.get("unit"))
                db_item = ctx["items"].get(ik)
                if db_item:
                    units = db_item["units"]
                    chosen_id, base_unit = db_item["id"], db_item["unit"]
                elif ik in new_semis and new_semis[ik]["kind"] == "semi":
                    base_unit = new_semis[ik]["out_unit"]
                    units = {norm(base_unit): base_unit} if base_unit else {}
                    chosen_id = None
                else:
                    iss.err(rn, T("ingredient"), f"المكوّن «{ing}» غير موجود (لا في أصناف المخزن ولا ضمن المنتجات النصف مصنعة في هذا الملف).")
                    continue
                if unit_in and norm(unit_in) not in units:
                    iss.err(rn, T("unit"), f"الوحدة «{unit_in}» غير معرّفة للصنف «{ing}». المعرّف: {'، '.join(units.values()) or '—'}.")
                    continue
                if ik in seen_ing:
                    iss.warn(rn, T("ingredient"), f"المكوّن «{ing}» مكرر في نفس الوصفة (الصف {seen_ing[ik]}).")
                seen_ing.setdefault(ik, rn)
                lines.append({"row": rn, "ing_key": ik, "ing": ing, "qty": q, "unit": units.get(norm(unit_in)) if unit_in else None,
                              "item_id": chosen_id})
        ok = sum(1 for i in iss.items if i.level == "error") == errs_before
        plan.append({"row": g["first"], "name": name, "kind": kind, "category": g["category"], "price": g["price"],
                     "out_unit": g["out_unit"], "yield": g["yield"], "lines": lines, "action": action,
                     "key": key, "valid": ok})
    # ---- دورات بين المنتجات النصف مصنعة الجديدة (أ تحتاج ب وب تحتاج أ)
    graph = {p["key"]: {l["ing_key"] for l in p["lines"] if l["ing_key"] in new_semis} for p in plan if p["kind"] == "semi" and p["action"] == "create"}
    state = {}

    def dfs(n, path):
        state[n] = 1
        for m in graph.get(n, ()):
            if state.get(m) == 1:
                cyc = " ← ".join(new_semis[x]["name"] for x in path[path.index(m):] + [m]) if m in path else new_semis[m]["name"]
                iss.err(new_semis[n]["first"], T("ingredient"), f"دورة مغلقة بين المنتجات النصف مصنعة: {cyc}.")
            elif state.get(m) is None:
                dfs(m, path + [m])
        state[n] = 2
    for n in list(graph):
        if state.get(n) is None:
            dfs(n, [n])
    return plan, iss.items


def _write_recipes(c, cid, plan, user, ctx):
    created = 0
    semis = [p for p in plan if p["kind"] == "semi" and p["action"] == "create"]
    dishes = [p for p in plan if p["kind"] == "dish" and p["action"] == "create"]
    for p in semis:                                   # أصناف المخرجات أولًا لتُحَل المراجع المتبادلة
        if not c.execute("SELECT 1 FROM items WHERE client_id=? AND name=?", (cid, p["name"])).fetchone():
            c.execute("INSERT INTO items(client_id,name,unit,is_semi,created_by) VALUES(?,?,?,1,?)", (cid, p["name"], p["out_unit"], user))
    ids = {norm(n): i for i, n in c.execute("SELECT id, name FROM items WHERE client_id=?", (cid,)).fetchall()}

    def lines(p):
        return [(l["item_id"] or ids[l["ing_key"]], l["qty"], l["unit"]) for l in p["lines"]]
    for p in semis:
        services._create_semi_c(c, cid, p["name"], p["out_unit"], p["yield"], lines(p), user)
        created += 1
    for p in dishes:
        services._create_dish_c(c, cid, p["name"], p["category"], p["price"], lines(p), user)
        created += 1
    return created


# ================================================================= الواجهات العامة
def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prepare(kind, data, filename, conn, cid, on_existing):
    columns, aliases = (MATERIAL_COLUMNS, MATERIAL_ALIASES) if kind == "materials" else (RECIPE_COLUMNS, RECIPE_ALIASES)
    headers, rows = read_table(data, filename)
    mapping, unknown = _map_headers(headers, columns, aliases)
    records = _records(rows, mapping)
    rep = ImportReport(kind=kind, filename=filename, rows_total=len(records), file_sha=_hash(data))
    for h in unknown:
        rep.issues.append(Issue(None, h, "عمود غير معروف وسيتم تجاهله.", "warning"))
    if not records:
        raise ImportFileError("لا توجد صفوف بيانات (الصف المسبوق بكلمة «مثال» لا يُستورد).")
    if kind == "materials":
        rep.plan, issues = _validate_materials(records, _material_ctx(conn, cid), on_existing)
    else:
        rep.plan, issues = _validate_recipes(records, _recipe_ctx(conn, cid), on_existing)
    rep.issues += issues
    dup = conn.execute("SELECT created_at FROM import_batches WHERE client_id=? AND kind=? AND file_sha256=? ORDER BY id DESC LIMIT 1",
                       (cid, kind, rep.file_sha)).fetchone()
    if dup:
        rep.issues.append(Issue(None, "الملف", f"تم استيراد هذا الملف نفسه من قبل بتاريخ {dup[0]}.", "warning"))
    rep.created = sum(1 for p in rep.plan if p["action"] == "create")
    rep.skipped = sum(1 for p in rep.plan if p["action"] == "skip")
    return rep


def _preview(kind, cid, data, filename, on_existing):
    conn = get_conn()
    try:
        return _prepare(kind, data, filename, conn, cid, on_existing)
    finally:
        conn.close()


@guard("import")
def preview_materials(cid, data, filename, user, on_existing="error"):
    """تحقق بدون كتابة: يرجع ImportReport بكل الأخطاء والتنبيهات وخطة الإدخال."""
    return _preview("materials", cid, data, filename, on_existing)


@guard("import")
def preview_recipes(cid, data, filename, user, on_existing="error"):
    return _preview("recipes", cid, data, filename, on_existing)


def _commit(kind, cid, data, filename, user, on_existing, opening_date=None):
    with tx() as c:                                   # التحقق والكتابة داخل نفس القفل: لا سباقات
        rep = _prepare(kind, data, filename, c, cid, on_existing)
        if rep.error_count:
            raise ImportValidationError(rep)
        batch = c.execute("INSERT INTO import_batches(client_id,kind,filename,file_sha256,rows_total,created_by,created_at) "
                          "VALUES(?,?,?,?,?,?,datetime('now','localtime'))",
                          (cid, kind, (filename or "")[:200], rep.file_sha, rep.rows_total, user)).lastrowid
        if kind == "materials":
            rep.total_value = _write_materials(c, cid, rep.plan, user, opening_date or date.today(), batch)
        else:
            _write_recipes(c, cid, rep.plan, user, None)
        rep.batch_id = batch
        summary = f"{rep.created} جديد، {rep.skipped} متخطى" + (f"، قيمة الرصيد الافتتاحي {rep.total_value:,.2f}" if kind == "materials" else "")
        c.execute("UPDATE import_batches SET created_count=?, skipped_count=?, summary=? WHERE id=?", (rep.created, rep.skipped, summary, batch))
        log(c, user, "import", "import", batch, f"{'خامات' if kind == 'materials' else 'وصفات'} من «{filename}»: {summary}",
            rep.total_value or None, cid)
    return rep


@guard("import")
def import_materials(cid, data, filename, user, on_existing="error", opening_date=None):
    """يستورد الخامات والرصيد الافتتاحي في معاملة واحدة. أي خطأ = لا شيء يُكتب."""
    return _commit("materials", cid, data, filename, user, on_existing, opening_date)


@guard("import")
def import_recipes(cid, data, filename, user, on_existing="error"):
    return _commit("recipes", cid, data, filename, user, on_existing)


def history(cid) -> pd.DataFrame:
    return query("""SELECT id, created_at, kind, filename, rows_total, created_count, skipped_count, summary, created_by
                    FROM import_batches WHERE client_id=? ORDER BY id DESC""", (cid,))


# ================================================================= القوالب
def _template(columns, sheet_title, legend_title, extra_rules, warehouses=None, kind_validation=False) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.sheet_view.rightToLeft = True
    arial = lambda **k: Font(name="Arial", size=k.pop("size", 10), **k)         # noqa: E731
    req_fill, opt_fill = PatternFill("solid", fgColor="C8A24A"), PatternFill("solid", fgColor="0A1F44")
    for j, (key, title, req, example, note) in enumerate(columns, 1):
        h = ws.cell(row=1, column=j, value=title)
        h.font = arial(bold=True, color="FFFFFF" if not req else "0A1F44")
        h.fill = req_fill if req else opt_fill
        h.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        h.comment = Comment(("مطلوب. " if req else "اختياري. ") + note, "Sanad")
        ex = ws.cell(row=2, column=j, value=example if example != "" else None)
        ex.font = arial(italic=True, color="888888")
        ws.column_dimensions[get_column_letter(j)].width = max(16, len(title) * 1.5 + 4)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    if kind_validation:
        dv = DataValidation(type="list", formula1='"وجبة,نصف مصنع"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add("B3:B5000")
    leg = wb.create_sheet("تعليمات")
    leg.sheet_view.rightToLeft = True
    leg.column_dimensions["A"].width = 28
    leg.column_dimensions["B"].width = 12
    leg.column_dimensions["C"].width = 90
    leg["A1"] = legend_title
    leg["A1"].font = arial(size=14, bold=True, color="0A1F44")
    leg["A2"] = ("اكتب بياناتك في ورقة «" + sheet_title + "» بدءًا من الصف 3 (الصف 2 مثال يبدأ بكلمة «مثال» ولا يُستورد؛ احذفه أو اتركه). "
                 "لا تغيّر عناوين الأعمدة (الصف 1). العنوان الذهبي = مطلوب، الكحلي = اختياري.")
    leg["A2"].font = arial(color="555555")
    for j, h in enumerate(["العمود", "مطلوب؟", "الشرح"], 1):
        c = leg.cell(row=4, column=j, value=h)
        c.font, c.fill = arial(bold=True, color="FFFFFF"), opt_fill
    for i, (key, title, req, example, note) in enumerate(columns, start=5):
        leg.cell(row=i, column=1, value=title).font = arial(bold=True)
        leg.cell(row=i, column=2, value="نعم" if req else "لا").font = arial()
        leg.cell(row=i, column=3, value=note).font = arial()
    r = 5 + len(columns) + 1
    leg.cell(row=r, column=1, value="قواعد مهمة").font = arial(bold=True, color="0A1F44")
    for k, rule in enumerate(extra_rules, start=r + 1):
        leg.cell(row=k, column=1, value="• " + rule).font = arial()
    if warehouses:
        leg.cell(row=r + len(extra_rules) + 2, column=1, value="المخازن المتاحة لهذا المطعم: " + "، ".join(warehouses)).font = arial(bold=True)
    wb.properties.title = sheet_title
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template_materials(warehouses=None) -> bytes:
    return _template(MATERIAL_COLUMNS, "الخامات", "قالب استيراد الخامات والرصيد الافتتاحي", [
        "كل الكميات والتكاليف بوحدة الاستهلاك (وليس وحدة الشراء). مثال: شيكارة 50 كجم، الرصيد 120 كجم، التكلفة 20.5 للكجم.",
        "إن حددت وحدة شراء فمعامل التحويل مطلوب (1 وحدة شراء = كم وحدة استهلاك).",
        "إن وُجد رصيد افتتاحي فتكلفة الوحدة مطلوبة، وتصبح متوسط التكلفة المرجح (WAC) المبدئي.",
        "الأصناف المكررة أو الموجودة بالمخزن تُرفض (أو تُتخطى بخيار «تخطي الموجود»).",
        "أي خطأ في أي صف يُلغي الاستيراد بالكامل ويُعرض رقم الصف والعمود.",
        "الوحدات التي لا تقبل الكسور (قطعة، علبة، كيس...) لا تقبل رصيدًا كسريًا.",
        "الصيغ المقبولة: .xlsx أو .csv (ترميز UTF-8). الأرقام العربية ٠-٩ مقبولة."], warehouses)


def template_recipes() -> bytes:
    return _template(RECIPE_COLUMNS, "الوصفات", "قالب استيراد الوصفات (مكونات الوجبات والمنتجات النصف مصنعة)", [
        "كل سطر = مكوّن واحد. كرّر اسم الوصفة ونوعها في كل سطر من مكوناتها.",
        "الوجبة تحتاج «سعر البيع». النصف مصنع يحتاج «وحدة الناتج» و«كمية ناتج التشغيلة» (تكفي في أول سطر).",
        "المكوّن يجب أن يكون صنفًا موجودًا بالمخزن، أو منتجًا نصف مصنع معرّفًا في نفس الملف.",
        "الوحدة اختيارية: فارغة = وحدة الاستهلاك، أو اكتب أي وحدة معرّفة للصنف (كرتونة...) وتُحوَّل تلقائيًا.",
        "استورد الخامات أولًا (القالب الآخر) ثم الوصفات.",
        "أي خطأ في أي صف يُلغي استيراد الملف بالكامل ويُعرض رقم الصف والعمود."], kind_validation=True)
