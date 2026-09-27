"""Author quality rules from a pandas DataFrame and merge them into the contract.

Run from the repository root:

    python examples/quality_authoring/author_pandas.py

Then review with the CLI:  redibis quality-run list eshop.customer_account --output-dir ./reports
Tutorial: docs/tutorials/QUALITY_AUTHORING.md
"""

import logging

import pandas as pd

from redibis.quality.authoring import SNAPSHOT_RULES, QualityAuthor

logging.getLogger("great_expectations").setLevel(logging.ERROR)

TABLE = "eshop.customer_account"
CSV = "tests/data/realistic_eshop_customer_account.csv"

# dtype=str keeps leading zeros (phone numbers, IDs) exactly as stored
df = pd.read_csv(CSV, dtype=str)
first, second = df.iloc[:500], df.iloc[500:]

qa = QualityAuthor(TABLE, output_dir="./reports")
draft = qa.author(first)
print(draft.summary())

print("\nas discovered, on the other half:", draft.validate(second).rules_passed,
      "/", draft.validate(second).rules_total)
draft.drop(rule_types=SNAPSHOT_RULES)
draft.drop(rule_types=["in_set"])          # names/IDs are not a fixed list
draft.relax(0.1)

# business rules the profile cannot know, in SQL (${object} is this table)
draft.add_sql("SELECT * FROM ${object} WHERE email_address NOT LIKE '%@%'",
              description="every email address has an @")
draft.add_sql("SELECT COUNT(*) FROM ${object} WHERE session_id <> eshop_customer_id",
              max_failures=10, description="sessions belong to their customer")
check = draft.validate(second)
print("after curation:", check.status, check.rules_passed, "/", check.rules_total)

run = qa.save(draft, note="example: authored on rows 0-499, checked on 500-999")
print("\nsaved run", run.run_id)
print(qa.diff(run.run_id))
merged = qa.merge(run.run_id)
print(f"merged into {TABLE} v{merged.upsert.version_after}")
