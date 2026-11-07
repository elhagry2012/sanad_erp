"""بيانات تجريبية لعميل واحد لتجربة لوحة التحكم. التشغيل: python -m database.seed"""
import random
from datetime import date, timedelta
from database.db import init_db, get_conn

random.seed(7)


def seed() -> None:
    init_db()
    with get_conn() as c:
        if c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]:
            print("البيانات موجودة بالفعل.")
            return
        cid = c.execute("INSERT INTO clients(name,cuisine) VALUES('مطعم الكريب النموذجي','كريب ووجبات سريعة')").lastrowid

        # (الاسم، الوحدة، نصف مصنع، السعر السابق، السعر الحالي)
        items = [
            ("دقيق", "كجم", 0, 18, 22), ("لبن", "لتر", 0, 30, 34), ("بيض", "قطعة", 0, 3.2, 4.0),
            ("صدور دجاج", "كجم", 0, 150, 175), ("جبنة موزاريلا", "كجم", 0, 200, 210),
            ("زيت", "لتر", 0, 55, 62), ("ثوم", "كجم", 0, 80, 70), ("عجينة كريب", "كجم", 1, 0, 0),
        ]
        ids = {}
        for n, u, semi, old, new in items:
            ids[n] = c.execute(
                "INSERT INTO items(client_id,name,unit,is_semi,qty_on_hand,avg_cost,last_price) VALUES(?,?,?,?,?,?,?)",
                (cid, n, u, semi, 50, new, new)).lastrowid
            if not semi:
                c.execute("INSERT INTO price_history(item_id,price_date,unit_price) VALUES(?,?,?)",
                          (ids[n], str(date.today() - timedelta(days=60)), old))
                c.execute("INSERT INTO price_history(item_id,price_date,unit_price) VALUES(?,?,?)",
                          (ids[n], str(date.today() - timedelta(days=3)), new))

        # تكلفة عجينة الكريب = 0.4 دقيق + 0.6 لبن + 4 بيض لكل كجم
        dough = 0.4 * 22 + 0.6 * 34 + 4 * 4.0
        c.execute("UPDATE items SET avg_cost=?, last_price=?, qty_on_hand=20 WHERE id=?", (dough, dough, ids["عجينة كريب"]))

        r = c.execute("INSERT INTO recipes(client_id,name,kind,output_item_id,yield_qty) VALUES(?,?,?,?,1)",
                      (cid, "عجينة كريب", "semi", ids["عجينة كريب"])).lastrowid
        for n, q in [("دقيق", .4), ("لبن", .6), ("بيض", 4)]:
            c.execute("INSERT INTO recipe_lines(recipe_id,item_id,qty) VALUES(?,?,?)", (r, ids[n], q))

        dishes = [
            ("كريب دجاج", "كريب", 95, [("عجينة كريب", .12), ("صدور دجاج", .10), ("جبنة موزاريلا", .04)], 55),
            ("كريب جبنة", "كريب", 70, [("عجينة كريب", .12), ("جبنة موزاريلا", .08)], 40),
            ("كريب ثوم وبيض", "كريب", 60, [("عجينة كريب", .12), ("بيض", 1), ("ثوم", .01)], 25),
            ("ستربس دجاج", "وجبات", 120, [("صدور دجاج", .20), ("دقيق", .05), ("زيت", .10)], 30),
            ("صوص ثومية", "صوصات", 15, [("ثوم", .03), ("زيت", .08)], 90),
        ]
        dish_ids = []
        for name, cat, price, lines, base in dishes:
            rid = c.execute("INSERT INTO recipes(client_id,name,kind) VALUES(?,?,'dish')", (cid, name)).lastrowid
            for n, q in lines:
                c.execute("INSERT INTO recipe_lines(recipe_id,item_id,qty) VALUES(?,?,?)", (rid, ids[n], q))
            did = c.execute("INSERT INTO dishes(client_id,name,category,price,recipe_id) VALUES(?,?,?,?,?)",
                            (cid, name, cat, price, rid)).lastrowid
            dish_ids.append((did, price, base))

        for d in range(90):
            day = date.today() - timedelta(days=d)
            for did, price, base in dish_ids:
                q = max(0, int(random.gauss(base, base * .25)))
                c.execute("INSERT INTO pos_sales(client_id,dish_id,sale_date,qty,unit_price) VALUES(?,?,?,?,?)",
                          (cid, did, str(day), q, price))

        cats = [("إيجار", "fixed"), ("مرتبات", "fixed"), ("كهرباء ومرافق", "variable"),
                ("تسويق", "variable"), ("صيانة", "variable")]
        cat_ids = {n: c.execute("INSERT INTO expense_categories(name,nature) VALUES(?,?)", (n, k)).lastrowid for n, k in cats}
        for m in range(3):
            day = date.today().replace(day=1) - timedelta(days=30 * m)
            for n, amt in [("إيجار", 15000), ("مرتبات", 42000), ("كهرباء ومرافق", 6500),
                           ("تسويق", 4000), ("صيانة", 1800)]:
                c.execute("INSERT INTO expenses(client_id,category_id,exp_date,amount) VALUES(?,?,?,?)",
                          (cid, cat_ids[n], str(day), amt))
        c.commit()
        c.commit()
    init_db()   # يُنشئ المخازن الافتراضية ويسند الأرصدة للمخزن الرئيسي
    print("تم إنشاء البيانات التجريبية.")


if __name__ == "__main__":
    seed()
