"""Per-table continuous quality monitoring deployment package builder.

One package = one authoritative, technology-neutral automation unit:

  - ``{slug}_quality.py``  — the *authoritative* entry point: a complete,
    executable Python program (redibis + Great Expectations at runtime) with
    every rule embedded as a literal. Spark-first by default so a full
    business-day partition is validated in the cluster, never a driver-side
    sample. Any scheduler (cron, Airflow, Dagster, ...) can invoke it directly.
  - ``quality/rule_set.json`` — the canonical, versioned rule set
    (``redibis.io/quality/v1alpha1``) — the portable interchange artifact.
  - ``quality/rules.yaml``   — the same rules in the legacy editable GE shape.
  - ``ge_paste.py``          — AST-safe fragment for the Quality UI paste panel.
  - ``dags/``                — optional Airflow wrappers around the same script.
  - ``requirements.txt``     — pinned-where-known dependency manifest.
  - ``schemas/``             — the JSON Schemas the rule set / run conform to.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import yaml

from redibis.integrations.airflow.generate import render_table_dag
from redibis.quality.contract_validate import quality_rules_from_contract
from redibis.quality.ge_codegen import (
    detect_runtime_versions,
    render_full_quality_program,
    render_ge_paste_module,
    render_rules_yaml,
)
from redibis.quality.schema import rule_set_from_contract, rule_set_from_ge_rules

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas" / "quality"


def _slug(table: str) -> str:
    return table.replace(".", "_").replace("-", "_")


def build_monitoring_package(
    *,
    table: str,
    contract: Optional[dict] = None,
    rules: Optional[Sequence[dict[str, Any]]] = None,
    dropped_rule_ids: Optional[Sequence[str]] = None,
    schedule: str = "0 6 * * *",
    output_dir: Path,
    sample_root: str = "${REDIBIS_SAMPLE_ROOT}",
    config_path: str = "${REDIBIS_CONFIG:-/etc/redibis/redibis.yaml}",
    rule_source: str = "",
    engine: str = "spark",
    source_digest: str = "",
) -> Path:
    """Write ``{table}-monitoring/`` directory with the executable program,
    canonical rule set, editable rules, DAG wrappers, and a dependency manifest.

    ``rule_source`` documents where ``rules``/``contract`` came from
    (``"active_contract"`` | ``"session_draft"`` | ``"python_file"`` |
    ``"rules_file"``) so the package is auditable rather than silently picking a
    rule origin. ``source_digest`` records the sha256 of that input when it was
    a file. ``engine`` defaults to ``"spark"``: the authoritative program
    validates a whole partition in the cluster, not a driver-side sample.
    """
    explicit_rules = rules is not None
    rule_set = list(rules or [])
    if not rule_set and contract is not None:
        rule_set = list(quality_rules_from_contract(contract).rules)
    if not rule_source:
        rule_source = "session_draft" if explicit_rules else ("active_contract" if contract is not None else "none")

    slug = _slug(table)
    root = Path(output_dir) / f"{slug}-monitoring"
    root.mkdir(parents=True, exist_ok=True)

    if contract is not None and not explicit_rules:
        canonical = rule_set_from_contract(contract, table=table)
    else:
        canonical = rule_set_from_ge_rules(rule_set, table=table, source=rule_source or "session_draft")

    redibis_version, ge_version = detect_runtime_versions()

    manifest = {
        "apiVersion": "redibis/v1",
        "kind": "QualityMonitorPackage",
        "table": table,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schedule": schedule,
        "rule_source": rule_source,
        "rule_set_id": canonical.rule_set_id,
        "rule_set_digest": canonical.semantic_digest,
        "dropped_rule_ids": list(dropped_rule_ids or []),
        "sample_root": sample_root,
        "config": config_path,
        "engine": engine,
        "runtime": {"redibis_version": redibis_version, "great_expectations_version": ge_version},
    }
    if source_digest:
        manifest["source_digest"] = source_digest
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    quality_dir = root / "quality"
    quality_dir.mkdir(exist_ok=True)
    (quality_dir / "rules.yaml").write_text(
        render_rules_yaml(rule_set, name=slug),
        encoding="utf-8",
    )
    (quality_dir / "rule_set.json").write_text(
        json.dumps(canonical.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    (root / "ge_paste.py").write_text(render_ge_paste_module(rule_set), encoding="utf-8")

    from redibis.quality.rule_set import QualityRuleSet as _QRS

    program = render_full_quality_program(
        table=table,
        rule_set=_QRS(name=slug, rules=rule_set),
        rule_set_id=canonical.rule_set_id,
        rule_set_digest=canonical.semantic_digest,
        redibis_version=redibis_version,
        ge_version=ge_version,
        engine=engine,
    )
    (root / f"{slug}_quality.py").write_text(program, encoding="utf-8")

    (root / "requirements.txt").write_text(
        _render_requirements(redibis_version, ge_version, engine), encoding="utf-8"
    )

    schemas_dir = root / "schemas"
    schemas_dir.mkdir(exist_ok=True)
    for name in ("quality_ruleset.v1alpha1.schema.json", "quality_run.v1alpha1.schema.json"):
        src = _SCHEMAS_DIR / name
        if src.is_file():
            (schemas_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    dags_dir = root / "dags"
    dags_dir.mkdir(exist_ok=True)
    # The package's DAGs wrap the package's own authoritative program, so the
    # scheduled run validates the same full partition the operator would.
    runner = "spark_submit" if engine == "spark" else "cli"
    dag_code = render_table_dag(
        table=table,
        schedule=schedule,
        sample_root=sample_root,
        config_path=config_path,
        trigger_only=False,
        runner=runner,
        program=f"{slug}_quality.py",
    )
    (dags_dir / f"redibis_quality_{slug}.py").write_text(dag_code, encoding="utf-8")

    event_dag = render_table_dag(
        table=table,
        schedule=None,
        sample_root=sample_root,
        config_path=config_path,
        trigger_only=True,
        runner=runner,
        program=f"{slug}_quality.py",
    )
    (dags_dir / f"redibis_quality_{slug}_event.py").write_text(event_dag, encoding="utf-8")

    (root / "README.md").write_text(
        _render_readme(table, slug, schedule, sample_root, config_path, rule_source, engine),
        encoding="utf-8",
    )

    checksums = {p.relative_to(root).as_posix(): _sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}
    (root / "CHECKSUMS.json").write_text(json.dumps(checksums, indent=2, sort_keys=True), encoding="utf-8")
    return root


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def build_monitoring_zip(
    *,
    table: str,
    contract: Optional[dict] = None,
    rules: Optional[Sequence[dict[str, Any]]] = None,
    dropped_rule_ids: Optional[Sequence[str]] = None,
    schedule: str = "0 6 * * *",
    rule_source: str = "",
    engine: str = "spark",
    source_digest: str = "",
) -> bytes:
    """Return a zip archive of the monitoring package (in-memory)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = build_monitoring_package(
            table=table,
            contract=contract,
            rules=rules,
            dropped_rule_ids=dropped_rule_ids,
            schedule=schedule,
            output_dir=Path(tmp),
            rule_source=rule_source,
            engine=engine,
            source_digest=source_digest,
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in root.rglob("*"):
                if path.is_file():
                    zf.write(path, arcname=path.relative_to(root).as_posix())
        return buf.getvalue()


def _render_requirements(redibis_version: str, ge_version: str, engine: str = "spark") -> str:
    extras = "ge,spark" if engine == "spark" else "ge"
    redibis_pin = (
        f"redibis[{extras}]=={redibis_version}" if redibis_version else f"redibis[{extras}]"
    )
    ge_pin = f"great_expectations=={ge_version}" if ge_version else "great_expectations"
    runtime = "pyspark" if engine == "spark" else "pandas"
    return f"{redibis_pin}\n{ge_pin}\n{runtime}\n"


def _render_readme(
    table: str,
    slug: str,
    schedule: str,
    sample_root: str,
    config_path: str,
    rule_source: str,
    engine: str = "spark",
) -> str:
    spark = engine == "spark"
    extras = "ge,spark" if spark else "ge"
    runtime_dep = "pyspark" if spark else "pandas"
    run_cmd = (
        f"spark-submit {slug}_quality.py --table {table} \\\n"
        f'  --partition-filter "txn_date = \'2026-08-30\'" --out results.json'
        if spark
        else f"python {slug}_quality.py --sample data.csv --out results.json"
    )
    jupyter = (
        f"""from {slug}_quality import build_rule_set, load_data, validate

# You usually already have a Spark DataFrame in a notebook:
report = validate(existing_spark_dataframe)

# Or let the program read one full partition itself:
df = load_data(partition_filter="txn_date = '2026-08-30'")
report = validate(df)"""
        if spark
        else f"""from {slug}_quality import build_rule_set, load_sample, validate

df = load_sample("data.csv")
report = validate(df)"""
    )
    scale_note = (
        "Rules run **in the Spark cluster** against the whole DataFrame — no "
        "`toPandas()`, no row cap, nothing collected to the driver. That is what "
        "makes a full business-day transaction partition a meaningful verdict."
        if spark
        else "This is the Pandas variant, intended for small local development "
        "samples only. Regenerate with the Spark engine for production volumes."
    )
    dag_note = (
        f"Both DAGs `spark-submit` `{slug}_quality.py`, so the scheduled run "
        f"validates the same full partition you would validate by hand."
        if spark
        else f"Both DAGs run `redibis quality-monitor run` against a bounded sample."
    )

    return f"""# Redibis quality monitor — `{table}`   (engine: {engine})

{scale_note}

## Contents

| File | Purpose |
|------|---------|
| `{slug}_quality.py` | **Authoritative entry point** — complete, executable Python (redibis + Great Expectations), rules embedded as literals. Run directly, import in Jupyter, or invoke from any scheduler. |
| `manifest.yaml` | Package metadata, rule provenance (`rule_source: {rule_source}`), engine, and curation audit |
| `quality/rule_set.json` | Canonical, versioned rule set (`redibis.io/quality/v1alpha1`) — portable interchange artifact |
| `quality/rules.yaml` | Same rules in the legacy editable GE shape |
| `ge_paste.py` | AST-safe rules-only fragment for the Quality UI paste panel (compatibility artifact; never executed as-is) |
| `requirements.txt` | Dependency manifest (redibis[{extras}], great_expectations, {runtime_dep}) |
| `schemas/` | JSON Schemas the rule set / run artifacts conform to |
| `dags/redibis_quality_{slug}.py` | Airflow daily DAG (`{schedule}`) — optional wrapper, not required |
| `dags/redibis_quality_{slug}_event.py` | Event-triggered DAG (`schedule=None`) — optional wrapper |
| `CHECKSUMS.json` | sha256 of every file in this package |

## Prerequisites

```bash
pip install -r requirements.txt
```

- A reachable Spark session / cluster and read access to `{table}`
- MinIO / S3 for run artifacts if using `redibis quality-monitor run` instead of the standalone script
- OpenMetadata for dashboards/alerts (`quality.publish.sinks: [openmetadata]` or legacy `catalog.push.quality: true`)

## Run anywhere (no Redibis server, no scheduler required)

```bash
{run_cmd}
```

## Use in Jupyter / any IDE

```python
{jupyter}
```

Edit the `RULES = [...]` block at the top of `{slug}_quality.py` to add, remove,
or retune expectations. Nothing is read from `quality/rules.yaml` at runtime —
the program is self-contained.

## Airflow deployment (optional)

1. Copy this whole package next to your DAGs and set `REDIBIS_PACKAGE_DIR`
2. Copy `dags/` into `$AIRFLOW_HOME/dags/`
3. Enable `redibis_quality_{slug}` for daily runs
4. Trigger `redibis_quality_{slug}_event` after upstream ETL completes

{dag_note}

Any other open-source scheduler (cron, Dagster, Prefect, ...) can invoke
`{slug}_quality.py` the same way — Airflow is not required.

## Round-tripping edits back into the contract

After editing `{slug}_quality.py` in Jupyter, either:

- `redibis quality-monitor export {table} --python-file {slug}_quality.py -o ./packages`
  to rebuild this package from the edited rules, or
- paste the edited program into the Quality UI → evaluate → approve → merge.

Either path parses the file with AST/literals only; the edited Python is never
imported or executed by Redibis, and contract writes still need approval.

## Push results to a data-quality dashboard

`{slug}_quality.py --out results.json` writes the run's report as JSON. To
publish to OpenMetadata (or any future sink), run via
`redibis quality-monitor run` in an environment with `REDIBIS_CONFIG` pointed at
your `quality.publish.sinks` config — the deployment script itself stays
dashboard-agnostic.
"""
