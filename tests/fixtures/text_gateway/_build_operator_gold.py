"""Build operator goldens with unique str.find offsets. Not collected by pytest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TYPE_MAP = (
    ("passport", "PASSPORT"),
    ("tax id", "EG_TAX_ID"),
    ("company", "ORGANIZATION"),
    ("organization", "ORGANIZATION"),
    ("medical", "GDPR_SPECIAL_CATEGORY"),
    ("health", "GDPR_SPECIAL_CATEGORY"),
    ("phi", "GDPR_SPECIAL_CATEGORY"),
    ("imei", "IMEI"),
    ("otp", "OTP"),
    ("puk", "SIM_PUK"),
    ("voucher", "VOUCHER"),
    ("scratch", "VOUCHER"),
    ("ticket", "SUPPORT_TICKET"),
    ("email", "EMAIL_ADDRESS"),
    ("cvv", "CVV"),
    ("cvc", "CVV"),
    ("expir", "CREDIT_CARD_EXPIRATION"),
    ("credit card number", "CREDIT_CARD"),
    ("pan", "CREDIT_CARD"),
    ("national id", "EG_NATIONAL_ID"),
    ("ip address", "IP_ADDRESS"),
    ("landmark", "LOCATION"),
    ("physical address", "LOCATION"),
    ("location", "LOCATION"),
    ("wallet", "PHONE_NUMBER"),
    ("msisdn", "PHONE_NUMBER"),
    ("phone", "PHONE_NUMBER"),
    ("agent name", "PERSON"),
    ("full name", "PERSON"),
)


def catalog_type(raw: str) -> str:
    blob = (raw or "").lower()
    for needle, entity in TYPE_MAP:
        if needle in blob:
            return entity
    raise SystemExit(f"unmapped type: {raw!r}")


def unique_span(text: str, value: str, case_id: str) -> tuple[int, int]:
    start = text.find(value)
    if start < 0:
        raise SystemExit(f"{case_id}: value not found: {value!r}")
    if text.find(value, start + 1) >= 0:
        print(
            f"NOTE {case_id}: value not unique, using first find()={start}: {value!r}",
            file=sys.stderr,
        )
    return start, start + len(value)


CASES_05_09 = [
    {
        "id": "case_05_credit_card_update",
        "raw_text": (
            "خدمة العملاء: أهلاً بيك يا فندم في قسم الحسابات، أقدر أساعدك إزاي؟\n"
            "العميل: كنت عايز أغير كارت الفيزا المربوط بدفع الفاتورة التلقائي عشان الكارت القديم ضاع وطلعت واحد بداله.\n"
            "خدمة العملاء: ولا يهمك يا فندم، ممكن تمليني الستاشر رقم بتوع الكارت الجديد؟\n"
            "العميل: أربعة اتنين زيرو تلاتة، خمسة خمسة ستة ستة، سبعة تمنية تسعة زيرو، واحد اتنين تلاتة أربعة.\n"
            "خدمة العملاء: تمام، تاريخ الانتهاء كام؟\n"
            "العميل: شهر حداشر عشرين تلاتين. ورقم السي في في اللي في الضهر تلاتة سبعة اتنين."
        ),
        "expected_pii": [
            {
                "value": "أربعة اتنين زيرو تلاتة، خمسة خمسة ستة ستة، سبعة تمنية تسعة زيرو، واحد اتنين تلاتة أربعة",
                "type": "Credit Card Number / PAN (Spoken)",
            },
            {"value": "شهر حداشر عشرين تلاتين", "type": "Credit Card Expiration Date (Spoken)"},
            {"value": "تلاتة سبعة اتنين", "type": "CVV / CVC (Spoken)"},
        ],
    },
    {
        "id": "case_06_emergency_geolocation",
        "raw_text": (
            "خدمة العملاء: خط الدعم العاجل معاك.\n"
            "العميل: يا فندم أنا عربيتي عطلت وفي حالة ولادة مستعجلة لمراتي اسمها سارة عبد الرحمن ومحتاجين إسعاف، أنا بكلمك من خط فودافون عشان معنديش شبكة تانية.\n"
            "خدمة العملاء: أهدي يا فندم إحنا حددنا اللوكيشن التقريبي بتاع الموبايل عن طريق الآي بي، هو اتنين واحد اتنين دوت خمسين دوت أربعتاشر دوت عشرة، حضرتك على طريق الإسكندرية الصحراوي الكيلو خمسة وأربعين صح؟\n"
            "العميل: أيوه بالظبط أنا واقف قدام واحة عمر."
        ),
        "expected_pii": [
            {"value": "حالة ولادة مستعجلة", "type": "Medical / Health Condition (PHI)"},
            {"value": "سارة عبد الرحمن", "type": "Full Name"},
            {
                "value": "اتنين واحد اتنين دوت خمسين دوت أربعتاشر دوت عشرة",
                "type": "IP Address (Spoken / Verbally Obfuscated)",
            },
            {
                "value": "طريق الإسكندرية الصحراوي الكيلو خمسة وأربعين",
                "type": "Physical Address / Location",
            },
            {"value": "قدام واحة عمر", "type": "Physical Address / Landmark"},
        ],
    },
    {
        "id": "case_07_b2b_tax_id",
        "raw_text": (
            "خدمة العملاء: خدمة عملاء الشركات، أقدر أساعد حضرتك إزاي؟\n"
            "العميل: أنا بكلمك بخصوص حساب شركة النور للاستيراد والتصدير. عايزين نضيف خط جديد للمندوبين.\n"
            "خدمة العملاء: تمام يا فندم، عشان الإجراء ده محتاجين بس نأكد رقم البطاقة الضريبية واسم المفوض بالتعاقد.\n"
            "العميل: البطاقة الضريبية رقمها ربعمية وخمسين داش تلتومية وعشرين داش مية وخمستاشر. واسم المفوض خالد مصطفى كمال."
        ),
        "expected_pii": [
            {"value": "شركة النور للاستيراد والتصدير", "type": "Company / Organization Name"},
            {
                "value": "ربعمية وخمسين داش تلتومية وعشرين داش مية وخمستاشر",
                "type": "Tax ID Number (Spoken with dashes)",
            },
            {"value": "خالد مصطفى كمال", "type": "Full Name"},
        ],
    },
    {
        "id": "case_08_roaming_passport",
        "raw_text": (
            "خدمة العملاء: قسم التجوال الدولي معاك.\n"
            "العميل: أنا هسافر دبي بكرة وعايز أفعل باقة التجوال، بس التطبيق طلب مني أحدث رقم الباسبور عشان الخط متسجل بجواز سفر قديم.\n"
            "خدمة العملاء: تمام يا فندم مليني رقم جواز السفر الجديد وحروفه.\n"
            "العميل: إيه كابيتال، تسعة زيرو تلاتة أربعة خمسة سبعة اتنين واحد. وابعتلي التأكيد على الإيميل بتاعي.\n"
            "خدمة العملاء: الإيميل اللي متسجل عندنا هو khaled underscore 20 at outlook dot com مظبوط؟\n"
            "العميل: أيوه هو ده."
        ),
        "expected_pii": [
            {
                "value": "إيه كابيتال، تسعة زيرو تلاتة أربعة خمسة سبعة اتنين واحد",
                "type": "Passport Number (Spoken Mixed Script)",
            },
            {
                "value": "khaled underscore 20 at outlook dot com",
                "type": "Email Address (Verbally Obfuscated)",
            },
        ],
    },
    {
        "id": "case_09_puk_grouped_numbers",
        "raw_text": (
            "خدمة العملاء: الدعم الفني معاك، إزاي أقدر أساعدك؟\n"
            "العميل: الخط قفل مني وطلب رمز الباك عشان كتبت البين كود غلط تلات مرات.\n"
            "خدمة العملاء: تمام، رقم الموبايل هو زيرو مية، خمسة وخمسين، تلاتين، ربعمية؟\n"
            "العميل: مظبوط.\n"
            "خدمة العملاء: للتحقق الأمني، ممكن الرقم القومي بالكامل؟\n"
            "العميل: اتنين تمانية تلاتة، زيرو تسعة واحد خمسة، زيرو واحد زيرو زيرو تلاتة تلاتة.\n"
            "خدمة العملاء: شكراً يا فندم، رمز الباك (PUK) بتاعك هو تمانية سبعة ستة خمسة أربعة تلاتة اتنين واحد."
        ),
        "expected_pii": [
            {
                "value": "زيرو مية، خمسة وخمسين، تلاتين، ربعمية",
                "type": "Phone Number / MSISDN (Spoken Grouped)",
            },
            {
                "value": "اتنين تمانية تلاتة، زيرو تسعة واحد خمسة، زيرو واحد زيرو زيرو تلاتة تلاتة",
                "type": "National ID (Egyptian 14-digit Spoken)",
            },
            {
                "value": "تمانية سبعة ستة خمسة أربعة تلاتة اتنين واحد",
                "type": "PUK Code (Spoken)",
            },
        ],
    },
]


def load_raw() -> list[dict]:
    prior = json.loads((ROOT / "operator_gold_raw.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in prior}
    for row in CASES_05_09:
        by_id[row["id"]] = row
    order = [
        "case_24_new_mixed_arabic_english",
        "case_01_basic_english",
        "case_10_arabic_wallet_failure",
        "case_02_technical_troubleshoot",
        "case_16_mixed_script_wallet_transfer",
        "case_19_plan_migration",
        "case_20_router_delivery",
        "case_21_scratch_card",
        "case_22_puk_code",
        "case_23_ticket_follow_up",
        "case_05_credit_card_update",
        "case_06_emergency_geolocation",
        "case_07_b2b_tax_id",
        "case_08_roaming_passport",
        "case_09_puk_grouped_numbers",
    ]
    return [by_id[i] for i in order if i in by_id]


def to_eval_dataset(rows: list[dict]) -> dict:
    cases = []
    for row in rows:
        text = row["raw_text"]
        spans = []
        for item in row["expected_pii"]:
            start, end = unique_span(text, item["value"], row["id"])
            assert text[start:end] == item["value"]
            if item.get("start") is not None and int(item["start"]) != start:
                print(
                    f"NOTE {row['id']}: operator start {item['start']} "
                    f"!= find() {start} for {item['value']!r}",
                    file=sys.stderr,
                )
            spans.append({
                "start": start,
                "end": end,
                "entity_type": catalog_type(item["type"]),
            })
        cases.append({
            "id": row["id"],
            "text": text,
            "language": "en" if row["id"] in {"case_01_basic_english", "case_02_technical_troubleshoot"} else "ar",
            "expected_spans": spans,
        })
    return {
        "kind": "redibis.text_span_eval_dataset",
        "schema_version": "1.0",
        "offset_unit": "unicode_codepoint",
        "id": "gateway-operator-call-center",
        "cases": cases,
    }


def diagnose(dataset: dict) -> int:
    from redibis.pii.rules.ruleset import RuleSetCompiler
    from redibis.pii.scan.result import TextScanConfig
    from redibis.pii.scan.text_scanner import TextScanner

    scanner = TextScanner(ruleset=RuleSetCompiler.default())
    cfg = TextScanConfig(
        engines="regex",
        language="ar",
        min_score=0.2,
        preprocess_obfuscation=True,
        resolve="priority",
    )
    misses = 0
    for row in dataset["cases"]:
        text = row["text"]
        cfg_case = cfg
        if row.get("language") == "en":
            cfg_case = TextScanConfig(
                engines="regex",
                language="en",
                min_score=0.2,
                preprocess_obfuscation=True,
                resolve="priority",
            )
        result = scanner.scan(text, cfg_case)
        print(f"\n=== {row['id']} ===")
        for span in row["expected_spans"]:
            hit = next(
                (
                    d for d in result.detections
                    if d.entity_type == span["entity_type"]
                    and d.start == span["start"]
                    and d.end == span["end"]
                ),
                None,
            )
            gold = text[span["start"]:span["end"]]
            if hit:
                print(f"  HIT {span['entity_type']} {span['start']}:{span['end']} {gold!r}")
                continue
            near = [
                (d.entity_type, d.start, d.end, text[d.start:d.end])
                for d in result.detections
                if d.start is not None and d.end is not None
                and max(0, min(d.end, span["end"]) - max(d.start, span["start"])) > 0
            ]
            misses += 1
            print(f"  MISS {span['entity_type']} {span['start']}:{span['end']} {gold!r}")
            if near:
                print(f"    near={near}")
        print("  detections:")
        for d in result.detections:
            print(f"    {d.entity_type} {d.start}:{d.end} {text[d.start:d.end]!r} score={d.score:.2f} eng={d.engine}")
    return misses


def main() -> None:
    dataset = to_eval_dataset(load_raw())
    out = ROOT / "operator_call_center.json"
    out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(dataset['cases'])} cases)", file=sys.stderr)
    if "--diagnose" in sys.argv:
        n = diagnose(dataset)
        raise SystemExit(n)


if __name__ == "__main__":
    main()
