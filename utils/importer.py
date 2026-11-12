"""الاستيراد الجماعي من Excel/CSV: قوالب جاهزة، تحقق دقيق بأرقام الصفوف، وإدخال ذري (كل شيء أو لا شيء).

أنواع القوالب:
- items    : بيانات الأصناف الأساسية (الخامات) + رصيد افتتاحي اختياري بدفعات لكل تاريخ صلاحية.
- stock    : الرصيد الافتتاحي / الجرد الفعلي لأصناف موجودة، صف لكل دفعة (صنف، مخزن، تاريخ صلاحية).
- recipes  : الوصفات (مكونات الوجبات).
- products : المنتجات المصنّعة (النصف مصنعة) ومكوناتها + رصيد افتتاحي اختياري بتاريخ صلاحية.

المراحل: (1) قراءة آمنة للملف (حجم/صيغة/ضغط/عدد صفوف)  (2) تحقق كامل قبل أي كتابة يجمع كل الأخطاء برقم الصف
(3) إن وُجد خطأ واحد لا يُكتب شيء  (4) الكتابة كلها في معاملة واحدة، وأي استثناء يلغيها بالكامل.
أرقام الصفوف في التقارير = أرقام الصفوف في Excel (الصف 1 هو العناوين).
كل كمية تدخل المخزن تُنشأ لها دفعة بتاريخ صلاحيتها (أو بدون تاريخ) عبر inventory.adjust — نفس منطق المشتريات.
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
from datetime import date, datetime, timedelta

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from database.db import get_conn, tx, query
from utils import inventory as inv, services, controls, sequences
from utils.audit import log
from utils.security import guard, _require_admin

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED = 60 * 1024 * 1024
MAX_ROWS = 5000
EXAMPLE_PREFIX = "مثال"
DATE_HINT = "YYYY-MM-DD"
TEMPLATE_ROWS = 2000                      # عدد الصفوف المنسّقة مسبقًا في القالب (تاريخ/نص/قوائم)

# ----------------------------------------------------------------- تعريف الأعمدة
# (المفتاح، العنوان في القالب، مطلوب في العناوين؟، مثال، شرح)
COL_BARCODE = ("barcode", "الباركود", False, "6221234567890", "اختياري. باركود الصنف كما على العبوة (يُحفظ كنص فلا تضيع الأصفار في البداية). غير مكرر.")
COL_EXPIRY = ("expiry", f"تاريخ الصلاحية ({DATE_HINT})", False, "2027-03-31", f"تاريخ انتهاء هذه الدفعة بصيغة {DATE_HINT} فقط (مثل 2026-03-31). "
              "إلزامي للأصناف التي تتتبع الصلاحية. كل تاريخ = دفعة مستقلة في المخزن.")
ITEM_COLUMNS = [
    ("name", "اسم الصنف", True, "مثال: دقيق فاخر", "اسم الخامة (غير مكرر). لإدخال أكثر من دفعة لنفس الصنف كرّر الاسم في صف جديد بكمية وتاريخ صلاحية آخر."),
    COL_BARCODE,
    ("unit", "وحدة الاستهلاك", True, "كجم", "الوحدة التي تُستخدم بها الخامة في الوصفات وتُحسب بها التكلفة والكميات (جرام، مل، قطعة، كجم...)."),
    ("purchase_unit", "وحدة الشراء", False, "شيكارة", "الوحدة التي تشتري بها (كرتونة، شيكارة، زجاجة...). اتركها فارغة إن كانت نفس وحدة الاستهلاك."),
    ("factor", "معامل التحويل", False, 50, "كم وحدة استهلاك في وحدة شراء واحدة (1 شيكارة = 50 كجم). مطلوب عند تحديد وحدة الشراء."),
    ("min_qty", "حد إعادة الطلب", False, 25, "بوحدة الاستهلاك. يظهر تنبيه عند الوصول إليه."),
    ("track_expiry", "يتتبع الصلاحية (نعم/لا)", False, "نعم", "«نعم» يجعل تاريخ الصلاحية إلزاميًا لهذا الصنف هنا وفي كل فاتورة شراء."),
    ("warehouse", "المخزن", False, "المخزن الرئيسي", "مخزن الرصيد الافتتاحي (اسم مخزن قائم). فارغ = المخزن الرئيسي."),
    ("opening_qty", "الكمية (رصيد افتتاحي)", False, 120, "الرصيد الافتتاحي لهذه الدفعة بوحدة الاستهلاك. فارغ = بدون رصيد."),
    ("unit_cost", "متوسط التكلفة (لوحدة الاستهلاك)", False, 20.5, "تكلفة وحدة الاستهلاك الواحدة. مطلوبة عند وجود كمية، وتصبح متوسط التكلفة المرجح (WAC) المبدئي."),
    COL_EXPIRY,
]
ITEM_ALIASES = {
    "name": ["الصنف", "اسم الخامة", "name", "item", "item_name"],
    "barcode": ["باركود", "barcode", "ean", "sku", "كود"],
    "unit": ["وحدة الاستهلاك (الاساسيه)", "الوحدة الاساسية", "الوحدة", "unit", "base_unit", "consumption_unit"],
    "purchase_unit": ["purchase_unit"],
    "factor": ["معامل التحويل (1 وحدة شراء = كم وحدة استهلاك)", "factor", "conversion_factor"],
    "min_qty": ["min_qty", "reorder_level", "حد الطلب"],
    "track_expiry": ["يتتبع الصلاحية", "تتبع الصلاحية", "track_expiry"],
    "warehouse": ["warehouse", "اسم المخزن"],
    "opening_qty": ["الرصيد الافتتاحي", "الرصيد الافتتاحي (بوحدة الاستهلاك)", "رصيد افتتاحي", "opening_qty", "opening_balance", "الكمية", "quantity", "qty"],
    "unit_cost": ["تكلفة الوحدة (wac)", "تكلفة وحدة الاستهلاك", "تكلفة الوحدة", "متوسط التكلفة", "wac", "unit_cost", "cost", "avg_cost", "التكلفة"],
    "expiry": ["تاريخ الصلاحية", "الصلاحية", "تاريخ الانتهاء", "expiry", "expiry_date", "exp"],
}
STOCK_COLUMNS = [
    ("name", "اسم الصنف", False, "مثال: دقيق فاخر", "اسم صنف موجود (خامة أو منتج مصنّع). يكفي الاسم أو الباركود."),
    ("barcode", "الباركود", False, "6221234567890", "بديل عن الاسم لتحديد الصنف. إن كُتبا معًا يجب أن يخصا نفس الصنف."),
    ("unit", "وحدة الاستهلاك", False, "كجم", "وحدة الكمية. فارغة = وحدة الاستهلاك، أو اكتب أي وحدة معرّفة للصنف (كرتونة...) وتُحوَّل تلقائيًا."),
    ("warehouse", "المخزن", False, "المخزن الرئيسي", "اسم مخزن قائم. فارغ = المخزن الرئيسي."),
    ("qty", "الكمية", True, 40, "كمية هذه الدفعة. في وضع الجرد: الكمية الفعلية المعدودة (صفر مسموح)."),
    ("unit_cost", "متوسط التكلفة (لوحدة الاستهلاك)", False, 20.5, "للرصيد الافتتاحي: تكلفة وحدة الاستهلاك (فارغة = متوسط التكلفة الحالي للصنف). "
     "في الجرد لا تغيّر التكلفة إلا لصنف بلا تكلفة."),
    COL_EXPIRY,
]
STOCK_ALIASES = {
    "name": ["الصنف", "اسم الخامة", "name", "item", "item_name"],
    "barcode": ITEM_ALIASES["barcode"],
    "unit": ["الوحدة", "unit", "consumption_unit"],
    "warehouse": ITEM_ALIASES["warehouse"],
    "qty": ["الكمية الفعلية", "الرصيد", "الرصيد الافتتاحي", "qty", "quantity", "count"],
    "unit_cost": ITEM_ALIASES["unit_cost"],
    "expiry": ITEM_ALIASES["expiry"],
}
# أعمدة مخفية من قالب الوصفات (تُقبل عند الرفع لتوافق ملفات القالب القديم المجمّع للوجبات والنصف مصنع)
HIDDEN_KEYS = {"recipes": {"kind", "out_unit", "yield"}}
RECIPE_COLUMNS = [
    ("recipe", "اسم الوصفة", True, "مثال: كريب دجاج", "اسم الوجبة. كرّره في كل سطر مكوّن."),
    ("barcode", "باركود الوجبة", False, "", "اختياري. باركود/كود الوجبة على الكاشير. غير مكرر."),
    ("category", "تصنيف الوجبة", False, "كريب", "اختياري."),
    ("price", "سعر البيع", False, 95, "مطلوب لكل وجبة (يكفي في أول سطر)."),
    ("ingredient", "المكوّن", True, "عجينة كريب", "اسم صنف موجود (خامة أو منتج مصنّع). يمكن تركه فارغًا إن كُتب باركود المكوّن."),
    ("ing_barcode", "باركود المكوّن", False, "", "بديل عن اسم المكوّن."),
    ("qty", "الكمية", True, 0.12, "كمية المكوّن في وجبة واحدة."),
    ("unit", "وحدة الاستهلاك", False, "كجم", "وحدة الكمية. فارغة = وحدة استهلاك المكوّن، أو أي وحدة معرّفة له وتُحوَّل تلقائيًا."),
    ("kind", "نوع الوصفة", False, "", ""),
    ("out_unit", "وحدة الناتج", False, "", ""),
    ("yield", "كمية ناتج التشغيلة", False, "", ""),
]
PRODUCT_COLUMNS = [
    ("recipe", "اسم المنتج المصنّع", True, "مثال: صوص الشيدر", "المنتج النصف مصنع. كرّره في كل سطر مكوّن."),
    ("barcode", "الباركود", False, "", "اختياري. باركود المنتج. غير مكرر."),
    ("out_unit", "وحدة الاستهلاك (وحدة الناتج)", True, "كجم", "وحدة المنتج التي يُستهلك بها في الوجبات وتُقاس بها التشغيلة."),
    ("yield", "كمية ناتج التشغيلة", True, 5, "كم وحدة تنتج التشغيلة الواحدة (يكفي في أول سطر)."),
    ("ingredient", "المكوّن", True, "جبنة شيدر", "اسم صنف موجود، أو منتج مصنّع آخر معرّف في نفس الملف."),
    ("ing_barcode", "باركود المكوّن", False, "", "بديل عن اسم المكوّن."),
    ("qty", "الكمية", True, 2, "كمية المكوّن في تشغيلة واحدة."),
    ("unit", "وحدة المكوّن", False, "كجم", "فارغة = وحدة استهلاك المكوّن."),
    ("warehouse", "المخزن", False, "", "مخزن الرصيد الافتتاحي للمنتج. فارغ = مخزن التشغيل."),
    ("opening_qty", "الكمية (رصيد افتتاحي)", False, 3, "رصيد المنتج الموجود حاليًا بوحدة الاستهلاك (يكفي في أول سطر). فارغ = بدون رصيد."),
    ("unit_cost", "متوسط التكلفة (لوحدة الاستهلاك)", False, 180, "مطلوبة عند وجود رصيد افتتاحي."),
    COL_EXPIRY,
]
RECIPE_ALIASES = {
    "recipe": ["الوصفة", "اسم الوجبة", "الوجبة", "المنتج", "اسم المنتج", "recipe", "dish", "product", "name"],
    "barcode": ["باركود", "barcode", "كود الوجبة", "باركود المنتج"],
    "kind": ["النوع", "نوع", "kind", "type"],
    "category": ["التصنيف", "category"],
    "price": ["السعر", "سعر", "price"],
    "out_unit": ["وحدة الناتج", "وحدة الانتاج", "out_unit", "output_unit"],
    "yield": ["ناتج التشغيلة", "كمية الناتج", "yield", "yield_qty"],
    "ingredient": ["المكون", "الصنف", "ingredient", "item"],
    "ing_barcode": ["ingredient_barcode", "باركود الصنف"],
    "qty": ["كمية", "qty", "quantity"],
    "unit": ["الوحدة", "unit", "وحدة الكمية", "وحدة الاستهلاك"],
    "warehouse": ITEM_ALIASES["warehouse"],
    "opening_qty": ITEM_ALIASES["opening_qty"],
    "unit_cost": ITEM_ALIASES["unit_cost"],
    "expiry": ITEM_ALIASES["expiry"],
}
# نوع كل عمود في القالب: تنسيق الخلايا + التحقق داخل Excel
COL_TYPES = {"barcode": "text", "ing_barcode": "text", "expiry": "date", "track_expiry": "bool", "warehouse": "wh",
             "qty": "number", "opening_qty": "number", "unit_cost": "number", "factor": "number", "min_qty": "number",
             "price": "number", "yield": "number"}
KINDS = {"وجبه": "dish", "dish": "dish", "meal": "dish", "نصف مصنع": "semi", "نصف مصنّع": "semi", "semi": "semi",
         "وصفه فرعيه": "semi", "منتج نصف مصنع": "semi", "منتج مصنع": "semi", "sub": "semi"}
KIND_AR = {"items": "بيانات الأصناف", "materials": "بيانات الأصناف", "stock": "الرصيد الافتتاحي / الجرد",
           "recipes": "الوصفات", "products": "المنتجات المصنّعة"}
_YES = {"نعم", "yes", "y", "true", "1", "✓", "صح", "x"}
_NO = {"لا", "no", "n", "false", "0", "×"}
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
    batches: int = 0                 # عدد الدفعات (تواريخ الصلاحية) التي ستدخل المخزن
    summary: list = field(default_factory=list)   # للجرد: مقارنة رصيد النظام بالمعدود لكل (صنف، مخزن)

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

    def errors_since(self, n) -> int:
        return sum(1 for i in self.items[n:] if i.level == "error")


# ----------------------------------------------------------------- مساعدات
def norm(s) -> str:
    s = unicodedata.normalize("NFKC", "" if s is None else str(s))
    s = re.sub("[ً-ٰٟـ]", "", s).translate(_AR)
    return re.sub(r"\s+", " ", s).strip().casefold()


def text(v, maxlen=None) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, datetime):
        v = v.date()
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
    s = str(v).strip().translate(_DIGITS).replace("٫", ".").replace("٬", ",").replace(" ", "").replace(" ", "")
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


def to_date(v):
    """تاريخ صلاحية: يقبل خلية تاريخ في Excel، أو نصًا بصيغة YYYY-MM-DD (والأرقام العربية). None للفارغ.

    يرفض صيغة يوم/شهر/سنة صراحة (لتفادي الخلط بين 03/04 و04/03) والتواريخ غير الموجودة في التقويم.
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        if not (f.is_integer() and 20000 <= f <= 80000):
            raise ValueError(f"«{text(v)}» رقم وليس تاريخًا. اكتب التاريخ بصيغة {DATE_HINT}.")
        d = date(1899, 12, 30) + timedelta(days=int(f))      # رقم تسلسلي لتاريخ Excel
    else:
        s = str(v).strip().translate(_DIGITS)
        s = re.split(r"[T ]", s, maxsplit=1)[0]
        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
        if not m:
            if re.fullmatch(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}", s):
                raise ValueError(f"«{s}» مكتوب بصيغة يوم/شهر/سنة. اكتبه بصيغة {DATE_HINT} (مثل 2026-03-31) لتفادي الخلط بين اليوم والشهر.")
            raise ValueError(f"«{s}» ليس تاريخًا بصيغة {DATE_HINT}.")
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            raise ValueError(f"«{s}» تاريخ غير موجود في التقويم.")
    if d.year < 2000 or d.year > date.today().year + 30:
        raise ValueError(f"سنة غير منطقية في التاريخ «{d}».")
    return d


