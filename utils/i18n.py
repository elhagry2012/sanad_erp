"""اللغة: عربي (الأصل) / English بمصطلحات أنظمة ERP والمحاسبة.

المبدأ: نصوص الواجهة مكتوبة بالعربية في الكود كما هي، والترجمة تتم **عند العرض فقط**:
- install() يلف دوال العرض في Streamlit (DeltaGenerator) فتُترجم العناوين والنصوص والأزرار وأسماء التبويبات
  وعناوين أعمدة الجداول وخيارات القوائم عند اختيار الإنجليزية.
- القيم نفسها لا تُترجم أبدًا: خيارات القوائم تبقى بقيمها الأصلية (تُترجم طريقة عرضها فقط)، وجداول التحرير تُترجم
  عناوين أعمدتها عبر column_config فقط، فلا يتغير أي منطق أو مقارنة في الكود. بيانات المستخدم (أسماء الأصناف...) لا تُترجم.
- t(s): مطابقة كاملة من القاموس، وإلا ترجمة المقاطع المعروفة داخل النص (للرسائل المركّبة بقيم متغيرة) بشرط
  ألا يكون المقطع جزءًا من كلمة عربية أطول، فلا يُمس اسم صنف أو مورد.
- النصوص غير المترجمة تُجمع في missing() لقياس التغطية واستكمال القاموس.
"""
import functools
import re
import zlib

import pandas as pd
import streamlit as st

LANGS = {"ar": "العربية", "en": "English"}
_AR = re.compile(r"[؀-ۿ]")
_ZW = ("​", "‌", "‍", "⁠")          # لاحقة غير مرئية تبقي معرّف كل عنصر فريدًا بعد الترجمة
_missing: set = set()
_installed = False


# ============================================================ اللغة الحالية
def lang() -> str:
    try:
        v = st.session_state.get("lang")
        if v is None:
            v = st.query_params.get("lang", "ar")
    except Exception:
        return "ar"
    return v if v in LANGS else "ar"


def is_en() -> bool:
    return lang() == "en"


def set_lang(v: str) -> None:
    st.session_state["lang"] = v if v in LANGS else "ar"
    if v == "en":
        st.query_params["lang"] = "en"
    else:
        st.query_params.pop("lang", None)


# ============================================================ القاموس
@functools.lru_cache(maxsize=1)
def _dict() -> dict:
    from utils.lang_en import EN
    return {k: v for k, v in EN.items() if v}


@functools.lru_cache(maxsize=1)
def _frag_re():
    """كل مفاتيح القاموس كمقاطع (الأطول أولًا) لترجمة النصوص المركّبة."""
    keys = sorted((k for k in _dict() if len(k.strip()) >= 2), key=len, reverse=True)
    if not keys:
        return None
    def piece(k):
        # حد الكلمة يُفحص فقط على الطرف العربي: «**هاتف المورد:** » يُترجم حتى لو جاء بعده اسم مورد عربي
        pre = r"(?<![؀-ۿ])" if _AR.match(k[0]) else ""
        post = r"(?![؀-ۿ])" if _AR.match(k[-1]) else ""
        if not re.search(r"\s", k):
            # كلمة مفردة: لا تُترجم إن كانت جزءًا من عبارة عربية أطول (اسم مورد/صنف مثل «مورد الخير» يبقى كما هو)
            pre += r"(?<![؀-ۿ]\s)" if _AR.match(k[0]) else ""
            post += r"(?!\s[؀-ۿ])" if _AR.match(k[-1]) else ""
        return pre + re.escape(k) + post
    return re.compile("|".join(piece(k) for k in keys))


@functools.lru_cache(maxsize=20000)
def _tr(s: str) -> str:
    d = _dict()
    if s in d:
        return d[s]
    core = s.strip()
    if core in d:                                            # نفس النص بمسافات حوله
        return s.replace(core, d[core])
    rx = _frag_re()
    out = rx.sub(lambda m: d[m.group(0)], s) if rx else s
    if _AR.search(out):
        _missing.add(out.strip()[:300])                       # ما تبقى بالعربية بعد الترجمة (بيانات المستخدم أو نص ناقص)
    return out


def t(s):
    """يترجم النص للإنجليزية إن كانت اللغة الحالية الإنجليزية، وإلا يرجعه كما هو."""
    if not isinstance(s, str) or not s or not is_en() or not _AR.search(s):
        return s
    return _tr(s)


def missing() -> list:
    return sorted(_missing)


