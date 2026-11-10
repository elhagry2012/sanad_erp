"""استيراد جماعي من Excel/CSV: الخامات والرصيد الافتتاحي، ومكونات الوصفات (BOM)."""
from datetime import date
import pandas as pd
import streamlit as st

from utils import theme, auth, importer, reports
from utils.export_ui import export_buttons, Section

KIND_AR = {"materials": "الخامات", "recipes": "الوصفات"}


def _plan_table(kind, rep) -> pd.DataFrame:
    if kind == "materials":
        df = pd.DataFrame(rep.plan)
        if df.empty:
            return df
        df["الإجراء"] = df["action"].map({"create": "إضافة", "skip": "تخطي"})
        return df.rename(columns={"row": "الصف", "name": "الصنف", "unit": "وحدة الاستهلاك", "purchase_unit": "وحدة الشراء",
                                  "factor": "المعامل", "min_qty": "حد الطلب", "wh_name": "المخزن", "opening_qty": "الرصيد الافتتاحي",
                                  "unit_cost": "تكلفة الوحدة"})[["الصف", "الصنف", "وحدة الاستهلاك", "وحدة الشراء", "المعامل", "المخزن",
                                                                  "الرصيد الافتتاحي", "تكلفة الوحدة", "الإجراء"]]
    rows = [{"الصف": p["row"], "الوصفة": p["name"], "النوع": "وجبة" if p["kind"] == "dish" else "نصف مصنع",
             "المكونات": len(p["lines"]), "السعر / الناتج": p["price"] if p["kind"] == "dish" else p["yield"],
             "الإجراء": {"create": "إضافة", "skip": "تخطي", "exists": "موجودة"}[p["action"]]} for p in rep.plan]
    return pd.DataFrame(rows)


def _panel(kind, cid, user):
    whs = list(reports.warehouse_list(cid)["name"])
    c1, c2 = st.columns([1, 2])
    tpl = importer.template_materials(whs) if kind == "materials" else importer.template_recipes()
    c1.download_button("📥 تنزيل القالب (.xlsx)", tpl, file_name=f"{kind}_template.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"tpl_{kind}")
    c2.caption("القالب يحتوي ورقة بيانات (عناوين + صف مثال) وورقة «تعليمات» بالقواعد. الصف الذي يبدأ بكلمة «مثال» لا يُستورد.")

    mode = st.radio("إذا كان العنصر موجودًا بالفعل", ["error", "skip"], horizontal=True, key=f"mode_{kind}",
                    format_func={"error": "رفض الملف وإظهار الخطأ (موصى به)", "skip": "تخطي الموجود واستيراد الباقي"}.get)
    opening_date = date.today()
    if kind == "materials":
        opening_date = st.date_input("تاريخ الرصيد الافتتاحي", date.today(), key="open_date")
    up = st.file_uploader("ارفع ملف Excel (.xlsx) أو CSV", type=["xlsx", "csv"], key=f"up_{kind}")
    if not up:
        return
    data = up.getvalue()
    done = st.session_state.get("import_done", {}).get(kind)
    try:
        preview = importer.preview_materials if kind == "materials" else importer.preview_recipes
        rep = preview(cid, data, up.name, user, mode)
    except importer.ImportFileError as e:
        st.error(f"❌ {e}")
        return
    if done and done[0] == rep.file_sha:
        st.success(f"✅ تم استيراد هذا الملف: {done[1]}. أزل الملف من الأعلى قبل رفع ملف جديد.")
        return

    m = st.columns(5)
    m[0].metric("صفوف البيانات", rep.rows_total)
    m[1].metric("ستُضاف", rep.created)
    m[2].metric("ستُتخطى", rep.skipped)
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
    plan = _plan_table(kind, rep)
    if not plan.empty:
        with st.expander("معاينة ما سيُستورد", expanded=not rep.error_count):
            st.dataframe(plan, use_container_width=True, hide_index=True)
    if not rep.error_count and rep.created:
        warn = " (مع تنبيهات)" if rep.warning_count else ""
        if st.button(f"✅ اعتماد استيراد {rep.created} عنصر{warn}", type="primary", key=f"go_{kind}"):
            try:
                if kind == "materials":
                    out = importer.import_materials(cid, data, up.name, user, mode, opening_date)
                    summary = f"{out.created} صنف، قيمة الرصيد الافتتاحي {out.total_value:,.2f}"
                else:
                    out = importer.import_recipes(cid, data, up.name, user, mode)
                    summary = f"{out.created} وصفة"
                st.session_state.setdefault("import_done", {})[kind] = (out.file_sha, summary)
                st.rerun()
            except importer.ImportValidationError as e:        # تغيّرت البيانات بين المعاينة والاعتماد
                st.error(f"❌ {e}")
                st.dataframe(e.report.issues_df(), use_container_width=True, hide_index=True)
            except ValueError as e:
                st.error(str(e))


def render():
    cid, user = auth.require_client(), auth.username()
    theme.header("استيراد البيانات", auth.client_name())
    st.info("الاستيراد **ذري**: يُتحقق من كل الصفوف أولًا، وإن وُجد خطأ واحد لا يُكتب شيء. الترتيب الموصى به: الخامات والرصيد الافتتاحي ثم الوصفات. "
            "كل عملية استيراد تُسجَّل باسمك في سجل التدقيق.")
    t1, t2, t3 = st.tabs(["📦 الخامات والمخزون الافتتاحي", "🍳 الوصفات (BOM)", "🕘 سجل الاستيراد"])
    with t1:
        _panel("materials", cid, user)
    with t2:
        _panel("recipes", cid, user)
    with t3:
        h = importer.history(cid)
        if h.empty:
            st.info("لا توجد عمليات استيراد بعد.")
        else:
            h["kind"] = h["kind"].map(KIND_AR)
            h = h.rename(columns={"id": "رقم", "created_at": "التاريخ", "kind": "النوع", "filename": "الملف", "rows_total": "الصفوف",
                                  "created_count": "أُضيف", "skipped_count": "تُخطي", "summary": "الملخص", "created_by": "المستخدم"})
            st.dataframe(h, use_container_width=True, hide_index=True)
            export_buttons(h, "سجل الاستيراد الجماعي", "import_history", key="imp_hist")