def to_barcode(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        raise ValueError("باركود غير صالح.")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError("باركود غير صالح (رقم عشري). نسّق العمود كنص في Excel.")
        v = int(v)
    s = str(v).strip().translate(_DIGITS).replace(" ", "")
    if not s:
        return ""
    if len(s) > 64 or not re.fullmatch(r"[0-9A-Za-z\-_.]+", s):
        raise ValueError(f"باركود غير صالح «{s}» (أرقام وحروف لاتينية و- _ . فقط، حتى 64 حرفًا).")
    return s


def to_bool(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return v
    s = norm(text(v))
    if s in {norm(x) for x in _YES}:
        return True
    if s in {norm(x) for x in _NO}:
        return False
    raise ValueError(f"«{text(v)}» — اكتب نعم أو لا.")


def _is_example(r: dict) -> bool:
    first = next((v for v in r.values() if v not in (None, "")), None)
    return isinstance(first, str) and first.strip().startswith(EXAMPLE_PREFIX)


class _Row:
    """قارئ قيم صف واحد يسجل الأخطاء برقم الصف واسم العمود."""

    def __init__(self, iss, rn, r, columns):
        self.iss, self.rn, self.r = iss, rn, r
        self.titles = {k: t for k, t, *_ in columns}

    def T(self, k):
        return self.titles.get(k, k)

    def text(self, k):
        return text(self.r.get(k))

    def num(self, k, minimum=0.0, strictly_positive=False):
        try:
            v = to_number(self.r.get(k))
        except ValueError:
            self.iss.err(self.rn, self.T(k), f"قيمة غير رقمية «{text(self.r.get(k))}».")
            return None
        if v is None:
            return None
        if v < minimum or (strictly_positive and v <= 0):
            self.iss.err(self.rn, self.T(k), "يجب أن تكون القيمة " + ("أكبر من صفر." if strictly_positive else "صفرًا أو أكبر."))
            return None
        if v > 1e9:
            self.iss.err(self.rn, self.T(k), "قيمة كبيرة بشكل غير منطقي.")
            return None
        return v

    def date(self, k):
        try:
            return to_date(self.r.get(k))
        except ValueError as e:
            self.iss.err(self.rn, self.T(k), str(e))
            return None

    def barcode(self, k="barcode"):
        try:
            return to_barcode(self.r.get(k))
        except ValueError as e:
            self.iss.err(self.rn, self.T(k), str(e))
            return ""

    def bool(self, k):
        try:
            return to_bool(self.r.get(k))
        except ValueError as e:
            self.iss.err(self.rn, self.T(k), str(e))
            return None

    def formulas(self, frm):
        for k, f in frm.items():
            self.iss.err(self.rn, self.T(k), f"الخلية تحتوي معادلة بلا قيمة محفوظة ({f[:30]}). افتح الملف في Excel واحفظه، أو الصق القيمة بدل المعادلة.")


def _expiry_checks(iss, rn, title, expiry, qty, track, name, ref_day):
    """قواعد تاريخ الصلاحية المشتركة لكل دفعة."""
    if qty and qty > 0:
        if track and expiry is None:
            iss.err(rn, title, f"تاريخ الصلاحية إلزامي لـ «{name}» (صنف يتتبع الصلاحية) بصيغة {DATE_HINT}.")
        elif expiry is not None and expiry < ref_day:
            iss.warn(rn, title, f"دفعة «{name}» منتهية الصلاحية ({expiry}). ستُسجَّل وتظهر في تنبيهات الصلاحية لتسجيلها كهدر.")
    elif expiry is not None:
        iss.warn(rn, title, "تم تجاهل تاريخ الصلاحية لعدم وجود كمية في هذا الصف.")


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
                if i > MAX_ROWS + TEMPLATE_ROWS + 5:
                    raise ImportFileError(f"عدد الصفوف يتجاوز الحد الأقصى ({MAX_ROWS}).")
            wb.close()
            # خلايا بمعادلة بلا قيمة محفوظة: تُكتشف ليظهر خطأ واضح بدل «فارغ» مضلل
            wb2 = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            for i, row in enumerate(wb2[wb2.sheetnames[0]].iter_rows(min_row=1, values_only=True), start=1):
                if i > len(raw_rows):
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


def _map_headers(headers, columns, aliases):
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
    return mapping, unknown, set(seen)


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


# ================================================================= سياق القاعدة
def _ctx(conn, cid):
    items, by_code = {}, {}
    for i, n, u, s, tr, bc, a, lp in conn.execute("SELECT id, name, unit, is_semi, COALESCE(track_expiry,0), barcode, avg_cost, last_price "
                                                  "FROM items WHERE client_id=?", (cid,)).fetchall():
        items[norm(n)] = {"id": i, "name": n, "unit": u, "semi": bool(s), "track": bool(tr), "barcode": bc or "",
                          "cost": inv.unit_cost(a, lp), "units": {norm(u): (u, 1.0)}}
        if bc:
            by_code[bc] = norm(n)
    by_id = {v["id"]: v for v in items.values()}
    for iid, un, f in conn.execute("SELECT u.item_id, u.unit_name, u.factor FROM item_units u JOIN items i ON i.id=u.item_id WHERE i.client_id=?",
                                   (cid,)).fetchall():
        if iid in by_id:
            by_id[iid]["units"][norm(un)] = (un, float(f))
    whs = {norm(n): (i, n) for i, n in conn.execute("SELECT id, name FROM warehouses WHERE client_id=? AND is_active=1", (cid,)).fetchall()}
    dishes = {norm(n) for (n,) in conn.execute("SELECT name FROM dishes WHERE client_id=?", (cid,)).fetchall()}
    dish_codes = {bc for (bc,) in conn.execute("SELECT barcode FROM dishes WHERE client_id=? AND barcode IS NOT NULL AND barcode<>''", (cid,)).fetchall()}
    semi_with_recipe = {i for (i,) in conn.execute("SELECT output_item_id FROM recipes WHERE client_id=? AND kind='semi' AND output_item_id IS NOT NULL",
                                                   (cid,)).fetchall()}
    return {"items": items, "by_code": by_code, "whs": whs, "wh_names": [n for _, n in whs.values()],
            "main_wh": inv.default_wh(conn, cid, "purchase"), "ops_wh": inv.default_wh(conn, cid, "ops"),
            "dishes": dishes, "dish_codes": dish_codes, "semi_with_recipe": semi_with_recipe}


def _wh(rw, ctx, key, default):
    wname = rw.text(key)
    if not wname:
        return default, next((n for i, n in ctx["whs"].values() if i == default), "")
    hit = ctx["whs"].get(norm(wname))
    if not hit:
        rw.iss.err(rw.rn, rw.T(key), f"المخزن «{wname}» غير موجود. المتاح: {'، '.join(ctx['wh_names'])}.")
        return None, wname
    return hit


# ================================================================= 1) بيانات الأصناف + دفعات افتتاحية
def _validate_items(records, ctx, on_existing, opening_date):
    iss, groups, codes = _Issues(), OrderedDict(), {}
    for rn, r, frm in records:
        rw = _Row(iss, rn, r, ITEM_COLUMNS)
        rw.formulas(frm)
        name = rw.text("name")
        if not name:
            iss.err(rn, rw.T("name"), "اسم الصنف مطلوب.")
            continue
        if len(name) > 100:
            iss.err(rn, rw.T("name"), "اسم الصنف أطول من 100 حرف.")
        key = norm(name)
        unit, pu, bc = rw.text("unit"), rw.text("purchase_unit"), rw.barcode()
        factor, min_qty, track = rw.num("factor", strictly_positive=True), rw.num("min_qty"), rw.bool("track_expiry")
        g = groups.get(key)
        if g is None:                                          # أول صف للصنف = بياناته الأساسية
            if not unit:
                iss.err(rn, rw.T("unit"), "وحدة الاستهلاك مطلوبة.")
            elif len(unit) > 30:
                iss.err(rn, rw.T("unit"), "اسم الوحدة أطول من 30 حرفًا.")
            if pu and norm(pu) == norm(unit):
                iss.warn(rn, rw.T("purchase_unit"), "وحدة الشراء مطابقة لوحدة الاستهلاك فتم تجاهلها.")
                pu, factor = "", None
            if pu and factor is None and not any(i.row == rn and i.column == rw.T("factor") for i in iss.items):
                iss.err(rn, rw.T("factor"), f"معامل التحويل مطلوب عند تحديد وحدة الشراء «{pu}».")
            if not pu and factor not in (None, 1.0):
                iss.warn(rn, rw.T("factor"), "تم تجاهل معامل التحويل لعدم وجود وحدة شراء.")
            action = "create"
            if key in ctx["items"]:
                if on_existing == "skip":
                    action = "skip"
                    iss.warn(rn, rw.T("name"), f"الصنف «{name}» موجود فتم تخطيه (لإضافة رصيد له استخدم قالب الرصيد الافتتاحي/الجرد).")
                else:
                    iss.err(rn, rw.T("name"), f"الصنف «{name}» موجود بالفعل. فعّل «تخطي الموجود»، أو أضف رصيده من قالب الرصيد الافتتاحي/الجرد.")
            if bc and action == "create":
                if bc in ctx["by_code"]:
                    iss.err(rn, rw.T("barcode"), f"الباركود {bc} مستخدم بالفعل للصنف «{ctx['items'][ctx['by_code'][bc]]['name']}».")
                elif bc in codes:
                    iss.err(rn, rw.T("barcode"), f"الباركود {bc} مكرر في الملف (الصف {codes[bc]}).")
                codes.setdefault(bc, rn)
            g = groups[key] = {"row": rn, "name": name, "unit": unit, "purchase_unit": pu, "factor": factor if pu else None,
                               "min_qty": min_qty or 0.0, "track": bool(track), "barcode": bc, "action": action,
                               "first_cost": None, "batches": []}
        else:                                                  # صف إضافي لنفس الصنف = دفعة أخرى
            for k, mine, theirs in (("unit", unit, g["unit"]), ("purchase_unit", pu, g["purchase_unit"]), ("barcode", bc, g["barcode"])):
                if mine and norm(mine) != norm(theirs):
                    iss.err(rn, rw.T(k), f"قيمة «{rw.T(k)}» تختلف عن الصف {g['row']} لنفس الصنف. اترك الأعمدة الأساسية فارغة في صفوف الدفعات الإضافية.")
            if track is not None and track != g["track"]:
                iss.err(rn, rw.T("track_expiry"), f"«{rw.T('track_expiry')}» يختلف عن الصف {g['row']} لنفس الصنف.")
        qty, cost, expiry = rw.num("opening_qty"), rw.num("unit_cost"), rw.date("expiry")
        wh_id, wh_name = _wh(rw, ctx, "warehouse", ctx["main_wh"])
        if g["row"] != rn and not qty:
            iss.err(rn, rw.T("opening_qty"), f"صف مكرر للصنف «{name}» (الصف {g['row']}) بدون كمية. الصفوف الإضافية للصنف تُستخدم لدفعات أخرى بكمية وتاريخ صلاحية.")
        if qty and qty > 0:
            if cost is None or cost <= 0:
                iss.err(rn, rw.T("unit_cost"), "متوسط التكلفة مطلوب (وأكبر من صفر) عند وجود كمية، حتى لا تظهر الوجبات بتكلفة صفرية.")
            if g["unit"] and g["unit"].lower() in {u.lower() for u in inv.DISCRETE_UNITS} and abs(qty - round(qty)) > 1e-6:
                iss.err(rn, rw.T("opening_qty"), f"الكمية {qty:g} كسرية لصنف بوحدة غير قابلة للتجزئة ({g['unit']}).")
            g["batches"].append({"row": rn, "qty": qty, "cost": cost, "expiry": expiry, "wh_id": wh_id, "wh_name": wh_name})
        elif cost is not None and g["first_cost"] is None:
            g["first_cost"] = cost
        _expiry_checks(iss, rn, rw.T("expiry"), expiry, qty, g["track"], name, opening_date)
    return list(groups.values()), iss.items


def _write_items(c, cid, plan, user, opening_date, batch_id) -> float:
    total = 0.0
    for p in plan:
        if p["action"] != "create":
            continue
        iid = c.execute("INSERT INTO items(client_id,name,unit,is_semi,min_qty,track_expiry,barcode,created_by) VALUES(?,?,?,0,?,?,?,?)",
                        (cid, p["name"], p["unit"], p["min_qty"], 1 if p["track"] else 0, p["barcode"] or None, user)).lastrowid
        if p["purchase_unit"]:
            c.execute("INSERT INTO item_units(item_id,unit_name,factor,is_purchase_default) VALUES(?,?,?,1)",
                      (iid, p["purchase_unit"], inv.rnd(p["factor"])))
        tq = tv = 0.0
        for b in p["batches"]:
            q, cost = inv.rnd(b["qty"]), inv.rnd(b["cost"])
            lots = inv.adjust(c, cid, iid, b["wh_id"], q, expiry=b["expiry"], unit_cost=cost, received=opening_date, source=("opening", batch_id))
            inv.move(c, cid, iid, b["wh_id"], opening_date, "opening", q, cost, batch_id, user, lots)
            tq, tv = tq + q, tv + q * cost
        if tq > 0:
            avg = tv / tq
            c.execute("UPDATE items SET avg_cost=?, last_price=? WHERE id=?", (round(avg, 8), inv.rnd(avg), iid))
            c.execute("INSERT INTO price_history(item_id,price_date,unit_price) VALUES(?,?,?)", (iid, str(opening_date), inv.rnd(avg)))
        elif p["first_cost"]:
            c.execute("UPDATE items SET last_price=? WHERE id=?", (inv.rnd(p["first_cost"]), iid))
        total += tv
        log(c, user, "create", "item", iid, f"{p['name']} (استيراد جماعي #{batch_id}" + (f"، {len(p['batches'])} دفعة" if p["batches"] else "") + ")",
            tv or None, cid)
    return total


# ================================================================= 2) الرصيد الافتتاحي / الجرد الفعلي
def _validate_stock(records, ctx, mode, ref_day, conn):
    iss, plan, seen = _Issues(), [], {}
    for rn, r, frm in records:
        rw = _Row(iss, rn, r, STOCK_COLUMNS)
        rw.formulas(frm)
        before = len(iss.items)
        name, bc = rw.text("name"), rw.barcode()
        it = None
        if not name and not bc:
            iss.err(rn, rw.T("name"), "حدد الصنف بالاسم أو بالباركود.")
        else:
            by_name = ctx["items"].get(norm(name)) if name else None
            by_code = ctx["items"].get(ctx["by_code"].get(bc)) if bc else None
            if name and not by_name:
                iss.err(rn, rw.T("name"), f"الصنف «{name}» غير موجود. أضفه أولًا من قالب بيانات الأصناف (أو المنتجات المصنّعة).")
            elif bc and not by_code:
                iss.err(rn, rw.T("barcode"), f"لا يوجد صنف بالباركود {bc}.")
            elif by_name and by_code and by_name["id"] != by_code["id"]:
                iss.err(rn, rw.T("barcode"), f"الباركود {bc} يخص «{by_code['name']}» وليس «{name}».")
            it = by_name or by_code
        wh_id, wh_name = _wh(rw, ctx, "warehouse", ctx["main_wh"])
        qty = rw.num("qty")
        if qty is None and not any(i.row == rn and i.column == rw.T("qty") for i in iss.items):
            iss.err(rn, rw.T("qty"), "الكمية مطلوبة" + (" (صفر مسموح في الجرد)." if mode == "count" else "."))
        elif qty is not None and mode == "opening" and qty <= 0:
            iss.err(rn, rw.T("qty"), "الكمية يجب أن تكون أكبر من صفر للرصيد الافتتاحي.")
        cost, expiry = rw.num("unit_cost"), rw.date("expiry")
        factor, uname, base = 1.0, "", None
        if it:
            unit_in = rw.text("unit")
            uname = it["unit"]
            if unit_in:
                hit = it["units"].get(norm(unit_in))
                if not hit:
                    iss.err(rn, rw.T("unit"), f"الوحدة «{unit_in}» غير معرّفة للصنف «{it['name']}». المعرّف: {'، '.join(u for u, _ in it['units'].values())}.")
                else:
                    uname, factor = hit
            if qty is not None:
                base = inv.rnd(qty * factor)
                if it["unit"].strip().lower() in inv.DISCRETE_UNITS and abs(base - round(base)) > 1e-6:
                    iss.err(rn, rw.T("qty"), f"الكمية بعد التحويل ({base:g} {it['unit']}) كسرية لصنف غير قابل للتجزئة.")
            if mode == "opening" and base and base > 0:
                if cost is None or cost <= 0:
                    if it["cost"] > 0:
                        cost = it["cost"]
                        iss.warn(rn, rw.T("unit_cost"), f"استُخدم متوسط التكلفة الحالي لـ «{it['name']}» ({cost:g}).")
                    else:
                        iss.err(rn, rw.T("unit_cost"), f"متوسط التكلفة مطلوب: «{it['name']}» ليس له تكلفة مسجلة بعد.")
            if mode == "count" and cost is not None and it["cost"] > 0 and abs(cost - it["cost"]) > 1e-6:
                iss.warn(rn, rw.T("unit_cost"), "في الجرد لا تتغير التكلفة؛ يُستخدم متوسط التكلفة الحالي للصنف.")
            _expiry_checks(iss, rn, rw.T("expiry"), expiry, base, it["track"], it["name"], ref_day)
            k = (it["id"], wh_id, str(expiry) if expiry else "")
            if k in seen:
                iss.warn(rn, rw.T("expiry"), f"نفس الصنف والمخزن وتاريخ الصلاحية في الصف {seen[k]}؛ تُجمع الكميتان في دفعة واحدة.")
            seen.setdefault(k, rn)
        if iss.errors_since(before) == 0 and it:
            plan.append({"row": rn, "item_id": it["id"], "name": it["name"], "base_unit": it["unit"], "unit": uname, "qty": qty,
                         "base": base or 0.0, "wh_id": wh_id, "wh_name": wh_name, "expiry": expiry, "cost": cost,
                         "item_cost": it["cost"], "action": "create"})
    summary = []
    if mode == "count":
        agg = OrderedDict()
        for p in plan:
            a = agg.setdefault((p["item_id"], p["wh_id"]), {"name": p["name"], "wh": p["wh_name"], "unit": p["base_unit"], "counted": 0.0, "dates": set()})
            a["counted"] += p["base"]
            if p["base"] > 0:
                a["dates"].add(str(p["expiry"]) if p["expiry"] else "بدون تاريخ")
        for (iid, wid), a in agg.items():
            system = inv.wh_qty(conn, wid, iid)
            summary.append({"الصنف": a["name"], "المخزن": a["wh"], "الوحدة": a["unit"], "رصيد النظام": round(system, 6),
                            "المعدود": round(a["counted"], 6), "الفرق": round(a["counted"] - system, 6),
                            "تواريخ الصلاحية": "، ".join(sorted(a["dates"])) or "—"})
    return plan, iss.items, summary


def _write_stock(c, cid, plan, user, day, batch_id, mode) -> float:
    total = 0.0
    if mode == "opening":
        for p in plan:
            q, cost = inv.rnd(p["base"]), inv.rnd(p["cost"])
            inv.wac_add(c, p["item_id"], q, q * cost)
            lots = inv.adjust(c, cid, p["item_id"], p["wh_id"], q, expiry=p["expiry"], unit_cost=cost, received=day, source=("opening", batch_id))
            inv.move(c, cid, p["item_id"], p["wh_id"], day, "opening", q, cost, batch_id, user, lots)
            if not p["item_cost"]:
                c.execute("UPDATE items SET last_price=? WHERE id=?", (cost, p["item_id"]))
            total += q * cost
        return total
    # الجرد: لكل (صنف، مخزن) في الملف يصبح الرصيد = مجموع المعدود، وتُعاد بناء الدفعات حسب تواريخ الصلاحية المعدودة
    by_wh = OrderedDict()
    for p in plan:
        by_wh.setdefault(p["wh_id"], OrderedDict()).setdefault(p["item_id"], []).append(p)
    for wh_id, items in by_wh.items():
        tid = c.execute("INSERT INTO stocktakes(client_id,count_date,warehouse_id,created_by) VALUES(?,?,?,?)", (cid, str(day), wh_id, user)).lastrowid
        no = sequences.assign(c, cid, "stocktake", tid, day)
        value_diff = 0.0
        for item_id, rows in items.items():
            avg, last = c.execute("SELECT avg_cost, last_price FROM items WHERE id=?", (item_id,)).fetchone()
            uc = inv.unit_cost(avg, last)
            if uc <= 0:                                        # صنف بلا تكلفة: تُعتمد التكلفة المكتوبة في الملف
                given = next((p["cost"] for p in rows if p["cost"]), None)
                if given:
                    uc = inv.rnd(given)
                    c.execute("UPDATE items SET avg_cost=?, last_price=? WHERE id=?", (uc, uc, item_id))
            system = inv.wh_qty(c, wh_id, item_id)
            actual = inv.rnd(sum(p["base"] for p in rows))
            diff = inv.rnd(actual - system)
            c.execute("INSERT INTO stocktake_lines(stocktake_id,item_id,system_qty,actual_qty) VALUES(?,?,?,?)", (tid, item_id, system, actual))
            c.execute("UPDATE stock_lots SET qty_remaining=0 WHERE item_id=? AND warehouse_id=? AND qty_remaining>0", (item_id, wh_id))
            c.execute("""INSERT INTO warehouse_stock(warehouse_id,item_id,qty) VALUES(?,?,?)
                         ON CONFLICT(warehouse_id,item_id) DO UPDATE SET qty=excluded.qty""", (wh_id, item_id, actual))
            c.execute("UPDATE items SET qty_on_hand=ROUND(qty_on_hand+?, 6) WHERE id=?", (diff, item_id))
            groups = OrderedDict()
            for p in rows:
                k = str(p["expiry"]) if p["expiry"] else ""
                groups[k] = groups.get(k, 0.0) + p["base"]
            lots = []
            for k, q in groups.items():
                if q > 1e-9:
                    lots.append([inv._new_lot(c, cid, item_id, wh_id, q, k or None, uc, day, ("stocktake", tid)), inv.rnd(q)])
            if abs(diff) > 1e-9:
                inv.move(c, cid, item_id, wh_id, day, "adjustment", diff, uc, tid, user, lots)
            value_diff += diff * uc
        name = c.execute("SELECT name FROM warehouses WHERE id=?", (wh_id,)).fetchone()[0]
        log(c, user, "create", "stocktake", tid, f"جرد {no} {name} مستورد من ملف (#{batch_id}) - {len(items)} صنف", abs(value_diff), cid)
        total += value_diff
    return total


# ================================================================= 3+4) الوصفات والمنتجات المصنّعة
def _validate_recipes(records, ctx, on_existing, columns, forced_kind=None, opening_date=None):
    iss = _Issues()
    titles = {k: t for k, t, *_ in columns}
    T = lambda k: titles.get(k, k)                       # noqa: E731
    groups: "OrderedDict[str, dict]" = OrderedDict()
    # ---- المرحلة 1: تجميع الصفوف حسب الوصفة والتحقق من خصائص الوصفة
    for rn, r, frm in records:
        rw = _Row(iss, rn, r, columns)
        rw.formulas(frm)
        name = rw.text("recipe")
        if not name:
            iss.err(rn, T("recipe"), "الاسم مطلوب.")
            continue
        g = groups.setdefault(norm(name), {"name": name, "first": rn, "kind": forced_kind, "category": "", "price": None,
                                           "out_unit": "", "yield": None, "barcode": "", "warehouse": "", "opening_qty": None,
                                           "unit_cost": None, "expiry": None, "rows": []})
        if not forced_kind:
            kraw = rw.text("kind")
            if kraw:
                k = KINDS.get(norm(kraw))
                if not k:
                    iss.err(rn, T("kind"), f"نوع غير معروف «{kraw}». المسموح: وجبة، نصف مصنع.")
                elif g["kind"] and g["kind"] != k:
                    iss.err(rn, T("kind"), f"نوع «{name}» يختلف عن الصف {g['first']}.")
                else:
                    g["kind"] = g["kind"] or k
            elif g["kind"] is None and g["first"] == rn:
                g["kind"] = "dish"                             # القالب الجديد للوصفات = وجبات
        for key in ("price", "yield", "opening_qty", "unit_cost"):
            v = rw.num(key, strictly_positive=key in ("price", "yield"))
            if v is not None:
                if g[key] is not None and abs(g[key] - v) > 1e-9:
                    iss.err(rn, T(key), f"قيمة «{T(key)}» تختلف عن الصف {g['first']} لنفس الاسم (تكفي في أول سطر).")
                else:
                    g[key] = g[key] if g[key] is not None else v
        for key, val in (("category", rw.text("category")), ("out_unit", rw.text("out_unit")), ("warehouse", rw.text("warehouse")),
                         ("barcode", rw.barcode())):
            if val:
                if g[key] and norm(g[key]) != norm(val):
                    iss.err(rn, T(key), f"قيمة «{T(key)}» تختلف عن الصف {g['first']} لنفس الاسم.")
                else:
                    g[key] = g[key] or val
        exp = rw.date("expiry")
        if exp is not None:
            if g["expiry"] is not None and g["expiry"] != exp:
                iss.err(rn, T("expiry"), f"تاريخ الصلاحية يختلف عن الصف {g['first']} (للدفعات المتعددة استخدم قالب الرصيد الافتتاحي/الجرد).")
            g["expiry"] = g["expiry"] or exp
        g["rows"].append((rn, r))
    # ---- المرحلة 2: تحقق كل وصفة وحل المكونات
    new_semis = {k: g for k, g in groups.items() if g["kind"] == "semi"}
    plan, file_codes = [], {}
    for key, g in groups.items():
        errs_before = sum(1 for i in iss.items if i.level == "error")
        name, kind = g["name"], g["kind"]
        action = "create"
        if kind == "dish":
            if g["price"] is None:
                iss.err(g["first"], T("price"), f"سعر البيع مطلوب للوجبة «{name}».")
            if key in ctx["dishes"]:
                action = "skip" if on_existing == "skip" else "exists"
            if g["opening_qty"]:
                iss.warn(g["first"], T("opening_qty"), "الوجبات لا يكون لها رصيد مخزني؛ تم تجاهل الكمية.")
        elif kind == "semi":
            if not g["out_unit"]:
                iss.err(g["first"], T("out_unit"), f"وحدة الاستهلاك (وحدة الناتج) مطلوبة للمنتج «{name}».")
            if g["yield"] is None:
                iss.err(g["first"], T("yield"), f"كمية ناتج التشغيلة مطلوبة للمنتج «{name}».")
            ex = ctx["items"].get(key)
            if ex and not ex["semi"]:
                iss.err(g["first"], T("recipe"), f"يوجد صنف خام باسم «{name}». اختر اسمًا مختلفًا للمنتج المصنّع.")
            elif ex and ex["id"] in ctx["semi_with_recipe"]:
                action = "skip" if on_existing == "skip" else "exists"
            elif ex and g["out_unit"] and norm(ex["unit"]) != norm(g["out_unit"]):
                iss.err(g["first"], T("out_unit"), f"وحدة «{name}» الحالية «{ex['unit']}» تختلف عن «{g['out_unit']}».")
            if g["opening_qty"] and g["opening_qty"] > 0:
                if not g["unit_cost"]:
                    iss.err(g["first"], T("unit_cost"), f"متوسط التكلفة مطلوب لرصيد «{name}» الافتتاحي.")
                if g["out_unit"].strip().lower() in inv.DISCRETE_UNITS and abs(g["opening_qty"] - round(g["opening_qty"])) > 1e-6:
                    iss.err(g["first"], T("opening_qty"), f"الكمية {g['opening_qty']:g} كسرية لوحدة غير قابلة للتجزئة.")
            _expiry_checks(iss, g["first"], T("expiry"), g["expiry"], g["opening_qty"], False, name, opening_date or date.today())
        if action == "exists":
            iss.err(g["first"], T("recipe"), f"«{name}» موجودة بالفعل. فعّل «تخطي الموجود» أو احذفها من الملف.")
        elif action == "skip":
            iss.warn(g["first"], T("recipe"), f"«{name}» موجودة فتم تخطيها.")
        wh_id, wh_name = None, ""
        if kind == "semi" and action == "create":
            if g["warehouse"]:
                hit = ctx["whs"].get(norm(g["warehouse"]))
                if not hit:
                    iss.err(g["first"], T("warehouse"), f"المخزن «{g['warehouse']}» غير موجود. المتاح: {'، '.join(ctx['wh_names'])}.")
                else:
                    wh_id, wh_name = hit
            else:
                wh_id = ctx["ops_wh"]
        if g["barcode"] and action == "create":
            used = ctx["dish_codes"] if kind == "dish" else set(ctx["by_code"])
            if g["barcode"] in used:
                iss.err(g["first"], T("barcode"), f"الباركود {g['barcode']} مستخدم بالفعل.")
            elif g["barcode"] in file_codes:
                iss.err(g["first"], T("barcode"), f"الباركود {g['barcode']} مكرر في الملف (الصف {file_codes[g['barcode']]}).")
            file_codes.setdefault(g["barcode"], g["first"])
        lines, seen_ing = [], {}
        if action == "create":
            for rn, r in g["rows"]:
                ing = text(r.get("ingredient"))
                try:
                    ibc = to_barcode(r.get("ing_barcode"))
                except ValueError as e:
                    iss.err(rn, T("ing_barcode"), str(e))
                    continue
                if ibc:
                    hit = ctx["by_code"].get(ibc)
                    if not hit:
                        iss.err(rn, T("ing_barcode"), f"لا يوجد صنف بالباركود {ibc}.")
                        continue
                    if ing and norm(ing) != hit:
                        iss.err(rn, T("ing_barcode"), f"الباركود {ibc} يخص «{ctx['items'][hit]['name']}» وليس «{ing}».")
                        continue
                    ing = ctx["items"][hit]["name"]
                if not ing:
                    iss.err(rn, T("ingredient"), "المكوّن مطلوب (بالاسم أو الباركود).")
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
                    units = {k_: u for k_, (u, _) in db_item["units"].items()}
                    chosen_id = db_item["id"]
                elif ik in new_semis:
                    base_unit = new_semis[ik]["out_unit"]
                    units = {norm(base_unit): base_unit} if base_unit else {}
                    chosen_id = None
                else:
                    iss.err(rn, T("ingredient"), f"المكوّن «{ing}» غير موجود (لا في الأصناف ولا ضمن المنتجات المصنّعة في هذا الملف).")
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
                     "out_unit": g["out_unit"], "yield": g["yield"], "lines": lines, "action": action, "key": key, "valid": ok,
                     "barcode": g["barcode"], "opening_qty": g["opening_qty"] if kind == "semi" else None, "unit_cost": g["unit_cost"],
                     "expiry": g["expiry"], "wh_id": wh_id, "wh_name": wh_name})
    # ---- دورات بين المنتجات المصنّعة الجديدة (أ تحتاج ب وب تحتاج أ)
    graph = {p["key"]: {l["ing_key"] for l in p["lines"] if l["ing_key"] in new_semis} for p in plan if p["kind"] == "semi" and p["action"] == "create"}
    state = {}

    def dfs(n, path):
        state[n] = 1
        for m in graph.get(n, ()):
            if state.get(m) == 1:
                cyc = " ← ".join(new_semis[x]["name"] for x in path[path.index(m):] + [m]) if m in path else new_semis[m]["name"]
                iss.err(new_semis[n]["first"], T("ingredient"), f"دورة مغلقة بين المنتجات المصنّعة: {cyc}.")
            elif state.get(m) is None:
                dfs(m, path + [m])
        state[n] = 2
    for n in list(graph):
        if state.get(n) is None:
            dfs(n, [n])
    return plan, iss.items


