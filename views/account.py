import streamlit as st
from utils import theme, auth, security


def render():
    theme.header("حسابي", f"المستخدم: {auth.username()}")
    st.caption("سياسة كلمة المرور: 10 أحرف على الأقل، حروف وأرقام، وغير متوقعة. عند التغيير تُنهى جلساتك الأخرى.")
    with st.form("pw", clear_on_submit=True):
        old = st.text_input("كلمة المرور الحالية", type="password")
        new = st.text_input("كلمة المرور الجديدة", type="password")
        new2 = st.text_input("تأكيد كلمة المرور الجديدة", type="password")
        if st.form_submit_button("تغيير كلمة المرور"):
            if new != new2:
                st.error("كلمتا المرور غير متطابقتين.")
            else:
                try:
                    security.change_own_password(auth.username(), old, new, keep_token=auth.current_token())
                    st.success("تم تغيير كلمة المرور.")
                except ValueError as e:
                    st.error(str(e))
