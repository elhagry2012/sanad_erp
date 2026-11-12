"""استيراد جماعي من Excel/CSV: بيانات الأصناف، الرصيد الافتتاحي/الجرد بالدفعات، المنتجات المصنّعة، والوصفات."""
from datetime import date
import pandas as pd
import streamlit as st

from utils import theme, auth, importer, reports
from utils.export_ui import export_buttons

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ORDER = ["items", "stock", "products", "recipes"]
CARDS = {
    "items": ("📦", "بيانات الأصناف", "الخامات: الاسم، الباركود، وحدة الاستهلاك ووحدة الشراء، حد الطلب، تتبع الصلاحية، "
              "والرصيد الافتتاحي (الكمية، متوسط التكلفة، تاريخ الصلاحية) — صف لكل دفعة."),
    "stock": ("🧮", "الرصيد الافتتاحي / الجرد", "لأصناف موجودة: صف لكل دفعة (الصنف أو الباركود، المخزن، الكمية، متوسط التكلفة، تاريخ الصلاحية). "
              "إضافة كرصيد افتتاحي أو جرد فعلي يستبدل الأرصدة."),
    "products": ("🍲", "المنتجات المصنّعة", "النصف مصنعة: الباركود، وحدة الاستهلاك، كمية الناتج، المكونات وكمياتها، "
                 "ورصيد افتتاحي اختياري بمتوسط تكلفة وتاريخ صلاحية."),
    "recipes": ("🍳", "الوصفات", "مكونات الوجبات: باركود الوجبة، السعر، المكوّن (بالاسم أو الباركود) وكميته ووحدة استهلاكه."),
}
ACTION_AR = {"create": "إضافة", "skip": "تخطي", "exists": "موجودة"}


@st.cache_data(show_spinner=False, max_entries=64)
def _tpl(kind, whs: tuple) -> bytes:
    """القوالب الفارغة لا تتغير إلا بتغير قائمة المخازن؛ تُبنى مرة وتُعاد من الذاكرة."""
    return importer.template(kind, list(whs))


@st.cache_data(show_spinner=False, ttl=30, max_entries=16)
def _count_tpl(cid, whs: tuple, user: str) -> bytes:
    return importer.stock_count_template(cid, list(whs), user=user)


def _templates_section(cid, whs):
    st.subheader("📥 تنزيل القوالب")
    st.caption("نزّل القالب، املأه في Excel، ثم ارفعه في التبويب المناسب أدناه. كل قالب منسّق مسبقًا: التواريخ بصيغة YYYY-MM-DD، "
               "الباركود كنص، قوائم منسدلة لنعم/لا والمخازن، وورقة «تعليمات» بالقواعد. الترتيب الموصى به: "
               "الأصناف ← الرصيد ← المنتجات المصنّعة ← الوصفات.")
    for row in (ORDER[:2], ORDER[2:]):
        cols = st.columns(2)
        for col, kind in zip(cols, row):
            icon, title, desc = CARDS[kind]
            with col.container(border=True):
                st.markdown(f"**{icon} {title}**")
                st.caption(desc)
                st.download_button(f"⬇️ تنزيل قالب «{title}»", _tpl(kind, tuple(whs)), file_name=importer.template_filename(kind),
                                   mime=XLSX, key=f"tpl_top_{kind}", use_container_width=True)
                if kind == "stock":
                    st.download_button("⬇️ قالب جرد مملوء بالأرصدة الحالية", _count_tpl(cid, tuple(whs), auth.username()),
                                       file_name=f"sanad_stock_count_{date.today()}.xlsx", mime=XLSX, key="tpl_top_count",
                                       use_container_width=True, help="صف لكل دفعة حالية (صنف، مخزن، تاريخ صلاحية، الكمية). عدّل الكميات الفعلية وارفعه بوضع «جرد فعلي».")