def _write_recipes(c, cid, plan, user, opening_date, batch_id) -> tuple[int, float]:
    created, total = 0, 0.0
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
        iid = ids[p["key"]]
        if p["barcode"]:
            c.execute("UPDATE items SET barcode=? WHERE id=?", (p["barcode"], iid))
        if p["opening_qty"] and p["opening_qty"] > 0:
            q, cost = inv.rnd(p["opening_qty"]), inv.rnd(p["unit_cost"])
            inv.wac_add(c, iid, q, q * cost)
            lots = inv.adjust(c, cid, iid, p["wh_id"], q, expiry=p["expiry"], unit_cost=cost, received=opening_date, source=("opening", batch_id))
            inv.move(c, cid, iid, p["wh_id"], opening_date, "opening", q, cost, batch_id, user, lots)
            c.execute("UPDATE items SET last_price=? WHERE id=?", (cost, iid))
            total += q * cost
        created += 1
    for p in dishes:
        did = services._create_dish_c(c, cid, p["name"], p["category"], p["price"], lines(p), user)
        if p["barcode"]:
            c.execute("UPDATE dishes SET barcode=? WHERE id=?", (p["barcode"], did))
        created += 1
    return created, total


# ================================================================= الواجهات العامة
KIND_SPEC = {  # النوع: (الأعمدة، المرادفات)
    "items": (ITEM_COLUMNS, ITEM_ALIASES), "stock": (STOCK_COLUMNS, STOCK_ALIASES),
    "recipes": (RECIPE_COLUMNS, RECIPE_ALIASES), "products": (PRODUCT_COLUMNS, RECIPE_ALIASES),
}


