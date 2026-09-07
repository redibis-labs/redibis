# Golden tutorial — retail customer domain glossary

Use this glossary when enriching `golden.tutorial_customers`.

| Column | Preferred business name | Definition guidance |
|--------|-------------------------|---------------------|
| `customer_id` | Customer Identifier | Internal synthetic customer key used only inside this tutorial dataset. |
| `full_name` | Customer Full Name | Legal or preferred full name of the customer as captured at signup. |
| `email` | Email Address | Primary contact email for order and account notifications. |
| `mobile_number` | Mobile Number (MSISDN) | Egyptian mobile number used for SMS OTP and support contact. |
| `national_id` | National ID | Egyptian national identity number; treat as sensitive government ID. |
| `account_status` | Account Status | Lifecycle state: `active`, `suspended`, or `churned`. |
| `created_at` | Account Created At | Timestamp when the customer account was first created. |

## Steward rules

- Prefer telecom/retail vocabulary from this glossary.
- Do not invent columns that are not in the contract.
- Keep definitions short (1–2 sentences) and operator-facing.
- For PII columns, define purpose without quoting raw sample values.
