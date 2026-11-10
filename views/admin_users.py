"""المستخدمون والصلاحيات والأمان - للمدير العام فقط (momar)."""
import pandas as pd
import streamlit as st

from database import db
from database.db import query
from utils import theme, auth, security
from utils.audit import ACTION_AR


def _access_summary(r) -> str:
    mods = "كل الوحدات" if r["all_modules"] else "وحدات محددة"
    cl = "كل المطاعم" if r["all_clients"] else "مطاعم محددة"
    return f"{mods} / {cl}"


def render():
    auth.require_admin()
    me = auth.username()
    theme.header("المستخدمون والصلاحيات", "المدير العام فقط: إدارة الحسابات والصلاحيات وكلمات المرور والجلسات")
    clients = query("SELECT id, name FROM clients ORDER BY name")
    t1, t2, t3, t4, t5 = st.tabs(["الحسابات", "الصلاحيات والمطاعم", "كلمات المرور والجلسات", "سجل الأمان", "النسخ الاحتياطي"])

    # ================= الحسابات =================
    with t1:
        users = security.list_users()
        show = users.copy()
        show["الدور"] = show["role"].map({"admin": "مدير عام", "partner": "شريك / موظف"})
        show["الحالة"] = show["is_active"].map({1: "مفعّل", 0: "معطّل"})
        show["الوصول"] = show.apply(_access_summary, axis=1)
        show["الدور الوظيفي"] = show["job_role"].map(lambda k: security.ROLE_PRESETS.get(k, {}).get("label", "—"))
        show["تغيير إجباري"] = show["must_change_password"].map({1: "نعم", 0: ""})
        st.dataframe(show[["username", "full_name", "الدور", "الدور الوظيفي", "الحالة", "الوصول", "تغيير إجباري", "password_changed_at",
                           "last_login_at", "created_by"]].rename(columns={
            "username": "المستخدم", "full_name": "الاسم", "password_changed_at": "آخر تغيير لكلمة المرور",
            "last_login_at": "آخر دخول", "created_by": "أنشأه"}), use_container_width=True, hide_index=True)
        st.caption("كلمات المرور مشفّرة ولا يمكن لأحد، ولا حتى المدير، عرضها. يمكنك إعادة تعيينها من تبويب «كلمات المرور والجلسات».")

        st.subheader("إنشاء حساب جديد")
        with st.form("new_user", clear_on_submit=True):
            c1, c2 = st.columns(2)
            uname = c1.text_input("اسم المستخدم (إنجليزي صغير)")
            full = c2.text_input("الاسم الكامل")
            gen = st.checkbox("توليد كلمة مرور مؤقتة تلقائيًا (موصى به)", value=True)
            pw = st.text_input("أو أدخل كلمة مرور ابتدائية", type="password")
            role_labels = {k: v["label"] for k, v in security.ROLE_PRESETS.items()}
            role_key = st.selectbox("الدور الوظيفي (يحدد الشاشات والتقارير الظاهرة، ويمكنك تعديلها بعد الإنشاء)", [None] + list(role_labels),
                                    format_func=lambda k: "— بدون دور: وصول كامل أقيّده لاحقًا —" if k is None else role_labels[k])
            st.caption("الحساب الجديد يبدأ بوصول كامل (كالسابق)، وتقيّده من تبويب «الصلاحيات والمطاعم». سيُطلب من المستخدم تغيير كلمة المرور عند أول دخول.")
            if st.form_submit_button("إنشاء الحساب"):
                try:
                    password = security.generate_password() if (gen or not pw) else pw
                    security.create_user(uname, full, password, me, role=role_key)
                    st.session_state["new_user_pw"] = (uname.strip().lower(), password)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        if st.session_state.get("new_user_pw"):
            u, p = st.session_state.pop("new_user_pw")
            st.success(f"تم إنشاء الحساب «{u}». كلمة المرور المؤقتة (تظهر مرة واحدة فقط، سلّمها لصاحبها بشكل خاص):")
            st.code(p)

    # ================= الصلاحيات =================
    with t2:
        others = users[users["username"] != security.ADMIN_USERNAME]
        if others.empty:
            st.info("لا توجد حسابات أخرى بعد.")
        else:
            target = st.selectbox("الحساب", others["username"], key="perm_user")
            row = others[others["username"] == target].iloc[0]
            acc = security.get_access(target)
            cur_role = row.get("job_role")
            st.markdown(f"**الدور الحالي:** {security.ROLE_PRESETS.get(cur_role, {}).get('label', 'غير محدد (مخصص)')}")
            rc1, rc2 = st.columns([2, 1])
            new_role = rc1.selectbox("تطبيق دور جاهز (يستبدل صلاحيات الوحدات، ولا يغيّر المطاعم المسموحة)", list(security.ROLE_PRESETS),
                                     format_func=lambda k: security.ROLE_PRESETS[k]["label"], key=f"role_{target}")
            if rc2.button("تطبيق الدور", key=f"role_btn_{target}"):
                security.apply_role(target, new_role, me)
                st.success("تم تطبيق الدور.")
                st.rerun()
            st.caption("الأدوار: كاشير (مبيعات فقط)، مدير مطعم (التشغيل + تقارير المخزون والإدارة)، مدير مالي (الخزينة والمصروفات + التقارير المالية والضريبية)، مدير عام غير Admin (كل الوحدات عدا الاستيراد).")
            st.markdown("**الوحدات المسموحة**")
            all_m = st.checkbox("كل الوحدات (عرض وتعديل)", value=bool(row["all_modules"]), key=f"allm_{target}")
            grid = pd.DataFrame([{"الوحدة": name, "key": k,
                                  "عرض": acc["all_modules"] or acc["modules"].get(k, (False, False))[0],
                                  "تعديل": acc["all_modules"] or acc["modules"].get(k, (False, False))[1]}
                                 for k, name in security.MODULES.items() if k not in security.SENSITIVE_MODULES])
            ed = st.data_editor(grid, hide_index=True, use_container_width=True, disabled=all_m or ["الوحدة", "key"],
                                column_config={"key": None, "عرض": st.column_config.CheckboxColumn(),
                                               "تعديل": st.column_config.CheckboxColumn()}, key=f"permgrid_{target}")
            st.caption("«تعديل» يتضمن «عرض». وحدة بدون عرض لا تظهر للمستخدم أصلًا، وتُرفض عملياتها في الخدمات أيضًا.")
            imp_now = acc["explicit"].get("import", (False, False))[1]
            imp_on = st.checkbox("السماح بوحدة «الاستيراد الجماعي للبيانات» (حساسة: لا تشملها «كل الوحدات»، تُمنح بالاسم فقط)",
                                 value=imp_now, key=f"imp_{target}")
            st.markdown("**المطاعم المسموحة**")
            all_c = st.checkbox("كل المطاعم", value=bool(row["all_clients"]), key=f"allc_{target}")
            names = list(clients["name"])
            default = [n for n, i in zip(clients["name"], clients["id"]) if int(i) in acc["clients"]]
            chosen = st.multiselect("اختر المطاعم", names, default=default, disabled=all_c, key=f"cl_{target}")
            if st.button("حفظ الصلاحيات", type="primary"):
                modules = {r["key"]: (bool(r["عرض"]) or bool(r["تعديل"]), bool(r["تعديل"])) for _, r in ed.iterrows()}
                modules["import"] = (imp_on, imp_on)
                ids = [int(clients.loc[clients["name"] == n, "id"].iloc[0]) for n in chosen]
                if not all_c and not ids:
                    st.error("اختر مطعمًا واحدًا على الأقل، أو فعّل «كل المطاعم».")
                else:
                    try:
                        security.set_permissions(target, me, all_m, modules, all_c, ids)
                        st.success("تم حفظ الصلاحيات (تسري فورًا على الجلسات الحالية).")
                    except ValueError as e:
                        st.error(str(e))

    # ================= كلمات المرور والجلسات =================
    with t3:
        others = users[users["username"] != security.ADMIN_USERNAME]
        if others.empty:
            st.info("لا توجد حسابات أخرى.")
        else:
            target = st.selectbox("الحساب", others["username"], key="pw_user")
            active = bool(others.loc[others["username"] == target, "is_active"].iloc[0])
            c1, c2, c3 = st.columns(3)
            if c1.button("توليد كلمة مرور مؤقتة"):
                try:
                    st.session_state["temp_pw"] = (target, security.reset_password(target, me))
                except ValueError as e:
                    st.error(str(e))
            if c2.button("إنهاء كل جلساته"):
                security.revoke_sessions(target, me)
                st.success("تم إنهاء الجلسات.")
            if c3.button("تعطيل الحساب" if active else "تفعيل الحساب"):
                security.set_active(target, not active, me)
                st.rerun()
            if st.session_state.get("temp_pw"):
                u, p = st.session_state.pop("temp_pw")
                st.success(f"كلمة المرور المؤقتة لـ «{u}» (تظهر مرة واحدة، وسيُجبَر على تغييرها عند الدخول، وأُنهيت جلساته):")
                st.code(p)
            with st.form("set_pw", clear_on_submit=True):
                newpw = st.text_input("أو حدّد كلمة مرور بنفسك", type="password")
                if st.form_submit_button("تعيين"):
                    try:
                        security.reset_password(target, me, newpw)
                        st.success("تم التعيين، وسيُطلب من المستخدم تغييرها عند الدخول.")
                    except ValueError as e:
                        st.error(str(e))
        st.subheader("الجلسات النشطة الآن")
        st.dataframe(security.active_sessions().rename(columns={"username": "المستخدم", "created_at": "بدأت", "last_seen": "آخر نشاط",
                                                                  "expires_at": "تنتهي"}), use_container_width=True, hide_index=True)

    # ================= سجل الأمان =================
    with t4:
        ev = query("""SELECT ts, username, action, entity, details FROM audit_log WHERE action IN
                      ('login','logout','login_failed','access_denied') OR entity IN ('user','permission')
                      ORDER BY id DESC LIMIT 500""")
        ev["الحدث"] = ev["action"].map(ACTION_AR).fillna(ev["action"])
        kind = st.multiselect("نوع الحدث", sorted(ev["الحدث"].unique()))
        if kind:
            ev = ev[ev["الحدث"].isin(kind)]
        st.dataframe(ev[["ts", "username", "الحدث", "details"]].rename(columns={"ts": "الوقت", "username": "المستخدم", "details": "التفاصيل"}),
                     use_container_width=True, hide_index=True)

    # ================= النسخ الاحتياطي =================
    with t5:
        st.write(f"تُحفظ نسخة تلقائية يوميًا داخل `{db.BACKUP_DIR}` (آخر {db.KEEP_BACKUPS} نسخة). صور الفواتير في `{db.IMAGES_DIR}` وتُنسخ منفصلة.")
        if st.button("إنشاء نسخة احتياطية الآن"):
            path = db.backup_db(force=True)
            st.success(f"تم الحفظ: {path.name}")
        if db.DB_PATH.exists():
            st.download_button("تنزيل قاعدة البيانات الحالية", db.DB_PATH.read_bytes(), file_name="sanad.db",
                               mime="application/octet-stream")