def _tag(original: str, translated: str) -> str:
    """لاحقة غير مرئية مشتقة من النص الأصلي: نصّان عربيان مختلفان تُرجما لنفس الإنجليزية لا يتعارض معرّفاهما."""
    if translated == original:
        return translated
    h = zlib.crc32(original.encode("utf-8"))
    return translated + "".join(_ZW[(h >> (2 * i)) & 3] for i in range(6))


def label(s):
    return _tag(s, t(s)) if isinstance(s, str) and is_en() else s


# ============================================================ جداول وأشكال
def frame(df):
    """نسخة للعرض فقط: عناوين الأعمدة والقيم النصية المعروفة (حالات، أنواع حركة...) بالإنجليزية."""
    if not is_en() or not isinstance(df, pd.DataFrame) or df.empty and not len(df.columns):
        return df
    out = df.rename(columns={c: t(c) for c in df.columns if isinstance(c, str)})
    if len(set(out.columns)) != len(out.columns):           # عمودان ترجمتهما واحدة: نبقي الأصل
        out = df.copy()
    d = _dict()
    for c in out.columns:
        if out[c].dtype == object:
            vals = out[c]
            mask = vals.map(lambda v: isinstance(v, str) and v in d)
            if mask.any():
                out.loc[mask, c] = vals[mask].map(d)
    return out


def column_config(df, cfg):
    """لجداول التحرير: ترجمة عناوين الأعمدة عبر column_config فقط (أسماء الأعمدة الفعلية لا تتغير)."""
    if not is_en() or not isinstance(df, pd.DataFrame):
        return cfg
    cfg = dict(cfg or {})
    for c in df.columns:
        if not isinstance(c, str):
            continue
        cur = cfg.get(c, ...)
        if cur is None:
            continue                                         # عمود مخفي
        if cur is ...:
            if _AR.search(c):
                cfg[c] = {"label": t(c)}
        elif isinstance(cur, str):
            cfg[c] = t(cur)
        elif isinstance(cur, dict):
            cur = dict(cur)
            cur["label"] = t(cur.get("label") or c)
            if cur.get("help"):
                cur["help"] = t(cur["help"])
            cfg[c] = cur
    return cfg


def figure(fig):
    """عناوين الرسم والمحاور ووسيلة الإيضاح وأسماء السلاسل (البيانات نفسها لا تُترجم)."""
    if not is_en() or fig is None:
        return fig
    try:
        lay = fig.layout
        if lay.title and lay.title.text:
            lay.title.text = t(lay.title.text)
        for ax in [k for k in lay if k.startswith(("xaxis", "yaxis"))]:
            a = lay[ax]
            if a.title and a.title.text:
                a.title.text = t(a.title.text)
        if lay.legend and lay.legend.title and lay.legend.title.text:
            lay.legend.title.text = t(lay.legend.title.text)
        for tr in fig.data:
            if getattr(tr, "name", None):
                tr.name = t(tr.name)
            if getattr(tr, "hovertemplate", None):
                tr.hovertemplate = t(tr.hovertemplate)
            if getattr(tr, "legendgroup", None):
                tr.legendgroup = t(tr.legendgroup)
        for ann in lay.annotations or ():
            if ann.text:
                ann.text = t(ann.text)
    except Exception:
        pass
    return fig


# ============================================================ لفّ دوال Streamlit
_BODY = ("markdown", "caption", "title", "header", "subheader", "success", "info", "warning", "error", "toast", "text")
_LABEL = ("button", "download_button", "form_submit_button", "checkbox", "toggle", "radio", "selectbox", "multiselect",
          "slider", "select_slider", "text_input", "text_area", "number_input", "date_input", "time_input", "file_uploader",
          "expander", "popover", "link_button", "color_picker", "metric", "pills", "segmented_control", "feedback")
_OPTIONS = ("radio", "selectbox", "multiselect", "select_slider", "pills", "segmented_control")
_TEXT_KW = ("help", "placeholder", "caption")


def _first(a, k, name):
    if name in k:
        return a, k, k[name]
    if a:
        return a, k, a[0]
    return a, k, None


def _set_first(a, k, name, val):
    if name in k:
        k[name] = val
        return a, k
    if a:
        return (val,) + tuple(a[1:]), k
    return a, k


