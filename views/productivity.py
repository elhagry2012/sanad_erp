"""تقرير إنتاجية الموظفين - للمدير العام (momar) فقط."""
from datetime import date, timedelta
import plotly.express as px
import streamlit as st

from database.db import query
from utils.export_ui import export_buttons, Section, FMT_QTY, FMT_MONEY, FMT_COST
from utils import theme, auth
from utils.audit import ENTITY_AR, ACTION_AR


def render():
    auth.require_admin()
    theme.header("إنتاجية الموظفين", "حجم الشغل وكل عملية نفّذها كل مستخدم")

    c1, c2 = st.columns(2)
    start = c1.date_input("من", date.today() - timedelta(days=30))
    end = c2.date_input("إلى", date.today())
    df = query("""SELECT id, ts, username, action, entity, entity_id, details, amount, client_id
                  FROM audit_log WHERE date(ts) BETWEEN ? AND ? ORDER BY ts DESC""", (str(start), str(end)))
    if df.empty:
        st.info("لا يوجد نشاط في هذه الفترة.")
        return
    df["العملية"] = df["action"].map(ACTION_AR).fillna(df["action"])
    df["النوع"] = df["entity"].map(ENTITY_AR).fillna(df["entity"])
    work = df[~df["action"].isin(["login", "logout", "login_failed", "access_denied"])]

    st.subheader("ملخص كل مستخدم")
    summ = work.groupby("username").agg(
        العمليات=("id", "count"),
        إضافات=("action", lambda s: (s == "create").sum()),
        تعديلات=("action", lambda s: (s == "update").sum()),
        حذف=("action", lambda s: (s == "delete").sum()),
        القيمة_المالية=("amount", "sum"),
        آخر_نشاط=("ts", "max"),
    ).reset_index()
    logins = df[df["action"] == "login"].groupby("username").size().rename("مرات الدخول")
    fails = df[df["action"] == "login_failed"].groupby("username").size().rename("محاولات فاشلة")
    denied = df[df["action"] == "access_denied"].groupby("username").size().rename("محاولات غير مصرح بها")
    summ = summ.merge(logins, on="username", how="left").merge(fails, on="username", how="left").merge(
        denied, on="username", how="left").fillna(0)
    summ["محاولات غير مصرح بها"] = summ["محاولات غير مصرح بها"].astype(int)
    summ["مرات الدخول"] = summ["مرات الدخول"].astype(int)
    summ["محاولات فاشلة"] = summ["محاولات فاشلة"].astype(int)
    st.dataframe(summ.rename(columns={"username": "المستخدم", "القيمة_المالية": "القيمة المالية للعمليات",
                                      "آخر_نشاط": "آخر نشاط"}).round(0), use_container_width=True, hide_index=True)

    l, r = st.columns(2)
    by_user = work.groupby("username", as_index=False).size().rename(columns={"size": "عدد العمليات"})
    fig = px.bar(by_user, x="username", y="عدد العمليات", title="عدد العمليات لكل مستخدم", labels={"username": ""})
    fig.update_traces(marker_color=theme.GREEN)
    l.plotly_chart(theme.style_fig(fig), use_container_width=True)
    daily = work.assign(day=work["ts"].str[:10]).groupby(["day", "username"], as_index=False).size()
    r.plotly_chart(theme.style_fig(px.bar(daily, x="day", y="size", color="username", title="النشاط اليومي",
                                          labels={"day": "اليوم", "size": "العمليات", "username": "المستخدم"})),
                   use_container_width=True)

    st.subheader("نوع العمل لكل مستخدم")
    st.dataframe(work.pivot_table(index="username", columns="النوع", values="id", aggfunc="count", fill_value=0)
                 .reset_index().rename(columns={"username": "المستخدم"}), use_container_width=True, hide_index=True)

    st.subheader("سجل العمليات التفصيلي")
    f1, f2 = st.columns(2)
    users = sorted(df["username"].unique())
    pick_u = f1.multiselect("المستخدم", users)
    pick_t = f2.multiselect("نوع العملية", sorted(df["النوع"].unique()))
    det = df
    if pick_u:
        det = det[det["username"].isin(pick_u)]
    if pick_t:
        det = det[det["النوع"].isin(pick_t)]
    show = det[["ts", "username", "العملية", "النوع", "entity_id", "details", "amount"]].rename(columns={
        "ts": "الوقت", "username": "المستخدم", "entity_id": "رقم السجل", "details": "التفاصيل", "amount": "القيمة"})
    st.dataframe(show, use_container_width=True, hide_index=True)
    export_buttons(show, "سجل عمليات المستخدمين (Audit Trail)", "audit_trail", subtitle=f"من {start} إلى {end}", formats={"القيمة": FMT_MONEY}, key="audit_exp")
