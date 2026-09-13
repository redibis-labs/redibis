"""Call-center transcripts for cases 19–23 (shared by fixture + tests)."""

from __future__ import annotations

CASE_19 = (
    "Agent: مساء الخير، معاك أحمد من قسم المبيعات، أقدر أساعدك إزاي؟\n"
    "Caller: مساء النور، كنت عايز أعمل migrate للخط بتاعي من باقة الكنترول للـ postpaid لو سمحت.\n"
    "Agent: تمام يا فندم، الخط اللي بنكلم عليه 01011223344؟\n"
    "Caller: أيوه بالظبط، واسمي مصطفى كمال محمود.\n"
    "Agent: تمام يا أستاذ مصطفى، هيتم رفع الـ request وهيوصلك رسالة بالتأكيد."
)

CASE_20 = (
    "Agent: بكلم حضرتك بخصوص الـ request الخاص بتغيير الـ router لـ VDSL.\n"
    "Caller: أيوه أنا طلبت router جديد، هو هيوصل إمتى؟\n"
    "Agent: هيوصل بكرا يا فندم، بس محتاجين نأكد الـ delivery address.\n"
    "Caller: العنوان 15 شارع المعادي الجديد، متفرع من شارع النصر، عمارة 4، شقة 12.\n"
    "Agent: تمام، هخلي المندوب يكلمك على رقم زيرو خمستاشر، اتنين اتنين، تلاتة أربعة خمسة، ستة سبعة تمانية.\n"
    "Caller: تمام شكراً."
)

CASE_21 = (
    "Agent: الدعم الفني للكروت والشحن، تحت أمرك.\n"
    "Caller: جبت scratch card بـ 100 جنيه وبحاول أشحنه بيديني invalid code، مش عارف ليه.\n"
    "Agent: ولا يهمك يا فندم، مليني الـ 14 رقم اللي على الكارت لو سمحت.\n"
    "Caller: واحد اتنين تلاتة، أربعة خمسة ستة، سبعة تمانية تسعة، زيرو واحد، اتنين تلاتة أربعة.\n"
    "Agent: والرقم اللي بتشحنله 01233445566؟\n"
    "Caller: مظبوط."
)

CASE_22 = (
    "Agent: أهلاً بيك في خدمة العملاء، إزاي أساعدك؟\n"
    "Caller: التليفون بتاع ابني عمل lock وطالب الـ PUK code عشان دخل الباسورد غلط.\n"
    "Agent: تمام، لتأكيد البيانات، ممكن الـ National ID بتاع صاحب الخط؟\n"
    "Caller: اتنين تسعة زيرو، واحد واحد اتنين اتنين، زيرو واحد، زيرو اتنين، تلاتة أربعة خمسة.\n"
    "Agent: شكراً لحضرتك، الـ PUK code هو 12345678."
)

CASE_23 = (
    "Agent: قسم المتابعة والشكاوى معاك.\n"
    "Caller: أنا بتابع الـ ticket رقم SR-45099، النت عندي بطيء جداً بقاله أسبوع.\n"
    "Agent: ثواني بفتح الـ profile... الخط مسجل باسم ياسمين طارق؟\n"
    "Caller: أيوه أنا.\n"
    "Agent: الـ technical team شغال عليها حالياً في السنترال، ممكن رقم contact تاني عشان نبلغك بالـ update؟\n"
    "Caller: كلموني على زيرو حداشر، خمسة خمسة، زيرو زيرو، اتنين تلاتة، واحد واحد."
)

NEEDLES = {
    "19": [
        ("01011223344", "PHONE_NUMBER"),
        ("مصطفى كمال محمود", "PERSON"),
    ],
    "20": [
        ("15 شارع المعادي الجديد، متفرع من شارع النصر، عمارة 4، شقة 12", "LOCATION"),
        (
            "زيرو خمستاشر، اتنين اتنين، تلاتة أربعة خمسة، ستة سبعة تمانية",
            "PHONE_NUMBER",
        ),
    ],
    "21": [
        (
            "واحد اتنين تلاتة، أربعة خمسة ستة، سبعة تمانية تسعة، زيرو واحد، اتنين تلاتة أربعة",
            "VOUCHER",
        ),
        ("01233445566", "PHONE_NUMBER"),
    ],
    "22": [
        (
            "اتنين تسعة زيرو، واحد واحد اتنين اتنين، زيرو واحد، زيرو اتنين، تلاتة أربعة خمسة",
            "EG_NATIONAL_ID",
        ),
        ("12345678", "SIM_PUK"),
    ],
    "23": [
        ("SR-45099", "SUPPORT_TICKET"),
        ("ياسمين طارق", "PERSON"),
        (
            "زيرو حداشر، خمسة خمسة، زيرو زيرو، اتنين تلاتة، واحد واحد",
            "PHONE_NUMBER",
        ),
    ],
}

TEXTS = {
    "19": CASE_19,
    "20": CASE_20,
    "21": CASE_21,
    "22": CASE_22,
    "23": CASE_23,
}


def _span(text: str, needle: str, entity_type: str) -> dict:
    start = text.index(needle)
    return {"start": start, "end": start + len(needle), "entity_type": entity_type}


def dataset_dict() -> dict:
    cases = []
    for case_id, text in TEXTS.items():
        cases.append({
            "id": f"case-{case_id}",
            "text": text,
            "language": "ar",
            "expected_spans": [
                _span(text, needle, et) for needle, et in NEEDLES[case_id]
            ],
        })
    return {
        "kind": "redibis.text_span_eval_dataset",
        "schema_version": "1.0",
        "offset_unit": "unicode_codepoint",
        "id": "gateway-cases-19-23",
        "cases": cases,
    }
