"""Generate Airflow DAG files for redibis continuous quality monitoring."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from redibis.integrations.airflow.templates import dag_id_for_table, render_table_dag


def generate_dags(
    tables: Sequence[str],
    output_dir: str | Path,
    *,
    schedule: Optional[str] = "0 6 * * *",
    sample_root: str = "${REDIBIS_SAMPLE_ROOT}",
    config_path: str = "${REDIBIS_CONFIG:-/etc/redibis/redibis.yaml}",
    include_event_dags: bool = True,
) -> list[Path]:
    """Write one (or two) DAG files per table. Returns written paths."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for table in tables:
        slug = table.replace(".", "_")
        daily = render_table_dag(
            table=table,
            schedule=schedule,
            sample_root=sample_root,
            config_path=config_path,
            trigger_only=False,
        )
        daily_path = out / f"redibis_quality_{slug}.py"
        daily_path.write_text(daily, encoding="utf-8")
        written.append(daily_path)
        if include_event_dags:
            event = render_table_dag(
                table=table,
                schedule=None,
                sample_root=sample_root,
                config_path=config_path,
                trigger_only=True,
            )
            event_path = out / f"redibis_quality_{slug}_event.py"
            event_path.write_text(event, encoding="utf-8")
            written.append(event_path)
    return written
