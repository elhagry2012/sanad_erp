"""معادلات SQL مشتركة للأرصدة (مصدر واحد لتفادي اختلاف الأرقام بين الشاشات والتقارير)."""

# الرصيد المستحق للمورد = أول المدة + صافي الفواتير النشطة (بعد خصم ضريبة الخصم) - المدفوع - الدفعات
#                       - الإشعارات الدائنة للمرتجعات النشطة + المبالغ المستردة من المورد
SUPPLIER_BALANCE = """(s.opening_balance
    + COALESCE((SELECT SUM(total - COALESCE(wht_amount,0)) FROM purchase_invoices WHERE supplier_id=s.id AND COALESCE(status,'active')='active'),0)
    - COALESCE((SELECT SUM(paid) FROM purchase_invoices WHERE supplier_id=s.id AND COALESCE(status,'active')='active'),0)
    - COALESCE((SELECT SUM(amount) FROM supplier_payments WHERE supplier_id=s.id),0)
    - COALESCE((SELECT SUM(net_credit) FROM purchase_returns WHERE supplier_id=s.id AND status='active'),0)
    + COALESCE((SELECT SUM(refund_amount) FROM purchase_returns WHERE supplier_id=s.id AND status='active'),0))"""
