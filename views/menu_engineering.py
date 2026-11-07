from datetime import date, timedelta
import plotly.express as px
import streamlit as st

from utils import theme, auth, reports

COLORS = {"star": theme.GREEN, "plow": theme.GOLD, "puzzle": "#5B6B8C", "dog": "#B3402F"}


def render():
    cid = auth.require_client()
    theme.header("هندسة القائمة", auth.client_name())
    a, b = st.columns(2)
    f = a.date_input("من", date.today() - timedelta(days=30), key="me_f")
    t = b.date_input("إلى", date.today(), key="me_t")
    df, meta = reports.menu_engineering(cid, f, t)
    if df.empty:
        st.info("لا توجد وجبات بعد.")
        return
    if meta["total_qty"] == 0:
        st.info("لا توجد مبيعات في هذه الفترة. التصنيف يحتاج مبيعات.")
        return
    if (df["missing_cost"] > 0).any():
        st.warning("بعض الوجبات بها مكونات بلا تكلفة، فهامشها مبالغ فيه: " + "، ".join(df[df["missing_cost"] > 0]["name"]))

    counts = df["class_key"].value_counts()
    m = st.columns(4)
    for col, key in zip(m, ["star", "plow", "puzzle", "dog"]):
        col.metric(reports.CLASS_AR[key], int(counts.get(key, 0)))
    st.caption(f"الشعبية: حصة الوجبة من إجمالي الكمية المباعة (الحد = {meta['pop_threshold']:.1f}% أي 70% من الحصة المتساوية). "
               f"الربحية: هامش المساهمة للوحدة (الحد = المتوسط المرجح {meta['cm_threshold']:,.2f}).")

    fig = px.scatter(df, x="mix_pct", y="cm", size="qty", color="class_key", text="name", color_discrete_map=COLORS,
                     labels={"mix_pct": "الشعبية (% من المبيعات)", "cm": "هامش المساهمة للوحدة", "class_key": "التصنيف"},
                     title="مصفوفة هندسة القائمة")
    fig.update_traces(textposition="top center")
    try:
        fig.add_vline(x=meta["pop_threshold"], line_dash="dash", line_color="#999")
        fig.add_hline(y=meta["cm_threshold"], line_dash="dash", line_color="#999")
    except Exception:  # noqa: BLE001
        pass
    st.plotly_chart(theme.style_fig(fig), use_container_width=True)

    show = df.sort_values(["class_key", "total_cm"], ascending=[True, False]).rename(columns={
        "name": "الوجبة", "category": "التصنيف", "price": "السعر", "cost": "التكلفة", "qty": "الكمية", "revenue": "الإيراد",
        "cm": "هامش الوحدة", "food_cost_pct": "نسبة التكلفة %", "mix_pct": "الشعبية %", "total_cm": "إجمالي الهامش",
        "class": "الفئة", "advice": "التوصية"})
    st.dataframe(show[["الوجبة", "الفئة", "السعر", "التكلفة", "هامش الوحدة", "نسبة التكلفة %", "الكمية", "الشعبية %", "إجمالي الهامش", "التوصية"]].round(2),
                 use_container_width=True, hide_index=True)
    st.download_button("تنزيل التقرير CSV", show.drop(columns=["dish_id", "class_key", "popular", "profitable", "avg_price", "missing_cost"]).to_csv(index=False).encode("utf-8-sig"),
                       file_name="menu_engineering.csv", mime="text/csv")