def _kind(kind) -> str:
    kind = "items" if kind == "materials" else kind
    if kind not in KIND_SPEC:
        raise ValueError("نوع استيراد غير معروف.")
    return kind


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prepare(kind, data, filename, conn, cid, on_existing, mode="opening", day=None):
    kind = _kind(kind)
    day = day or date.today()
    columns, aliases = KIND_SPEC[kind]
    headers, rows = read_table(data, filename)
    mapping, unknown, present = _map_headers(headers, columns, aliases)
    records = _records(rows, mapping)
    rep = ImportReport(kind=kind, filename=filename, rows_total=len(records), file_sha=_hash(data))
    for h in unknown:
        rep.issues.append(Issue(None, h, "عمود غير معروف وسيتم تجاهله.", "warning"))
    if kind == "stock" and not ({"name", "barcode"} & present):
        raise ImportFileError("يلزم عمود «اسم الصنف» أو «الباركود» لتحديد الأصناف.")
    if not records:
        raise ImportFileError("لا توجد صفوف بيانات (الصف المسبوق بكلمة «مثال» لا يُستورد).")
    ctx = _ctx(conn, cid)
    if kind == "items":
        rep.plan, issues = _validate_items(records, ctx, on_existing, day)
        rep.batches = sum(len(p["batches"]) for p in rep.plan if p["action"] == "create")
    elif kind == "stock":
        if mode not in ("opening", "count"):
            raise ValueError("وضع غير صحيح.")
        rep.plan, issues, rep.summary = _validate_stock(records, ctx, mode, day, conn)
        rep.batches = sum(1 for p in rep.plan if p["base"] > 0)
    else:
        rep.plan, issues = _validate_recipes(records, ctx, on_existing, columns, "semi" if kind == "products" else None, day)
        rep.batches = sum(1 for p in rep.plan if p["action"] == "create" and p.get("opening_qty"))
    rep.issues += issues
    if day and any(p.get("batches") or p.get("base") or p.get("opening_qty") for p in rep.plan):
        try:
            controls.check_open(conn, cid, day)
        except ValueError as e:
            rep.issues.append(Issue(None, "التاريخ", str(e), "error"))
    dup = conn.execute("SELECT created_at FROM import_batches WHERE client_id=? AND kind=? AND file_sha256=? ORDER BY id DESC LIMIT 1",
                       (cid, kind, rep.file_sha)).fetchone()
    if dup:
        rep.issues.append(Issue(None, "الملف", f"تم استيراد هذا الملف نفسه من قبل بتاريخ {dup[0]}.", "warning"))
    rep.created = sum(1 for p in rep.plan if p["action"] == "create")
    rep.skipped = sum(1 for p in rep.plan if p["action"] == "skip")
    return rep


