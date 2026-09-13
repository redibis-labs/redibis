"""Structured table-column evaluation schema, scoring, CLI, and session API."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from redibis.evaluation import (
    DATASET_KIND,
    evaluate_table,
    from_column_evaluations_corpus,
    render_table_report_html,
    scaffold_from_frame,
    validate_table_dataset,
)

CORPUS = (
    Path(__file__).resolve().parent
    / "generators/pandas/v1/table_column_golden_truth_json_package"
    / "table_column_golden_truth_json/support_ticket_free_text_golden_truth.json"
)


def _dataset(columns, **extra):
    payload = {
        "kind": DATASET_KIND,
        "schema_version": "1.0",
        "table_name": "demo.customers",
        "columns": columns,
    }
    payload.update(extra)
    return payload


def test_validate_and_score_engine_actuals():
    dataset = _dataset([
        {"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"},
        {"name": "age", "is_pii": False},
    ])
    report = evaluate_table(
        dataset,
        [
            {"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"},
            {"name": "age", "is_pii": True, "entity_type": "AGE"},
        ],
        target="engine",
    )
    assert report["kind"] == "redibis.table_column_eval_report"
    assert report["pii"]["micro"]["fp"] == 1
    assert report["pii"]["micro"]["tp"] == 1
    assert report["columns"][0]["fields"]["entity_type"]["match"] is True
    age = next(c for c in report["columns"] if c["name"] == "age")
    assert age["fields"]["is_pii"]["match"] is False


def test_wrong_entity_type_and_tags():
    dataset = _dataset([
        {
            "name": "email",
            "is_pii": True,
            "entity_type": "EMAIL_ADDRESS",
            "tags": ["customer", "contact"],
        }
    ])
    report = evaluate_table(
        dataset,
        [{
            "name": "email",
            "is_pii": True,
            "entity_type": "PHONE_NUMBER",
            "tags": ["customer"],
        }],
    )
    email = report["columns"][0]
    assert email["fields"]["entity_type"]["match"] is False
    assert email["fields"]["tags"]["tp"] == 1
    assert email["fields"]["tags"]["fn"] == 1


def test_optional_definitions_skipped_when_blank():
    dataset = _dataset([
        {"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS", "description": ""}
    ])
    report = evaluate_table(
        dataset,
        [{"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS", "description": "anything"}],
    )
    assert report["columns"][0]["fields"]["description"]["skipped"] is True


def test_missing_extra_and_stale_fingerprint():
    dataset = _dataset(
        [{"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"}],
        fingerprint="aaaa",
    )
    checked = validate_table_dataset(
        dataset,
        table_columns=["email", "age"],
        table_fingerprint="bbbb",
    )
    assert "age" in checked["missing_columns"]
    assert checked["stale_fingerprint"] is True


def test_from_llm_proposals_requires_llm_score():
    from redibis.evaluation.targets import from_llm_proposals

    rows = from_llm_proposals([
        {"column": "email", "detected": True, "entity_type": "EMAIL_ADDRESS"},
        {"column": "phone", "detected": True, "entity_type": "PHONE_NUMBER", "llm_score": 0.9},
    ])
    by_name = {row["name"]: row for row in rows}
    assert by_name["email"]["is_proposal"] is False
    assert by_name["email"]["source"] == "engine"
    assert by_name["phone"]["is_proposal"] is True
    assert by_name["phone"]["source"] == "llm"


def test_run_blocks_stale_fingerprint():
    import pandas as pd
    from redibis.evaluation import TableEvalError, run_table_evaluation

    with pytest.raises(TableEvalError, match="stale"):
        run_table_evaluation(
            _dataset(
                [{"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"}],
                fingerprint="old",
            ),
            df=pd.DataFrame({"email": ["a@b.c"]}),
            target="engine",
        )


def test_llm_target_marks_proposals():
    dataset = _dataset([
        {"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"}
    ])
    report = evaluate_table(
        dataset,
        [{"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"}],
        target="llm",
    )
    assert report["columns"][0]["is_proposal"] is True


def test_extra_actual_columns_count_as_false_positives():
    report = evaluate_table(
        _dataset([{"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"}]),
        [
            {"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"},
            {"name": "ssn", "is_pii": True, "entity_type": "NATIONAL_ID"},
        ],
    )
    assert report["actual_extra_columns"] == ["ssn"]
    assert report["exact"]["micro"]["fp"] >= 1
    assert report["exact"]["micro"]["f1"] < 1.0


def test_missing_actual_is_coverage_failure():
    report = evaluate_table(
        _dataset([{"name": "status", "is_pii": False}]),
        [],
        target="contract",
    )
    assert report["exact"]["micro"]["f1"] == 0.0
    assert report["actual_missing_columns"] == ["status"]
    assert report["coverage"]["missing"] == 1
    assert report["columns"][0]["fields"]["present"]["match"] is False


def test_semantic_judge_parses_match_boolean_strictly(monkeypatch):
    import redibis.enrich.capability_routing as routing
    import redibis.telemetry.model_gateway as gateway
    from redibis.evaluation.service import _semantic_judge

    class Provider:
        name = "fake"

        def complete(self, *_args, **_kwargs):
            return '{"match": false, "reason": "not a true match"}'

    monkeypatch.setattr(
        routing,
        "get_provider_for_role",
        lambda *_args, **_kwargs: (Provider(), SimpleNamespace(model="fake")),
    )
    monkeypatch.setattr(
        gateway,
        "guarded_model_call",
        lambda fn, **_kwargs: (fn(), {}),
    )
    result = _semantic_judge(
        {"description": "customer identifier"},
        {"description": "unrelated text"},
        redibis_config=SimpleNamespace(agents=None),
    )
    assert result["description"] is False


def test_llm_target_fails_when_llm_is_disabled():
    import pandas as pd
    from redibis.config import RedibisConfig
    from redibis.evaluation import TableEvalError, run_table_evaluation

    with pytest.raises(TableEvalError, match="pii.llm.enabled"):
        run_table_evaluation(
            _dataset([{"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"}]),
            df=pd.DataFrame({"email": ["a@b.c"]}),
            target="llm",
            redibis_config=RedibisConfig(),
        )


def test_corpus_adapter_strips_sample_values_and_html_escapes():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    dataset = from_column_evaluations_corpus(corpus)
    assert dataset["kind"] == DATASET_KIND
    blob = json.dumps(dataset)
    assert "sample_values" not in blob
    assert "ID-00000001" not in blob
    assert any(col["description"] for col in dataset["columns"])
    report = evaluate_table(
        _dataset([{"name": "<script>x</script>", "is_pii": False}]),
        [{"name": "<script>x</script>", "is_pii": False, "description": "<script>x</script>"}],
    )
    html = render_table_report_html(report)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_scaffold_does_not_auto_accept_suggestions():
    import pandas as pd

    df = pd.DataFrame({"email": ["a@b.c"], "n": [1]})
    out = scaffold_from_frame(
        df,
        table_name="t",
        suggestions={"email": {"is_pii": True, "entity_type": "EMAIL_ADDRESS"}},
    )
    email = next(c for c in out["columns"] if c["name"] == "email")
    assert email["is_pii"] is False
    assert email["suggestion"]["is_pii"] is True
    assert "a@b.c" not in json.dumps(out)


def test_cli_eval_validate_and_run(tmp_path):
    from redibis.cli.eval_cmd import run_eval

    path = tmp_path / "labels.json"
    path.write_text(json.dumps(_dataset([
        {"name": "email", "is_pii": True, "entity_type": "EMAIL_ADDRESS"},
        {"name": "age", "is_pii": False},
    ])), encoding="utf-8")
    rc = run_eval(SimpleNamespace(eval_action="validate", dataset=str(path), json=True))
    assert rc == 0
    csv = tmp_path / "t.csv"
    csv.write_text("email,age\na@b.com,30\n", encoding="utf-8")
    out = tmp_path / "report.json"
    html = tmp_path / "report.html"
    rc = run_eval(SimpleNamespace(
        eval_action="run",
        dataset=str(path),
        data=str(csv),
        table="",
        target="engine",
        engines="regex",
        equation="independent",
        semantic=False,
        out=str(out),
        html=str(html),
        min_exact_f1=None,
        config=None,
    ))
    assert rc == 0, out.read_text(encoding="utf-8") if out.exists() else "no report"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["target"] == "engine"
    assert report["provenance"]["engines"] == "regex"
    assert html.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_session_eval_scaffold_validate_export_and_run():
    os.environ.setdefault("USE_LOCAL_STORAGE", "true")
    from fastapi.testclient import TestClient
    from redibis.webapp.backend import app

    client = TestClient(app)
    csv = b"email,age\na@b.com,30\n"
    created = client.post(
        "/api/sessions",
        files={"file": ("t.csv", io.BytesIO(csv), "text/csv")},
        data={"table": "demo.customers"},
    )
    if created.status_code in (401, 403):
        pytest.skip("session APIs require auth in this environment")
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    scaffold = client.get(f"/api/sessions/{sid}/eval/scaffold")
    assert scaffold.status_code == 200, scaffold.text
    body = scaffold.json()
    assert body["kind"] == DATASET_KIND
    assert "a@b.com" not in json.dumps(body)
    email = next(c for c in body["columns"] if c["name"] == "email")
    assert email["is_pii"] is False
    email["is_pii"] = True
    email["entity_type"] = "EMAIL_ADDRESS"
    checked = client.post(
        f"/api/sessions/{sid}/eval/validate",
        json={"dataset": body, "target": "engine"},
    )
    assert checked.status_code == 200, checked.text
    exported = client.post(
        f"/api/sessions/{sid}/eval/export",
        json={"dataset": body, "target": "engine"},
    )
    assert exported.status_code == 200, exported.text
    exported_blob = json.dumps(exported.json())
    assert "sample_values" not in exported_blob
    assert "suggestion" not in exported_blob
    ran = client.post(
        f"/api/sessions/{sid}/eval/run",
        json={"dataset": body, "target": "engine", "engines": "regex"},
    )
    assert ran.status_code == 200, ran.text
    report = ran.json()
    assert report["kind"] == "redibis.table_column_eval_report"
    stream = client.post(
        f"/api/sessions/{sid}/eval/run/stream",
        json={"dataset": body, "target": "engine", "engines": "regex"},
    )
    assert stream.status_code == 200, stream.text
    events = [json.loads(line) for line in stream.text.splitlines() if line.strip()]
    assert any(ev.get("event") == "result" for ev in events)
    missing = client.post(
        f"/api/sessions/{sid}/eval/run",
        json={"dataset": body, "target": "contract"},
    )
    assert missing.status_code in (404, 400)
    wrong_table = dict(body)
    wrong_table["table_name"] = "demo.orders"
    mismatch = client.post(
        f"/api/sessions/{sid}/eval/run",
        json={"dataset": wrong_table, "target": "engine", "engines": "regex"},
    )
    assert mismatch.status_code == 400
    invalid_target = client.post(
        f"/api/sessions/{sid}/eval/run",
        json={"dataset": body, "target": "engne"},
    )
    assert invalid_target.status_code == 422


def test_evaluation_package_never_writes_contracts():
    root = Path(__file__).resolve().parents[1] / "redibis" / "evaluation"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    assert "ContractStore" not in blob
    assert ".upsert(" not in blob


def test_local_openai_compat_is_credential_ready():
    from redibis.enrich.providers import provider_is_credential_ready

    assert provider_is_credential_ready({
        "name": "sglang",
        "kind": "openai_compat",
        "needs_key": False,
        "residency": "local",
        "api_base": "http://localhost:30000/v1",
    }) is True
    assert provider_is_credential_ready({
        "name": "openai",
        "kind": "openai",
        "needs_key": True,
        "api_key_env_set": False,
        "api_key_saved": False,
    }) is False
