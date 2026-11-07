"""سجل التدقيق (Audit Trail): من فعل ماذا ومتى وبأي قيمة."""
from datetime import datetime

ENTITY_AR = {
    "supplier": "مورد", "item": "صنف", "purchase_invoice": "فاتورة شراء",
    "supplier_payment": "دفعة مورد", "recipe": "وصفة", "dish": "وجبة",
    "production_batch": "تشغيلة إنتاج", "sale": "مبيعات كاشير", "waste": "هالك",
    "expense": "مصروف", "expense_category": "بند مصروف", "stocktake": "جرد",
    "client": "عميل", "treasury": "حركة خزينة", "warehouse": "مخزن", "transfer": "إذن تحويل", "permission": "صلاحيات", "user": "مستخدم", "session": "جلسة دخول",
}
ACTION_AR = {
    "create": "إضافة", "update": "تعديل", "delete": "حذف",
    "login": "دخول", "logout": "خروج", "login_failed": "محاولة دخول فاشلة",
    "access_denied": "محاولة غير مصرح بها", "approve": "اعتماد", "cancel": "إلغاء",
}


def log(conn, user: str, action: str, entity: str, entity_id=None,
        details: str = "", amount=None, client_id=None) -> None:
    """يُكتب داخل نفس المعاملة (conn) فلا يوجد تعديل بدون أثر مسجل."""
    conn.execute(
        "INSERT INTO audit_log(ts,username,action,entity,entity_id,client_id,details,amount) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), user, action, entity,
         entity_id, client_id, details, None if amount is None else float(amount)),
    )