@guard("import")
def preview(kind, cid, data, filename, user, on_existing="error", mode="opening", day=None):
    """تحقق بدون كتابة: يرجع ImportReport بكل الأخطاء والتنبيهات وخطة الإدخال. للمدير العام فقط."""
    _require_admin(user)
    conn = get_conn()
    try:
        return _prepare(kind, data, filename, conn, cid, on_existing, mode, day)
    finally:
        conn.close()


@guard("import")
def run(kind, cid, data, filename, user, on_existing="error", mode="opening", day=None):
    """يتحقق ويكتب في معاملة واحدة (التحقق والكتابة داخل نفس القفل: لا سباقات). أي خطأ = لا شيء يُكتب."""
    _require_admin(user)
    kind = _kind(kind)
    day = day or date.today()
    with tx() as c:
        rep = _prepare(kind, data, filename, c, cid, on_existing, mode, day)
        if rep.error_count:
            raise ImportValidationError(rep)
        batch = c.execute("INSERT INTO import_batches(client_id,kind,filename,file_sha256,rows_total,created_by,created_at) "
                          "VALUES(?,?,?,?,?,?,datetime('now','localtime'))",
                          (cid, kind, (filename or "")[:200], rep.file_sha, rep.rows_total, user)).lastrowid
        if kind == "items":
            rep.total_value = _write_items(c, cid, rep.plan, user, day, batch)
            summary = f"{rep.created} صنف جديد، {rep.skipped} متخطى، {rep.batches} دفعة، قيمة الرصيد الافتتاحي {rep.total_value:,.2f}"
        elif kind == "stock":
            rep.total_value = _write_stock(c, cid, rep.plan, user, day, batch, mode)
            summary = (f"{'جرد فعلي' if mode == 'count' else 'رصيد افتتاحي'}: {rep.created} صف، {rep.batches} دفعة، " +
                       (f"صافي فرق القيمة {rep.total_value:,.2f}" if mode == "count" else f"القيمة {rep.total_value:,.2f}"))
        else:
            n, rep.total_value = _write_recipes(c, cid, rep.plan, user, day, batch)
            summary = f"{n} {'منتج مصنّع' if kind == 'products' else 'وصفة'} جديد، {rep.skipped} متخطى" + \
                      (f"، رصيد افتتاحي بقيمة {rep.total_value:,.2f}" if rep.total_value else "")
        rep.batch_id = batch
        c.execute("UPDATE import_batches SET created_count=?, skipped_count=?, summary=? WHERE id=?", (rep.created, rep.skipped, summary, batch))
        log(c, user, "import", "import", batch, f"{KIND_AR[kind]} من «{filename}»: {summary}", rep.total_value or None, cid)
    rep.summary_text = summary
    return rep


