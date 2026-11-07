import streamlit as st
from database.db import query
from utils import theme, auth, services


def render():
    user = auth.username()
    theme.header("العملاء", "المطاعم التي تديرها الشركة")
    with st.form("new_client", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("اسم المطعم")
        cuisine = c2.text_input("النشاط (كريب، وجبات سريعة...)")
        c3, c4 = st.columns(2)
        opening = c3.number_input("رصيد الخزينة النقدية الافتتاحي", step=100.0)
        opening_bank = c4.number_input("رصيد البنك الافتتاحي", step=100.0)
        if st.form_submit_button("إضافة عميل"):
            try:
                services.add_client(name, cuisine, user, opening, opening_bank)
                st.success("تمت الإضافة (مع مخزن رئيسي ومخزن تشغيل)، وهو يظهر الآن في قائمة المطاعم بشاشة الدخول.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    df = query("SELECT id, name, cuisine FROM clients ORDER BY id")
    if not df.empty:
        st.caption("يمكنك تعديل الاسم والنشاط مباشرة ثم الضغط على حفظ.")
        edited = st.data_editor(df, hide_index=True, disabled=["id"], key="ed_clients", use_container_width=True,
                                column_config={"id": None, "name": "الاسم", "cuisine": "النشاط"})
        if st.button("حفظ التعديلات", key="sv_clients"):
            try:
                n = services.apply_edits("clients", df, edited, user)
                st.success(f"تم حفظ {n} تعديل.") if n else st.info("لا توجد تعديلات.")
            except ValueError as e:
                st.error(str(e))