def _wrap(name, orig):
    body = name in _BODY
    lab = name in _LABEL
    opts = name in _OPTIONS

    @functools.wraps(orig)
    def w(self, *a, **k):
        if not is_en():
            return orig(self, *a, **k)
        if body:
            a, k, v = _first(a, k, "body")
            if isinstance(v, str):
                a, k = _set_first(a, k, "body", t(v))
        elif lab:
            a, k, v = _first(a, k, "label")
            if isinstance(v, str):
                a, k = _set_first(a, k, "label", t(v) if name in ("metric", "expander", "popover") else label(v))
            if name == "metric":
                for key in ("value", "delta"):
                    if isinstance(k.get(key), str):
                        k[key] = t(k[key])
                if len(a) > 1 and isinstance(a[1], str):
                    a = (a[0], t(a[1])) + tuple(a[2:])
        for key in _TEXT_KW:
            if isinstance(k.get(key), str):
                k[key] = t(k[key])
        if opts:
            ff = k.get("format_func") or str
            # خياران مختلفان قد يُترجمان لنفس النص (مثل «منتهي الصلاحية» و«انتهاء صلاحية» = Expired)؛
            # Streamlit يطابق الاختيار بالنص المعروض، فيُميَّز المتكرر بلاحقة غير مرئية مشتقة من الأصل
            def _as_list(o):                                  # DataFrame = عمودها الأول في Streamlit؛ يُترك كما هو
                return o if o is None or isinstance(o, pd.DataFrame) else list(o)
            if "options" in k:
                k["options"] = _as_list(k["options"])
                options = k["options"]
            elif len(a) > 1:
                a = (a[0], _as_list(a[1])) + tuple(a[2:])
                options = a[1]
            else:
                options = None
            if isinstance(options, pd.DataFrame):
                options = None
            seen, dup = {}, set()
            for o in options or []:
                try:
                    src = ff(o)
                except Exception:
                    continue
                shown = _tr(src) if isinstance(src, str) and _AR.search(src) else src
                if shown in seen and seen[shown] != src:
                    dup.add(shown)
                seen.setdefault(shown, src)

            def fmt(o, _f=ff, _dup=frozenset(dup)):
                # دالة نقية: اللغة حُسمت عند رسم العنصر (لا تعتمد على حالة الجلسة لحظة الاستدعاء)
                src = _f(o)
                if not isinstance(src, str) or not _AR.search(src):
                    return src
                shown = _tr(src)
                return _tag(src, shown) if shown in _dup else shown
            k["format_func"] = fmt
        return orig(self, *a, **k)
    return w


def _wrap_tabs(orig):
    @functools.wraps(orig)
    def w(self, tabs, *a, **k):
        if is_en():
            tabs = [label(x) for x in tabs]
        return orig(self, tabs, *a, **k)
    return w


def _wrap_write(orig):
    @functools.wraps(orig)
    def w(self, *args, **k):
        if is_en():
            args = tuple(t(x) if isinstance(x, str) else x for x in args)
        return orig(self, *args, **k)
    return w


def _wrap_table(orig, editable):
    @functools.wraps(orig)
    def w(self, data=None, *a, **k):
        if is_en() and isinstance(data, pd.DataFrame):
            if editable:
                k["column_config"] = column_config(data, k.get("column_config"))
            else:
                cfg = k.get("column_config")
                data = frame(data)
                if cfg:
                    k["column_config"] = {t(c) if isinstance(c, str) else c: (t(v) if isinstance(v, str) else v) for c, v in cfg.items()}
        return orig(self, data, *a, **k)
    return w


def install() -> None:
    """يُستدعى مرة واحدة لكل عملية خادم. يعيد ربط st.xxx بالدوال الملفوفة (لأنها مربوطة مسبقًا بالحاوية الرئيسية)."""
    global _installed
    if _installed:
        return
    from streamlit.delta_generator import DeltaGenerator as DG
    names = set(_BODY) | set(_LABEL)
    for n in names:
        f = getattr(DG, n, None)
        if f is not None:
            setattr(DG, n, _wrap(n, f))
    DG.tabs = _wrap_tabs(DG.tabs)
    DG.write = _wrap_write(DG.write)
    DG.dataframe = _wrap_table(DG.dataframe, False)
    DG.table = _wrap_table(DG.table, False)
    DG.data_editor = _wrap_table(DG.data_editor, True)
    main = st._main
    for n in list(names) + ["tabs", "write", "dataframe", "table", "data_editor"]:
        if hasattr(main, n) and hasattr(st, n):
            setattr(st, n, getattr(main, n))
    _installed = True
