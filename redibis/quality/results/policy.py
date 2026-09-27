"""What a consumer needs from a table's quality — decided from stored results, no rescans.

A policy is a small YAML file owned by the consuming team::

    consumer: churn_model
    table: shop.orders
    window: {last_days: 2}          # today, today-1, today-2  (or: today: true · since: 2026-09-01 ·
                                    #  partitions: [...] · latest: 3)
    require: all                    # all partitions in the window pass · latest · any
    on_missing: block               # a partition not validated yet: block (default) · ignore
    rules:                          # choose from the contract / the stored rule set
      ids: [q_0f3a…]                #   exact rules (ids from `redibis quality-results rules TABLE`)
      columns: [items_value, total] #   …and/or filters, combined: P1 rules on these columns
      severities: [P1]
      types: [not_null, expect_column_values_to_be_between]
      # all: true                   #   every rule of the table
    custom_sql:                     # computed ONCE by the producer, for this consumer
      - query: SELECT COUNT(*) FROM ${object} WHERE channel = 'app' AND total > 20000
        mustBe: 0
        description: no app order above 20 000

``ConsumerPolicy.from_yaml(path).evaluate(store)`` returns a
:class:`ConsumerDecision`: accepted or not, one verdict per partition with the
failing rules, and the partitions a pipeline may read
(``decision.partition_filter("dt")``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd
import yaml

from redibis.quality.results.store import QualityResultStore, get_result_store

REQUIRE = ("all", "latest", "any")


def _clean(value: Any) -> Any:
    """Missing values from any store (None, NaN, "") → None."""
    if value is None or value == "" or (isinstance(value, float) and value != value):
        return None
    return value
ON_MISSING = ("block", "ignore")


@dataclass
class RuleSelection:
    ids: list[str] = field(default_factory=list)
    columns: list[Optional[str]] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    all: bool = False

    @property
    def has_filters(self) -> bool:
        return bool(self.columns or self.types or self.severities or self.owners)

    def select(self, rule_set: pd.DataFrame) -> set[str]:
        """Rule ids of ``rule_set`` this selection picks (explicit ids ∪ filtered rules)."""
        from redibis.quality.authoring import _RULE_ALIASES, _rule_names

        if rule_set.empty:
            return set()
        if self.all or (not self.ids and not self.has_filters):
            return set(rule_set["rule_id"])
        chosen = {i for i in self.ids if i in set(rule_set["rule_id"])}
        if self.has_filters:
            types = {str(t).lower() for t in self.types}
            types |= {_RULE_ALIASES[t] for t in types if t in _RULE_ALIASES}
            for row in rule_set.to_dict("records"):
                col = _clean(row.get("column_name"))
                if self.columns and col not in self.columns:
                    continue
                if self.severities and row.get("severity") not in self.severities:
                    continue
                if self.owners and not any(o in str(row.get("owner") or "") for o in self.owners):
                    continue
                names = _rule_names({"type": row.get("rule_type"), "odcs_rule": row.get("odcs_rule"),
                                     "expectation_type": row.get("expectation")})
                if types and not names & types:
                    continue
                chosen.add(row["rule_id"])
        return chosen


@dataclass
class Window:
    """Which partitions a consumer needs, relative to ``as_of`` (default: today)."""

    today: bool = False
    last_days: Optional[int] = None
    since: Optional[date] = None
    partitions: list[str] = field(default_factory=list)
    latest: Optional[int] = None

    def dates(self, as_of: date) -> Optional[list[date]]:
        """Expected partition dates, or None for non-date windows."""
        if self.today:
            return [as_of]
        if self.last_days is not None:
            return [as_of - timedelta(days=d) for d in range(self.last_days, -1, -1)]
        if self.since is not None:
            return [self.since + timedelta(days=d) for d in range((as_of - self.since).days + 1)]
        return None

    def describe(self) -> str:
        if self.today:
            return "partition == today"
        if self.last_days is not None:
            return f"partition >= today - {self.last_days} days"
        if self.since is not None:
            return f"partition >= {self.since}"
        if self.partitions:
            return f"partitions {self.partitions}"
        return f"latest {self.latest} partition(s)"


@dataclass
class PartitionVerdict:
    partition: str
    status: str                        # passed | failed | missing | incomplete
    run_id: str = ""
    validated_at: str = ""
    failures: list[dict] = field(default_factory=list)
    missing_rules: list[str] = field(default_factory=list)


@dataclass
class ConsumerDecision:
    consumer: str
    table: str
    accepted: bool
    reason: str
    window: str
    require: str
    rules_checked: int
    verdicts: list[PartitionVerdict]

    @property
    def accepted_partitions(self) -> list[str]:
        return [v.partition for v in self.verdicts if v.status == "passed"]

    def partition_filter(self, column: str = "dt") -> str:
        """SQL predicate selecting only the partitions that passed, e.g. for ``spark.table(...).where()``."""
        parts = self.accepted_partitions
        if not parts:
            return "1 = 0"
        values = ", ".join("'" + p.split("=")[-1].replace("'", "''") + "'" for p in parts)
        return f"{column} IN ({values})"

    def to_dict(self) -> dict:
        return {**asdict(self), "accepted_partitions": self.accepted_partitions}

    def __str__(self) -> str:
        head = (f"{'ACCEPT' if self.accepted else 'BLOCK'} {self.consumer} ← {self.table} "
                f"({self.window}, require {self.require}, {self.rules_checked} rules): {self.reason}")
        lines = [head]
        for v in self.verdicts:
            lines.append(f"  [{v.partition}] {v.status}" + (f"  run {v.run_id}" if v.run_id else ""))
            for f in v.failures[:10]:
                what = f"{f['rule']} \"{f['description']}\"" if f.get("description") else f["rule"]
                lines.append(f"      ✗ {f['severity']} {what} on {f['column'] or '(table)'}: "
                             f"{f['detail']}")
            if v.missing_rules:
                lines.append(f"      ? not computed in this run: {', '.join(v.missing_rules[:5])}")
        return "\n".join(lines)


@dataclass
class ConsumerPolicy:
    consumer: str
    table: str
    window: Window = field(default_factory=lambda: Window(today=True))
    require: str = "all"
    on_missing: str = "block"
    rules: RuleSelection = field(default_factory=RuleSelection)
    custom_sql: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.require not in REQUIRE:
            raise ValueError(f"require must be one of {REQUIRE}")
        if self.on_missing not in ON_MISSING:
            raise ValueError(f"on_missing must be one of {ON_MISSING}")

    # ── load / save ──────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict) -> "ConsumerPolicy":
        w = dict(data.get("window") or {"today": True})
        if isinstance(w.get("since"), str):
            w["since"] = date.fromisoformat(w["since"])
        if w.get("partitions"):
            w["partitions"] = [str(p) for p in w["partitions"]]
        r = dict(data.get("rules") or {})
        return cls(consumer=str(data["consumer"]), table=str(data["table"]), window=Window(**w),
                   require=str(data.get("require", "all")), on_missing=str(data.get("on_missing", "block")),
                   rules=RuleSelection(**r), custom_sql=list(data.get("custom_sql") or []))

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "ConsumerPolicy":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["window"] = {k: (v.isoformat() if isinstance(v, date) else v)
                       for k, v in d["window"].items() if v not in (None, False, [])}
        d["rules"] = {k: v for k, v in d["rules"].items() if v not in (None, False, [])}
        return d

    # ── custom SQL ───────────────────────────────────────────────────────

    def custom_sql_rules(self) -> list[dict]:
        from redibis.quality.sql_rules import sql_rule

        out = []
        for item in self.custom_sql:
            item = dict(item)
            query = item.pop("query", None) or item.pop("sql", None)
            item.pop("severity", None)
            out.append(sql_rule(query, **item))
        return out

    def custom_rule_ids(self) -> list[str]:
        from redibis.contracts.rules import stable_rule_id
        from redibis.quality.sql_rules import sql_rule_to_contract

        return [stable_rule_id(None, sql_rule_to_contract(r)) for r in self.custom_sql_rules()]

    # ── decide ───────────────────────────────────────────────────────────

    def evaluate(self, store: Union[QualityResultStore, str, None] = None, *,
                 as_of: Optional[date] = None) -> ConsumerDecision:
        """Decide from stored results whether this consumer may use the table's partitions."""
        store = store if isinstance(store, QualityResultStore) else get_result_store(store)
        as_of = as_of or date.today()
        expected_dates = self.window.dates(as_of)
        if expected_dates is not None:
            runs = store.runs(self.table, date_from=expected_dates[0], date_to=expected_dates[-1],
                              latest_only=True)
        elif self.window.partitions:
            runs = store.runs(self.table, partitions=self.window.partitions, latest_only=True)
        else:
            runs = store.runs(self.table, latest_only=True)
            if not runs.empty:
                order = runs.assign(_ts=pd.to_datetime(runs["partition_ts"], utc=True, errors="coerce"),
                                    _d=pd.to_datetime(runs["partition_date"], errors="coerce"))
                runs = (order.sort_values(["_ts", "_d", "partition"], na_position="first")
                        .tail(self.window.latest or 1).drop(columns=["_ts", "_d"]))

        slots: list[tuple[str, Optional[dict]]] = []
        if expected_dates is not None:
            by_date: dict[date, list[dict]] = {}
            for row in runs.to_dict("records"):
                d = pd.to_datetime(row["partition_date"]).date() if row.get("partition_date") is not None else None
                by_date.setdefault(d, []).append(row)
            for d in expected_dates:
                rows = by_date.get(d) or []
                slots += [(r["partition"], r) for r in rows] or [(d.isoformat(), None)]
        elif self.window.partitions:
            found = {r["partition"]: r for r in runs.to_dict("records")}
            slots = [(p, found.get(p)) for p in self.window.partitions]
        else:
            slots = [(r["partition"], r) for r in runs.to_dict("records")]

        custom_ids = set(self.custom_rule_ids())
        rule_sets: dict[str, pd.DataFrame] = {}
        verdicts: list[PartitionVerdict] = []
        rules_checked = 0
        for partition, run in slots:
            if run is None:
                verdicts.append(PartitionVerdict(partition=partition, status="missing"))
                continue
            digest = run["rule_set_digest"]
            if digest not in rule_sets:
                rule_sets[digest] = store.rule_set(self.table, digest)
            rs = rule_sets[digest]
            wanted = self.rules.select(rs) if (self.rules.all or self.rules.ids or self.rules.has_filters
                                               or not custom_ids) else set()
            wanted |= custom_ids
            wanted_explicit = set(self.rules.ids) | custom_ids
            present = set(rs["rule_id"]) if not rs.empty else set()
            missing_rules = sorted(wanted_explicit - present)
            required = wanted & present
            if not required and not missing_rules:
                missing_rules = ["(the rule selection matches no rule of this rule set)"]
            rules_checked = max(rules_checked, len(required))
            res = store.results(self.table, [run["run_id"]], rule_ids=sorted(required))
            described = ({r["rule_id"]: _clean(r.get("description")) for r in rs.to_dict("records")}
                         if not rs.empty else {})
            failures = []
            for r in res.to_dict("records"):
                if bool(r["passed"]):
                    continue
                unexpected = int(_clean(r.get("unexpected_count")) or 0)
                sample = json.loads(r["sample"]) if _clean(r.get("sample")) else []
                message = _clean(r.get("message")) or ""
                if unexpected:
                    shown = [s if len(s) <= 80 else s[:79] + "…" for s in map(str, sample[:3])]
                    detail = f"{unexpected} unexpected" + (f", e.g. {shown}" if shown else "")
                    detail += f" — {message}" if message else ""
                else:
                    detail = message or f"observed {_clean(r.get('observed')) or ''}"
                failures.append({
                    "rule_id": r["rule_id"],
                    "rule": _clean(r.get("expectation")) or r.get("rule_type"),
                    "description": described.get(r["rule_id"]) or "",
                    "column": _clean(r.get("column_name")), "severity": r.get("severity"),
                    "detail": detail,
                })
            status = "failed" if failures else ("incomplete" if missing_rules else "passed")
            verdicts.append(PartitionVerdict(
                partition=partition, status=status, run_id=str(run["run_id"]),
                validated_at=str(run["validated_at"]), failures=failures, missing_rules=missing_rules))

        accepted, reason = self._decide(verdicts)
        return ConsumerDecision(consumer=self.consumer, table=self.table, accepted=accepted,
                                reason=reason, window=self.window.describe(), require=self.require,
                                rules_checked=rules_checked, verdicts=verdicts)

    def _decide(self, verdicts: list[PartitionVerdict]) -> tuple[bool, str]:
        considered = [v for v in verdicts if not (v.status == "missing" and self.on_missing == "ignore")]
        if not considered:
            return False, "no validated partition in the window"
        bad = [v for v in considered if v.status != "passed"]
        if self.require == "all":
            if not bad:
                return True, f"all {len(considered)} partition(s) passed"
            return False, "; ".join(f"{v.partition} {v.status}" for v in bad)
        if self.require == "latest":
            last = considered[-1]
            return last.status == "passed", f"latest partition {last.partition} {last.status}"
        good = [v for v in considered if v.status == "passed"]
        return bool(good), (f"{len(good)} of {len(considered)} partition(s) passed" if good
                            else "no partition in the window passed")


