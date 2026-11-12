"""سند للأعمال كتطبيق ويب قابل للتثبيت (PWA) على الجوال واللوحي والحاسوب.

وضعان:

1) الوضع الافتراضي — تلقائي وداخل مجلد المشروع فقط (لا يلمس حزمة Streamlit المثبّتة):
   build_app_static() يولّد في H:/.../static/sanad-pwa/ الأيقونات و manifest.json وصفحة «لا يوجد اتصال».
   Streamlit يقدّم مجلد static عبر /app/static/ (server.enableStaticServing في .streamlit/config.toml)،
   و utils/theme.pwa_head() يضيف إلى <head> رابط الـ manifest ووسوم iOS/Android عند فتح كل صفحة.
   هذا يكفي لزر «تثبيت التطبيق» في Chrome/Edge/Samsung Internet الحديثة و«إضافة إلى الشاشة الرئيسية» في iOS.

2) الوضع الكامل — اختياري ويُشغَّل صراحة فقط (مثلًا خطوة بناء في Dockerfile على نسخة الحاوية من Streamlit):
   install() يضيف أيضًا Service Worker على مستوى الموقع كله (لصفحة «لا يوجد اتصال» عند انقطاع الشبكة، وللمتصفحات
   الأقدم التي تشترط Service Worker للتثبيت). يعدّل index.html داخل حزمة Streamlit بين علامتين ويُزال بـ --remove.
   التطبيق نفسه لا يشغّل هذا الوضع أبدًا.

   python -m tools.pwa_setup --full       |   python -m tools.pwa_setup --remove
"""
import base64
import hashlib
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "pwa"
LOGO = ROOT / "assets" / "logo.png"
APP_STATIC = ROOT / "static"
NAME, SHORT = "سند للأعمال", "سند"
NAVY, WHITE = "#0A1F44", "#FFFFFF"
START, END = "<!-- sanad-pwa:start -->", "<!-- sanad-pwa:end -->"
DESCRIPTION = "نظام سند للأعمال لإدارة المطاعم: المشتريات والمخزون بالدفعات والتصنيع والمبيعات والخزينة."


def _icon(size: int, pad: float) -> bytes:
    """أيقونة مربعة بخلفية بيضاء والشعار في الوسط. pad = نسبة الهامش (maskable يحتاج منطقة آمنة 80%)."""
    from PIL import Image
    logo = Image.open(LOGO).convert("RGBA")
    canvas = Image.new("RGBA", (size, size), WHITE)
    inner = int(size * (1 - 2 * pad))
    logo.thumbnail((inner, inner), Image.LANCZOS)
    canvas.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _manifest(root_rel: str) -> dict:
    """root_rel = مسار جذر التطبيق نسبةً لمجلد الـ manifest (مثل ../../../ عند /app/static/sanad-pwa/)."""
    return {
        "id": root_rel, "name": NAME, "short_name": SHORT, "description": DESCRIPTION, "lang": "ar", "dir": "rtl",
        "start_url": root_rel, "scope": root_rel, "display": "standalone", "display_override": ["standalone", "minimal-ui"],
        "orientation": "any", "background_color": WHITE, "theme_color": NAVY, "categories": ["business", "productivity", "finance"],
        "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  {"src": "maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }


def _write_assets(out: Path, root_rel: str) -> str:
    """يكتب الأيقونات والـ manifest وصفحة عدم الاتصال في out، ويرجع بصمة الإصدار. لا يعيد الكتابة إن لم يتغير شيء."""
    icons = {"icon-192.png": _icon(192, .08), "icon-512.png": _icon(512, .08), "maskable-512.png": _icon(512, .18),
             "apple-touch-icon.png": _icon(180, .08)}
    offline = (SRC / "offline.html").read_text(encoding="utf-8").replace(
        "__ICON__", "data:image/png;base64," + base64.b64encode(icons["icon-192.png"]).decode())
    files = {**icons, "manifest.json": json.dumps(_manifest(root_rel), ensure_ascii=False, indent=2).encode("utf-8"),
             "offline.html": offline.encode("utf-8")}
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        f = out / name
        if not f.exists() or f.read_bytes() != data:
            f.write_bytes(data)
    return hashlib.sha256(b"".join(files[k] for k in sorted(files))).hexdigest()[:10]