def _plan_table(kind, rep) -> pd.DataFrame:
    if kind == "items":
        rows = []
        for p in rep.plan:
            base = {"الصف": p["row"], "الصنف": p["name"], "الباركود": p["barcode"] or "", "وحدة الاستهلاك": p["unit"],
                    "يتتبع الصلاحية": "نعم" if p["track"] else "لا", "الإجراء": ACTION_AR[p["action"]]}
            if not p["batches"]:
                rows.append({**base, "المخزن": "", "الكمية": None, "متوسط التكلفة": None, "تاريخ الصلاحية": ""})
            for b in p["batches"]:
                rows.append({**base, "الصف": b["row"], "المخزن": b["wh_name"], "الكمية": b["qty"], "متوسط التكلفة": b["cost"],
                             "تاريخ الصلاحية": str(b["expiry"]) if b["expiry"] else "بدون تاريخ"})
        return pd.DataFrame(rows)
    if kind == "stock":
        return pd.DataFrame([{"الصف": p["row"], "الصنف": p["name"], "المخزن": p["wh_name"], "الكمية": p["qty"], "الوحدة": p["unit"],
                              "بوحدة الاستهلاك": p["base"], "متوسط التكلفة": p["cost"],
                              "تاريخ الصلاحية": str(p["expiry"]) if p["expiry"] else "بدون تاريخ"} for p in rep.plan])
    return pd.DataFrame([{"الصف": p["row"], "الاسم": p["name"], "النوع": "وجبة" if p["kind"] == "dish" else "منتج مصنّع",
                          "الباركود": p["barcode"] or "", "المكونات": len(p["lines"]),
                          "السعر / الناتج": p["price"] if p["kind"] == "dish" else p["yield"],
                          "رصيد افتتاحي": p.get("opening_qty") or None,
                          "تاريخ الصلاحية": str(p["expiry"]) if p.get("expiry") and p.get("opening_qty") else "",
                          "الإجراء": ACTION_AR[p["action"]]} for p in rep.plan])


