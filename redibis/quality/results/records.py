"""The quality results data model, shared by every store.

Three logical tables, identical in Iceberg, Parquet on S3/MinIO and Postgres:

``partition_runs``   one row per validation of one partition of one table
``rule_results``     one row per rule per run (passed, observed, unexpected, sample)
``rule_sets``        the rules a run used, one row per rule per rule-set digest —
                     consumers read it to choose the rules they need

A run is identified by ``run_id`` and deduplicated on
``(table_name, partition, data_fingerprint, rule_set_digest)``: the same data
checked with the same rules is never computed twice.

Which partition a DataFrame holds is the caller's business: redibis validates
the frame it is given and records the partition it is told, as
``partition`` (``dt=2026-09-26/hour=05``), ``partition_column`` (``dt/hour``),
``partition_value`` (``2026-09-26/05``), and two typed, sortable columns for
``SELECT max(…)``: ``partition_ts`` (TIMESTAMP, from the date, date + hour or
datetime in the value) and ``partition_date`` (DATE). ``validated_at`` orders
the runs of one partition.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import pyarrow as pa

PARTITION_RUNS = "partition_runs"
RULE_RESULTS = "rule_results"
RULE_SETS = "rule_sets"
TABLES = (PARTITION_RUNS, RULE_RESULTS, RULE_SETS)

_TS = pa.timestamp("us", tz="UTC")

SCHEMAS: dict[str, pa.Schema] = {
    PARTITION_RUNS: pa.schema([
        ("table_name", pa.string()),
        ("partition", pa.string()),
        ("partition_column", pa.string()),
        ("partition_value", pa.string()),
        ("partition_ts", _TS),
        ("partition_date", pa.date32()),
        ("run_id", pa.string()),
        ("validated_at", _TS),
        ("data_fingerprint", pa.string()),
        ("rule_set_digest", pa.string()),
        ("contract_version", pa.string()),
        ("engine", pa.string()),
        ("row_count", pa.int64()),
        ("rules_total", pa.int32()),
        ("rules_passed", pa.int32()),
        ("p1_failed", pa.int32()),
        ("status", pa.string()),
        ("error", pa.string()),
    ]),
    RULE_RESULTS: pa.schema([
        ("table_name", pa.string()),
        ("partition", pa.string()),
        ("partition_column", pa.string()),
        ("partition_value", pa.string()),
        ("partition_ts", _TS),
        ("partition_date", pa.date32()),
        ("run_id", pa.string()),
        ("validated_at", _TS),
        ("rule_set_digest", pa.string()),
        ("rule_id", pa.string()),
        ("rule_type", pa.string()),
        ("expectation", pa.string()),
        ("column_name", pa.string()),
        ("severity", pa.string()),
        ("owner", pa.string()),
        ("passed", pa.bool_()),
        ("unexpected_count", pa.int64()),
        ("observed", pa.string()),
        ("sample", pa.string()),
        ("message", pa.string()),
    ]),
    RULE_SETS: pa.schema([
        ("table_name", pa.string()),
        ("rule_set_digest", pa.string()),
        ("contract_version", pa.string()),
        ("rule_id", pa.string()),
        ("rule_type", pa.string()),
        ("odcs_rule", pa.string()),
        ("expectation", pa.string()),
        ("column_name", pa.string()),
        ("severity", pa.string()),
        ("owner", pa.string()),
        ("description", pa.string()),
        ("definition", pa.string()),
        ("created_at", _TS),
    ]),
}

_DATE_IN_PARTITION = re.compile(r"(?:^|[/=_ ])(\d{4}-\d{2}-\d{2})(?:$|[/ T])")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def partition_date_of(partition: str) -> Optional[date]:
    """``2026-09-26``, ``dt=2026-09-26`` or ``dt=2026-09-26/region=eg`` → the date."""
    text = str(partition).strip()
    if len(text) >= 10 and text[4] == "-":
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    m = _DATE_IN_PARTITION.search(text)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return None


_HOUR_NAMES = ("hour", "hr", "h", "hh")


def _as_datetime(value: Any) -> Optional[datetime]:
    """A date/time partition value → datetime (UTC); None when it is not one."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):                       # 20260926
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if not re.match(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T")
                                        if len(text) > 10 else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PartitionSpec:
    """How a validated partition is recorded: its name, columns, values and sort keys."""

    partition: str                     # dt=2026-09-26/hour=05  (or the bare value given)
    column: str                        # dt/hour  ("" when only a value was given)
    value: str                         # 2026-09-26/05
    ts: Optional[datetime]             # 2026-09-26 05:00 UTC — for SELECT max(partition_ts)
    date: Optional[date]


def partition_spec(partition: Any, *, partition_ts: Optional[datetime] = None) -> PartitionSpec:
    """Parse what the caller says the DataFrame holds.

    ``"dt=2026-09-26"``, ``"dt=2026-09-26/hour=05"``, ``"2026-09-26"``, or a dict
    ``{"dt": date(2026, 9, 26), "hour": 5}`` (``partition_by`` order kept). The
    timestamp comes from the first date/datetime value, plus an ``hour`` column
    when there is one; pass ``partition_ts`` to set it yourself.
    """
    if isinstance(partition, dict):
        pairs = [(str(k), v) for k, v in partition.items()]
    else:
        text = str(partition).strip().strip("/")
        parts = [p for p in text.split("/") if p]
        if parts and all("=" in p for p in parts):
            pairs = [tuple(p.split("=", 1)) for p in parts]          # type: ignore[misc]
        else:
            pairs = [("", text)]

    def show(v: Any) -> str:
        return v.isoformat() if isinstance(v, (date, datetime)) else str(v)

    name = "/".join(f"{k}={show(v)}" if k else show(v) for k, v in pairs)
    ts = partition_ts
    if ts is None:
        for k, v in pairs:
            ts = _as_datetime(v)
            if ts is not None:
                break
        if ts is not None and ts.hour == 0 and ts.minute == 0:
            hour = next((v for k, v in pairs if k.lower() in _HOUR_NAMES), None)
            try:
                if hour is not None and 0 <= int(hour) <= 23:
                    ts = ts.replace(hour=int(hour))
            except (TypeError, ValueError):
                pass
    elif ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return PartitionSpec(
        partition=name,
        column="/".join(k for k, _v in pairs if k),
        value="/".join(show(v) for _k, v in pairs),
        ts=ts,
        date=ts.date() if ts is not None else partition_date_of(name),
    )


def to_json(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(value, default=str, ensure_ascii=False)


@dataclass
class PartitionRun:
    """One validation of one partition — also returned when a run is skipped."""

    table_name: str
    partition: str
    partition_date: Optional[date]
    run_id: str
    validated_at: datetime
    data_fingerprint: str
    rule_set_digest: str
    contract_version: str = ""
    engine: str = ""
    row_count: Optional[int] = None
    rules_total: int = 0
    rules_passed: int = 0
    p1_failed: int = 0
    status: str = "passed"          # passed | failed | error
    error: str = ""
    partition_column: str = ""
    partition_value: str = ""
    partition_ts: Optional[datetime] = None
    skipped: bool = field(default=False, compare=False)   # already computed: nothing ran

    def row(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("skipped")
        return out

    @classmethod
    def from_row(cls, row: dict[str, Any], *, skipped: bool = False) -> "PartitionRun":
        keep = {k: row.get(k) for k in SCHEMAS[PARTITION_RUNS].names}
        for k in ("rules_total", "rules_passed", "p1_failed"):
            keep[k] = int(keep.get(k) or 0)
        if keep.get("row_count") is not None and keep["row_count"] == keep["row_count"]:
            keep["row_count"] = int(keep["row_count"])
        else:
            keep["row_count"] = None
        for k in ("contract_version", "engine", "error", "data_fingerprint",
                  "partition_column", "partition_value"):
            keep[k] = keep[k] if isinstance(keep.get(k), str) else ""    # None / NaN from old rows
        ts = keep.get("partition_ts")
        keep["partition_ts"] = None if ts is None or ts != ts else ts   # NaT/None from old rows
        return cls(**keep, skipped=skipped)

    def __str__(self) -> str:
        how = "already validated, reused" if self.skipped else "validated"
        return (f"{self.table_name} [{self.partition}] {self.status.upper()} — "
                f"{self.rules_passed}/{self.rules_total} rules passed, {self.p1_failed} P1 failed "
                f"({how}; run {self.run_id}, rules {self.rule_set_digest})")