def build_app_static() -> str:
    """الوضع الافتراضي: الملفات في static/sanad-pwa داخل المشروع (تُقدَّم عبر /app/static/sanad-pwa/)."""
    return _write_assets(APP_STATIC / "sanad-pwa", "../../../")


# ============================================================ الوضع الكامل (اختياري، صريح)
def streamlit_static_dir() -> Path:
    import streamlit
    return Path(streamlit.__file__).resolve().parent / "static"


def _head_block(version: str) -> str:
    return f"""{START}
    <link rel="manifest" href="./sanad-pwa/manifest.json?v={version}" />
    <meta name="theme-color" content="{NAVY}" />
    <meta name="mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
    <meta name="apple-mobile-web-app-title" content="{SHORT}" />
    <meta name="application-name" content="{NAME}" />
    <link rel="apple-touch-icon" href="./sanad-pwa/apple-touch-icon.png?v={version}" />
    <script>
      if ("serviceWorker" in navigator) {{
        window.addEventListener("load", function () {{
          navigator.serviceWorker.register("./sanad-sw.js?v={version}", {{ scope: "./" }}).catch(function () {{}});
        }});
      }}
    </script>
    {END}"""


def _strip(html: str) -> str:
    return re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\s*", "", html, flags=re.S)


def install(quiet: bool = False, target: Path | None = None) -> str:
    """الوضع الكامل: يعدّل index.html في مجلد static الخاص بـ Streamlit (target للاختبار على نسخة)."""
    st_dir = target or streamlit_static_dir()
    index = st_dir / "index.html"
    if not index.exists():
        raise FileNotFoundError(f"Streamlit index.html not found: {index}")
    version = _write_assets(st_dir / "sanad-pwa", "../")
    sw = (SRC / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", version)
    (st_dir / "sanad-sw.js").write_text(sw, encoding="utf-8")
    html = index.read_text(encoding="utf-8")
    if f"manifest.json?v={version}" in html:
        msg = f"PWA (full mode) already installed (version {version})."
    else:
        new = _strip(html)
        new = re.sub(r'<html lang="[^"]*"', '<html lang="ar"', new, count=1)
        new = new.replace('content="width=device-width, initial-scale=1, shrink-to-fit=no"',
                          'content="width=device-width, initial-scale=1, shrink-to-fit=no, viewport-fit=cover"', 1)
        new = re.sub(r"<title>[^<]*</title>", f"<title>{NAME}</title>", new, count=1)
        new = new.replace("<head>", "<head>\n    " + _head_block(version), 1)
        index.write_text(new, encoding="utf-8")
        msg = f"PWA (full mode) installed (version {version}) into {st_dir}"
    if not quiet:
        print(msg)
    return msg


def remove(target: Path | None = None) -> str:
    st_dir = target or streamlit_static_dir()
    index = st_dir / "index.html"
    index.write_text(_strip(index.read_text(encoding="utf-8")), encoding="utf-8")
    (st_dir / "sanad-sw.js").unlink(missing_ok=True)
    d = st_dir / "sanad-pwa"
    if d.exists():
        for f in d.glob("*"):
            f.unlink()
        d.rmdir()
    return "PWA (full mode) removed."


if __name__ == "__main__":
    if "--remove" in sys.argv:
        print(remove())
    elif "--full" in sys.argv:
        print(install(quiet=True))
    else:
        print(f"App static PWA files built (version {build_app_static()}) in {APP_STATIC / 'sanad-pwa'}")
