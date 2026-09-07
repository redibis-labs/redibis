# Edge cases

- `customer_id` is not `NATIONAL_ID` unless entity detection and validators agree.
- Short internal extensions are not MSISDN.
- LAC/cell identifiers are network identifiers, not location PII by themselves.
- Billing account numbers are confidential but not always personal data.
