"""تشكيل الحروف العربية وترتيبها البصري (RTL) لملفات PDF.

ReportLab يرسم الحروف كما هي من اليسار لليمين، فيلزم (1) ربط الحروف بأشكالها الصحيحة (بداية/وسط/نهاية)،
و(2) عكس الترتيب مع إبقاء الأرقام والكلمات اللاتينية سليمة. الجداول مبنية من تفكيك Unicode نفسه
لأشكال العرض (Presentation Forms-B)، فلا حاجة لمكتبات خارجية. التشكيل (الفتحة...) يُحذف للتبسيط.
"""
import re
import unicodedata

_TAGS = {"<isolated>": "iso", "<final>": "fin", "<initial>": "ini", "<medial>": "med"}
_FORMS: dict[str, dict[str, str]] = {}     # الحرف الأساسي -> أشكاله
_LIGA: dict[str, dict[str, str]] = {}      # حرف الألف بعد اللام -> شكلا اللام-ألف

for _cp in range(0xFE70, 0xFEFF):
    _ch = chr(_cp)
    _dec = unicodedata.decomposition(_ch)
    if not _dec.startswith("<"):
        continue
    _tag, _rest = _dec.split(" ", 1)
    _parts = [chr(int(p, 16)) for p in _rest.split()]
    _key = _TAGS.get(_tag)
    if not _key:
        continue
    if len(_parts) == 1:
        _FORMS.setdefault(_parts[0], {})[_key] = _ch
    elif len(_parts) == 2 and _parts[0] == "\u0644":      # لا لأ لإ لآ
        _LIGA.setdefault(_parts[1], {})[_key] = _ch

_DUAL = {c for c, f in _FORMS.items() if "ini" in f}        # يتصل من الجهتين
_RIGHT = {c for c, f in _FORMS.items() if "fin" in f}       # يتصل بما قبله (يمين) على الأقل
_STRIP = re.compile("[\u064B-\u065F\u0670\u0640]")          # تشكيل + تطويل
_MIRROR = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "«": "»", "»": "«"}
_LTR = re.compile(r"(?<![A-Za-z0-9_])[+\-]?[A-Za-z0-9_]+(?:[-.,:/@][A-Za-z0-9_]+)*%?|[A-Za-z0-9_]+")


def has_arabic(text: str) -> bool:
    return any("\u0600" <= c <= "\u06FF" or "\uFB50" <= c <= "\uFEFF" for c in text)


def _is_ar(c: str) -> bool:
    return "\u0600" <= c <= "\u06FF" or "\uFB50" <= c <= "\uFDFF" or "\uFE70" <= c <= "\uFEFF"


def shape(text: str) -> str:
    """يحوّل الحروف العربية لأشكالها المتصلة (بترتيب منطقي)."""
    text = _STRIP.sub("", text)
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c not in _FORMS:
            out.append(c)
            i += 1
            continue
        prev = text[i - 1] if i > 0 else ""
        joins_prev = prev in _DUAL and (c in _RIGHT)
        if c == "\u0644" and i + 1 < n and text[i + 1] in _LIGA:           # لام + ألف
            lig = _LIGA[text[i + 1]]
            out.append(lig["fin"] if joins_prev and "fin" in lig else lig["iso"])
            i += 2
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        joins_next = c in _DUAL and nxt in _RIGHT
        form = "med" if joins_prev and joins_next else "fin" if joins_prev else "ini" if joins_next else "iso"
        out.append(_FORMS[c].get(form) or _FORMS[c].get("iso", c))
        i += 1
    return "".join(out)


def _tokens(text: str):
    """(نص، فئة) حيث الفئة L لاتيني/أرقام، R عربي، N محايد."""
    toks, pos = [], 0
    for m in _LTR.finditer(text):
        for ch in text[pos:m.start()]:
            toks.append([ch, "R" if _is_ar(ch) else "N"])
        toks.append([m.group(), "L"])
        pos = m.end()
    for ch in text[pos:]:
        toks.append([ch, "R" if _is_ar(ch) else "N"])
    return toks


def reorder(text: str) -> str:
    """الترتيب البصري لفقرة RTL: يعكس التسلسل ويُبقي مقاطع الأرقام/اللاتيني من اليسار لليمين."""
    toks = _tokens(text)
    # المحايدات: بين لاتينيين = لاتيني، وإلا = عربي (اتجاه الفقرة)
    i = 0
    while i < len(toks):
        if toks[i][1] != "N":
            i += 1
            continue
        j = i
        while j < len(toks) and toks[j][1] == "N":
            j += 1
        left = toks[i - 1][1] if i > 0 else "R"
        right = toks[j][1] if j < len(toks) else "R"
        cls = "L" if left == "L" and right == "L" else "R"
        for k in range(i, j):
            toks[k][1] = cls
            if cls == "R" and toks[k][0] in _MIRROR:
                toks[k][0] = _MIRROR[toks[k][0]]
        i = j
    runs = []                                    # دمج المتجاورات من نفس الفئة
    for t, c in toks:
        if runs and runs[-1][1] == c:
            runs[-1][0] += t
        else:
            runs.append([t, c])
    return "".join(t[::-1] if c == "R" else t for t, c in reversed(runs))


def visual(text) -> str:
    """نص جاهز للرسم في PDF: يُرجع النص كما هو إن لم يحوِ عربية."""
    s = "" if text is None else str(text)
    if not has_arabic(s):
        return s
    return reorder(shape(s))