def _panel(kind, cid, user, whs):
    icon, title, desc = CARDS[kind]
    st.caption(desc)
    st.download_button(f"⬇️ تنزيل قالب «{title}» (.xlsx)", _tpl(kind, tuple(whs)), file_name=importer.template_filename(kind),
                       mime=XLSX, key=f"tpl_{kind}")
    mode, mode_opt = "opening", "error"
    a, b = st.columns(2)
    if kind == "stock":
        mode = a.radio("طريقة التطبيق", ["opening", "count"], key="stock_mode",
                       format_func={"opening": "➕ رصيد افتتاحي (يُضاف للموجود كدفعات جديدة)",
                                    "count": "🧮 جرد فعلي (يستبدل رصيد ودفعات الأصناف المذكورة)"}.get)
        day = b.date_input("تاريخ الجرد" if mode == "count" else "تاريخ الرصيد الافتتاحي", date.today(), key="stock_day")
    else:
        mode_opt = a.radio("إذا كان العنصر موجودًا بالفعل", ["error", "skip"], key=f"mode_{kind}",
                           format_func={"error": "رفض الملف وإظهار الخطأ (موصى به)", "skip": "تخطي الموجود واستيراد الباقي"}.get)
        day = b.date_input("تاريخ الرصيد الافتتاحي", date.today(), key=f"day_{kind}") if kind in ("items", "products") else date.today()
    up = st.file_uploader("ارفع الملف بعد تعبئته (Excel .xlsx أو CSV)", type=["xlsx", "csv"], key=f"up_{kind}")
    if not up:
        return
    data = up.getvalue()
    done = st.session_state.get("import_done", {}).get(kind)
    try:
        rep = importer.preview(kind, cid, data, up.name, user, mode_opt, mode=mode, day=day)
    except importer.ImportFileError as e:
        st.error(f"❌ {e}")
        return
    if done and done[0] == rep.file_sha:
        st.success(f"✅ تم استيراد هذا الملف: {done[1]}. أزل الملف من الأعلى قبل رفع ملف جديد.")
        return

    m = st.columns(5)
    m[0].metric("صفوف البيانات", rep.rows_total)
    m[1].metric("ستُضاف" if kind != "stock" else "صفوف سليمة", rep.created)
    m[2].metric("دفعات بتاريخ صلاحية" if kind != "recipes" else "ستُتخطى", rep.batches if kind != "recipes" else rep.skipped)
    m[3].metric("أخطاء", rep.error_count)
    m[4].metric("تنبيهات", rep.warning_count)

    if rep.error_count:
        st.error(f"❌ يوجد {rep.error_count} خطأ في الملف. **لم يُستورد أي شيء** (الاستيراد كامل أو لا شيء). صحّح الصفوف التالية وأعد رفع الملف:")
    elif rep.created == 0:
        st.warning("لا توجد عناصر جديدة للاستيراد.")
    else:
        st.success("✅ الملف سليم ويمكن استيراده.")
    if rep.issues:
        idf = rep.issues_df()
        idf = idf.assign(_o=idf["النوع"].map({"خطأ": 0, "تنبيه": 1})).sort_values("_o", kind="stable").drop(columns="_o")
        st.dataframe(idf, use_container_width=True, hide_index=True)
        export_buttons(idf, "تقرير أخطاء الاستيراد", f"import_issues_{kind}", subtitle=f"الملف: {up.name}", key=f"iss_{kind}")
    if rep.summary:
        st.markdown("**أثر الجرد على الأرصدة** (لكل صنف ومخزن في الملف)")
        st.dataframe(pd.DataFrame(rep.summary), use_container_width=True, hide_index=True)
    plan = _plan_table(kind, rep)
    if not plan.empty:
        with st.expander("معاينة ما سيُستورد (صف لكل دفعة)", expanded=not rep.error_count):
            st.dataframe(plan, use_container_width=True, hide_index=True)
    if not rep.error_count and rep.created:
        warn = " (مع تنبيهات)" if rep.warning_count else ""
        what = "تطبيق الجرد" if mode == "count" else f"استيراد {rep.created} عنصر"
        if st.button(f"✅ اعتماد {what}{warn}", type="primary", key=f"go_{kind}", use_container_width=True):
            try:
                out = importer.run(kind, cid, data, up.name, user, mode_opt, mode=mode, day=day)
                st.session_state.setdefault("import_done", {})[kind] = (out.file_sha, out.summary_text)
                st.rerun()
            except importer.ImportValidationError as e:        # تغيّرت البيانات بين المعاينة والاعتماد
                st.error(f"❌ {e}")
                st.dataframe(e.report.issues_df(), use_container_width=True, hide_index=True)
            except ValueError as e:
                st.error(str(e))


def render():
    auth.require_admin()                                       # أداة إدارية: المدير العام فقط
    cid, user = auth.require_client(), auth.username()
    theme.header("استيراد البيانات", auth.client_name())
    st.info("الاستيراد **ذري**: يُتحقق من كل الصفوف أولًا، وإن وُجد خطأ واحد لا يُكتب شيء. كل كمية تدخل المخزن تُسجَّل كدفعة "
            "بتاريخ صلاحيتها (نفس منطق فواتير الشراء)، وكل عملية استيراد تُسجَّل باسمك في سجل التدقيق.")
    whs = list(reports.warehouse_list(cid)["name"])
    _templates_section(cid, whs)
    st.divider()
    tabs = st.tabs([f"{CARDS[k][0]} {CARDS[k][1]}" for k in ORDER] + ["🕘 سجل الاستيراد"])
    for t, kind in zip(tabs, ORDER):
        with t:
            _panel(kind, cid, user, whs)
    with tabs[-1]:
        h = importer.history(cid)
        if h.empty:
            st.info("لا توجد عمليات استيراد بعد.")
        else:
            h["kind"] = h["kind"].map(importer.KIND_AR).fillna(h["kind"])
            h = h.rename(columns={"id": "رقم", "created_at": "التاريخ", "kind": "النوع", "filename": "الملف", "rows_total": "الصفوف",
                                  "created_count": "أُضيف", "skipped_count": "تُخطي", "summary": "الملخص", "created_by": "المستخدم"})
            st.dataframe(h, use_container_width=True, hide_index=True)
            export_buttons(h, "سجل الاستيراد الجماعي", "import_history", key="imp_hist")