# توافق مع الواجهات السابقة
def preview_materials(cid, data, filename, user, on_existing="error"):
    return preview("items", cid, data, filename, user, on_existing)


def preview_recipes(cid, data, filename, user, on_existing="error"):
    return preview("recipes", cid, data, filename, user, on_existing)


def import_materials(cid, data, filename, user, on_existing="error", opening_date=None):
    return run("items", cid, data, filename, user, on_existing, day=opening_date)


def import_recipes(cid, data, filename, user, on_existing="error"):
    return run("recipes", cid, data, filename, user, on_existing)


def history(cid) -> pd.DataFrame:
    return query("""SELECT id, created_at, kind, filename, rows_total, created_count, skipped_count, summary, created_by
                    FROM import_batches WHERE client_id=? ORDER BY id DESC""", (cid,))


# ================================================================= القوالب
TEMPLATE_INFO = {  # النوع: (اسم الورقة، العنوان، اسم الملف، قواعد)
    "items": ("الأصناف", "قالب بيانات الأصناف (الخامات) والرصيد الافتتاحي بالدفعات", "sanad_items_template.xlsx", [
        "صف لكل صنف. لإدخال أكثر من دفعة (تاريخ صلاحية) لنفس الصنف كرّر اسم الصنف في صف جديد واكتب الكمية والتكلفة وتاريخ الصلاحية فقط.",
        "الكمية ومتوسط التكلفة بوحدة الاستهلاك (وليس وحدة الشراء). مثال: شيكارة 50 كجم، الرصيد 120 كجم، التكلفة 20.5 للكجم.",
        f"تاريخ الصلاحية بصيغة {DATE_HINT} فقط (مثل 2026-03-31)، وإلزامي للأصناف التي «تتتبع الصلاحية = نعم».",
        "إن حددت وحدة شراء فمعامل التحويل مطلوب (1 وحدة شراء = كم وحدة استهلاك).",
        "الأصناف الموجودة بالمخزن تُرفض (أو تُتخطى بخيار «تخطي الموجود»)؛ لإضافة رصيد لصنف موجود استخدم قالب الرصيد الافتتاحي/الجرد.",
        "الباركود منسّق كنص حتى لا تضيع الأصفار في بدايته. لا تحوّله إلى رقم.",
        "أي خطأ في أي صف يُلغي الاستيراد بالكامل ويُعرض رقم الصف والعمود.",
        "الصيغ المقبولة: .xlsx أو .csv (ترميز UTF-8). الأرقام العربية ٠-٩ مقبولة."]),
    "stock": ("الرصيد والجرد", "قالب الرصيد الافتتاحي / الجرد الفعلي (صف لكل دفعة)", "sanad_stock_template.xlsx", [
        "صف لكل دفعة: الصنف + المخزن + الكمية + تاريخ الصلاحية. نفس الصنف بتاريخين = صفان.",
        "حدد الصنف بالاسم أو بالباركود (أو كليهما). الصنف يجب أن يكون موجودًا (استورده أولًا من قالب الأصناف).",
        "الكمية بوحدة الاستهلاك، أو بأي وحدة معرّفة للصنف إن كتبتها في عمود الوحدة وتُحوَّل تلقائيًا.",
        f"تاريخ الصلاحية بصيغة {DATE_HINT} فقط، وإلزامي للأصناف التي تتتبع الصلاحية.",
        "وضع «رصيد افتتاحي»: تُضاف الكميات للرصيد الحالي كدفعات جديدة ويُعاد حساب متوسط التكلفة.",
        "وضع «جرد فعلي»: لكل صنف ومخزن في الملف يصبح الرصيد = مجموع الكميات المعدودة، وتُستبدل دفعاته بالتواريخ المعدودة، "
        "ويُسجَّل جرد بفرق الكمية والقيمة. الأصناف غير المذكورة في الملف لا تتغير.",
        "للجرد: نزّل القالب المملوء بالأرصدة الحالية من شاشة الاستيراد، وعدّل الكميات الفعلية فقط."]),
    "recipes": ("الوصفات", "قالب الوصفات (مكونات الوجبات)", "sanad_recipes_template.xlsx", [
        "كل سطر = مكوّن واحد. كرّر اسم الوجبة في كل سطر من مكوناتها، و«سعر البيع» يكفي في أول سطر.",
        "المكوّن صنف موجود (خامة أو منتج مصنّع) بالاسم أو بالباركود.",
        "الكمية لوجبة واحدة. وحدة الاستهلاك اختيارية: فارغة = وحدة استهلاك المكوّن، أو أي وحدة معرّفة له وتُحوَّل تلقائيًا.",
        "الوجبات لا يكون لها رصيد مخزني ولا تاريخ صلاحية؛ المخزون وتواريخ الصلاحية تخص الخامات والمنتجات المصنّعة.",
        "الترتيب: الأصناف ← المنتجات المصنّعة ← الوصفات.",
        "أي خطأ في أي صف يُلغي استيراد الملف بالكامل ويُعرض رقم الصف والعمود."]),
    "products": ("المنتجات المصنّعة", "قالب المنتجات المصنّعة (النصف مصنعة) ومكوناتها ورصيدها الافتتاحي", "sanad_products_template.xlsx", [
        "كل سطر = مكوّن واحد. كرّر اسم المنتج في كل سطر؛ وحدة الاستهلاك وكمية الناتج والرصيد الافتتاحي تكفي في أول سطر.",
        "المكوّن صنف موجود أو منتج مصنّع آخر معرّف في نفس الملف (بدون دورات مغلقة).",
        f"الرصيد الافتتاحي اختياري: الكمية + متوسط التكلفة + تاريخ الصلاحية ({DATE_HINT}). المخزن الافتراضي = مخزن التشغيل.",
        "لأكثر من دفعة للمنتج استورد المنتج هنا بدون رصيد، ثم أدخل دفعاته من قالب الرصيد الافتتاحي/الجرد.",
        "أي خطأ في أي صف يُلغي استيراد الملف بالكامل ويُعرض رقم الصف والعمود."]),
}


