"""القاموس الإنجليزي (مصطلحات ERP ومحاسبية) مجمّعًا من أقسامه. المفتاح = النص العربي كما في الكود."""
from utils.lang_en import core, inventory, operations, reports, labels, messages

EN: dict = {}
for _part in (core, inventory, operations, reports, labels, messages):
    EN.update(_part.EN)
