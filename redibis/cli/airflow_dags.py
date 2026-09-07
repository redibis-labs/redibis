"""
Airflow DAG examples for redibis continuous quality monitoring.

Prefer generating DAGs via::

    redibis quality-monitor airflow generate --all-contracts -o ./dags --config redibis.yaml

This module keeps a minimal reference DAG for documentation and smoke tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta

try:
    from airflow import DAG
    from airflow.operators.bash import BashOperator
    _AIRFLOW_AVAILABLE = True
except ImportError:
    _AIRFLOW_AVAILABLE = False


_DEFAULT_ARGS = {
    "owner": "redibis",
    "depends_on_past": False,
    "email_on_failure": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


def _monitor_env() -> dict:
    return {
        "REDIBIS_CONFIG": "{{ var.value.get('redibis_config', '/etc/redibis/redibis.yaml') }}",
        "REDIBIS_SAMPLE_ROOT": "{{ var.value.get('redibis_sample_root', '/data/samples') }}",
        "S3_ENDPOINT_URL": "{{ var.value.get('s3_endpoint_url', '') }}",
    }


if _AIRFLOW_AVAILABLE:

    with DAG(
        dag_id="redibis_monitor_daily",
        default_args=_DEFAULT_ARGS,
        description="Daily validate-only quality monitor (redibis quality-monitor batch)",
        schedule="0 6 * * *",
        start_date=datetime(2026, 1, 1),
        catchup=False,
        tags=["redibis", "quality", "monitor"],
    ) as monitor_dag:

        BashOperator(
            task_id="monitor_batch",
            bash_command=(
                "redibis quality-monitor batch --all-contracts "
                "--config \"$REDIBIS_CONFIG\" "
                "--output-dir /var/redibis/reports "
                "--json"
            ),
            env=_monitor_env(),
        )