def _template(kind, warehouses=None, rows=None) -> bytes:
    sheet_title, legend_title, _, extra_rules = TEMPLATE_INFO[kind]
    columns = [c for c in KIND_SPEC[kind][0] if c[0] not in HIDDEN_KEYS.get(kind, set())]
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.sheet_view.rightToLeft = True
    arial = lambda **k: Font(name="Arial", size=k.pop("size", 10), **k)         # noqa: E731
    req_fill, opt_fill = PatternFill("solid", fgColor="C8A24A"), PatternFill("solid", fgColor="0A1F44")
    last = TEMPLATE_ROWS + 1
    for j, (key, title, req, example, note) in enumerate(columns, 1):
        h = ws.cell(row=1, column=j, value=title)
        h.font = arial(bold=True, color="FFFFFF" if not req else "0A1F44")
        h.fill = req_fill if req else opt_fill
        h.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        h.comment = Comment(("مطلوب. " if req else "اختياري. ") + note, "Sanad")
        col = get_column_letter(j)
        ws.column_dimensions[col].width = max(16, len(title) * 1.3 + 4)
        t = COL_TYPES.get(key)
        fmt = {"text": "@", "date": "yyyy-mm-dd", "number": "#,##0.###"}.get(t)
        if fmt:
            for r in range(2, last + 1):
                ws.cell(row=r, column=j).number_format = fmt
        if t == "date":
            dv = DataValidation(type="date", operator="between", formula1="DATE(2000,1,1)", formula2="DATE(2100,12,31)", allow_blank=True,
                                showErrorMessage=True, errorTitle="تاريخ غير صالح", error=f"اكتب التاريخ بصيغة {DATE_HINT} مثل 2026-03-31",
                                promptTitle="تاريخ الصلاحية", prompt=DATE_HINT, showInputMessage=True)
        elif t == "bool":
            dv = DataValidation(type="list", formula1='"نعم,لا"', allow_blank=True)
        elif t == "number":
            dv = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True, showErrorMessage=True,
                                errorTitle="قيمة غير صالحة", error="اكتب رقمًا صفرًا أو أكبر")
        elif t == "wh" and warehouses and len(",".join(warehouses)) < 250 and not any("," in w or '"' in w for w in warehouses):
            dv = DataValidation(type="list", formula1='"' + ",".join(warehouses) + '"', allow_blank=True)
        else:
            dv = None
        if dv is not None:
            ws.add_data_validation(dv)
            dv.add(f"{col}2:{col}{last}")
    if rows:                                                   # قالب مملوء ببيانات حقيقية (للجرد)
        keys = [c[0] for c in columns]
        for i, rec in enumerate(rows, start=2):
            for j, k in enumerate(keys, 1):
                v = rec.get(k)
                ws.cell(row=i, column=j, value=(date.fromisoformat(v) if k == "expiry" and isinstance(v, str) and v else v))
    else:
        for j, (key, title, req, example, note) in enumerate(columns, 1):
            if COL_TYPES.get(key) == "date" and example:
                example = date.fromisoformat(example)
            ex = ws.cell(row=2, column=j, value=(None if example == "" else example))
            ex.font = arial(italic=True, color="888888")
    ws.row_dimensions[1].height = 36
    ws.freeze_panes = "A2"
    leg = wb.create_sheet("تعليمات")
    leg.sheet_view.rightToLeft = True
    leg.column_dimensions["A"].width = 30
    leg.column_dimensions["B"].width = 12
    leg.column_dimensions["C"].width = 95
    leg["A1"] = legend_title
    leg["A1"].font = arial(size=14, bold=True, color="0A1F44")
    leg["A2"] = ("اكتب بياناتك في ورقة «" + sheet_title + "» بدءًا من الصف 3 (الصف 2 مثال يبدأ بكلمة «مثال» ولا يُستورد؛ احذفه أو اتركه). "
                 "لا تغيّر عناوين الأعمدة (الصف 1). العنوان الذهبي = مطلوب، الكحلي = اختياري.") if not rows else \
                ("الملف مملوء بالأرصدة الحالية لكل دفعة. عدّل عمود «الكمية» بالكميات الفعلية المعدودة (وأضف صفوفًا لدفعات جديدة)، ثم ارفعه بوضع «جرد فعلي».")
    leg["A2"].font = arial(color="555555")
    for j, h in enumerate(["العمود", "مطلوب؟", "الشرح"], 1):
        cell = leg.cell(row=4, column=j, value=h)
        cell.font, cell.fill = arial(bold=True, color="FFFFFF"), opt_fill
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