def load_policies(source: Union[str, Path, Iterable[Any], None], *,
                  table: Optional[str] = None) -> list[ConsumerPolicy]:
    """Policies from a YAML file, a directory of ``*.yaml``/``*.yml``, or objects."""
    if source is None:
        return []
    if isinstance(source, (str, Path)):
        p = Path(source)
        files = sorted([*p.glob("*.yaml"), *p.glob("*.yml")]) if p.is_dir() else [p]
        policies = [ConsumerPolicy.from_yaml(f) for f in files]
    else:
        policies = [s if isinstance(s, ConsumerPolicy) else ConsumerPolicy.from_dict(s) for s in source]
    return [p for p in policies if table is None or p.table == table]


def rules_catalog(store: Union[QualityResultStore, str, None] = None, table: str = "", *,
                  contract: Optional[dict] = None) -> pd.DataFrame:
    """The rules a consumer can choose from: the stored rule set, or a contract's rules."""
    cols = ["rule_id", "column_name", "rule_type", "expectation", "severity", "owner", "description"]
    if contract is not None:
        from redibis.quality.results.producer import rule_set_rows

        _digest, rows = rule_set_rows(contract, table, owners={}, contract_version="")
        return pd.DataFrame(rows)[cols]
    store = store if isinstance(store, QualityResultStore) else get_result_store(store)
    rs = store.rule_set(table)
    return rs[cols] if not rs.empty else pd.DataFrame(columns=cols)