def template(kind, warehouses=None) -> bytes:
    """قالب Excel فارغ منسّق (تاريخ/نص/قوائم منسدلة/تعليمات) لنوع الاستيراد."""
    return _template(_kind(kind), warehouses)


def template_filename(kind) -> str:
    return TEMPLATE_INFO[_kind(kind)][2]


def stock_count_template(cid, warehouses=None, warehouse_id=None, user=None) -> bytes:
    """قالب الجرد مملوءًا بالأرصدة الحالية: صف لكل (صنف، مخزن، تاريخ صلاحية). يحتوي بيانات المخزون، فهو للمدير العام فقط."""
    _require_admin(user)
    sql = """SELECT i.name, i.barcode, i.unit, w.name AS wh, l.expiry_date, SUM(l.qty_remaining) AS q,
             CASE WHEN i.avg_cost>0 THEN i.avg_cost ELSE COALESCE(i.last_price,0) END AS cost
             FROM stock_lots l JOIN items i ON i.id=l.item_id JOIN warehouses w ON w.id=l.warehouse_id
             WHERE l.client_id=? AND l.qty_remaining>0.000000001 {wh}
             GROUP BY i.id, w.id, l.expiry_date ORDER BY w.name, i.name, (l.expiry_date IS NULL), l.expiry_date"""
    params = [cid] + ([int(warehouse_id)] if warehouse_id else [])
    df = query(sql.format(wh="AND l.warehouse_id=?" if warehouse_id else ""), tuple(params))
    rows = [{"name": r["name"], "barcode": r["barcode"] or None, "unit": r["unit"], "warehouse": r["wh"], "qty": round(float(r["q"]), 6),
             "unit_cost": round(float(r["cost"]), 6), "expiry": r["expiry_date"]} for _, r in df.iterrows()]
    return _template("stock", warehouses, rows or [{"name": None}])


# توافق مع الواجهات السابقة
def template_materials(warehouses=None) -> bytes:
    return template("items", warehouses)


def template_recipes() -> bytes:
    return template("recipes")
