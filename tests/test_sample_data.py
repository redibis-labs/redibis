"""Server-side sample data library — containment, listing, preview, scan.

The containment tests are the point of this file. Every path the web app
accepts is attacker-controlled, so each escape technique gets its own case.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from redibis.config import RedibisConfig
from redibis.services import sample_data as sd


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    root = tmp_path / "samples"
    (root / "nested").mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.mkdir()

    (root / "customers.csv").write_text(
        "id,name,phone\n1,Mahmoud,01012345678\n2,Sara,01199887766\n", encoding="utf-8"
    )
    (root / "nested" / "deep.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "notes.txt").write_text("not a dataset\n", encoding="utf-8")
    (root / ".hidden.csv").write_text("x\n1\n", encoding="utf-8")
    (outside / "passwords.csv").write_text("TOPSECRET\n", encoding="utf-8")

    # Two escape routes: a directory symlink and a file symlink.
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (root / "link.csv").symlink_to(outside / "passwords.csv")
        has_symlinks = True
    except (OSError, NotImplementedError):  # Windows without developer mode
        has_symlinks = False

    monkeypatch.setenv(sd.ENV_ROOT, str(root))
    monkeypatch.delenv("REDIBIS_CONFIG", raising=False)
    return SimpleNamespace(root=root, outside=outside, has_symlinks=has_symlinks)


@pytest.fixture()
def cfg():
    return RedibisConfig().sample_data


# ── containment ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("attack", [
    "../secret/passwords.csv",
    "../../etc/passwd",
    "nested/../../secret/passwords.csv",
    "nested/./../customers.csv",
    "..\\secret\\passwords.csv",
    ".hidden.csv",
])
def test_traversal_and_dot_entries_are_rejected(library, cfg, attack):
    with pytest.raises(sd.SampleDataError):
        sd.resolve("", attack, cfg)


def test_symlink_out_of_the_root_is_rejected(library, cfg):
    if not library.has_symlinks:
        pytest.skip("symlinks unavailable on this platform")
    # Resolving before the containment check is what makes this work; checking
    # the un-resolved path would let both of these through.
    with pytest.raises(sd.SampleDataError, match="escapes"):
        sd.resolve("", "escape/passwords.csv", cfg)
    with pytest.raises(sd.SampleDataError, match="escapes"):
        sd.resolve("", "link.csv", cfg)


def test_absolute_path_cannot_escape(library, cfg):
    with pytest.raises(sd.SampleDataError):
        sd.resolve("", "/etc/passwd", cfg)


def test_absolute_path_inside_the_root_is_accepted(library, cfg):
    inside = library.root / "customers.csv"
    assert sd.resolve("", str(inside), cfg) == inside.resolve()
    preview = sd.preview("", str(inside), cfg=cfg)
    assert preview["path"] == "customers.csv"
    assert preview["name"] == "customers.csv"


def test_unlisted_extension_is_rejected(library, cfg):
    with pytest.raises(sd.SampleDataError, match="unsupported file type"):
        sd.resolve("", "notes.txt", cfg)


def test_empty_path_is_rejected(library, cfg):
    with pytest.raises(sd.SampleDataError):
        sd.resolve("", "", cfg)


def test_directory_is_not_a_file(library, cfg):
    with pytest.raises(sd.SampleDataError, match="not a file"):
        sd.resolve("", "nested", cfg)


def test_size_cap_is_enforced(library, monkeypatch):
    small = RedibisConfig.from_dict({"sample_data": {"max_file_mb": 0}}).sample_data
    with pytest.raises(sd.SampleDataError, match="max_file_mb"):
        sd.resolve("", "customers.csv", small)


def test_legitimate_paths_resolve(library, cfg):
    assert sd.resolve("", "customers.csv", cfg).name == "customers.csv"
    assert sd.resolve("", "nested/deep.csv", cfg).name == "deep.csv"


def test_single_dot_segments_are_normalized_not_rejected(library, cfg):
    """A lone "." is a no-op that cannot escape, so it is stripped rather than
    refused. ".." is a different matter and is rejected by the dot-prefix rule."""
    assert sd.resolve("", "./customers.csv", cfg) == sd.resolve("", "customers.csv", cfg)
    assert sd.resolve("", "nested/./deep.csv", cfg).name == "deep.csv"


# ── listing ──────────────────────────────────────────────────────────────

def test_listing_hides_dotfiles_symlinks_and_other_extensions(library, cfg):
    paths = {f["path"] for f in sd.list_files("", cfg)["files"]}
    assert paths == {"customers.csv", "nested/deep.csv"}


def test_listing_marks_oversized_files_instead_of_hiding_them(library):
    small = RedibisConfig.from_dict({"sample_data": {"max_file_mb": 0}}).sample_data
    files = {f["path"]: f for f in sd.list_files("", small)["files"]}
    assert files["customers.csv"]["too_large"] is True


def test_depth_limit_is_respected(library, tmp_path):
    deep = tmp_path / "samples" / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "buried.csv").write_text("z\n1\n", encoding="utf-8")
    shallow = RedibisConfig.from_dict({"sample_data": {"max_depth": 1}}).sample_data
    paths = {f["path"] for f in sd.list_files("", shallow)["files"]}
    assert "a/b/c/d/e/buried.csv" not in paths


def test_webapp_samples_is_the_root_when_nothing_is_configured(tmp_path, monkeypatch, cfg):
    monkeypatch.delenv(sd.ENV_ROOT, raising=False)
    monkeypatch.delenv("REDIBIS_CONFIG", raising=False)
    samples = tmp_path / "webapp" / "samples"
    samples.mkdir(parents=True)
    (samples / "demo.csv").write_text("a\n1\n", encoding="utf-8")
    monkeypatch.setattr(sd, "default_root_path", lambda: samples.resolve())
    monkeypatch.setattr(sd, "default_tests_data_path", lambda: None)
    assert sd.is_enabled(cfg) is True
    roots = sd.list_roots(cfg)
    assert len(roots) == 1
    assert roots[0].name == "samples"
    assert roots[0].path == samples.resolve()
    assert {f["path"] for f in sd.list_files("", cfg)["files"]} == {"demo.csv"}


def test_default_root_path_is_webapp_samples():
    path = sd.default_root_path()
    assert path is not None
    assert path.as_posix().endswith("redibis/webapp/samples")


def test_default_tests_data_path_is_repo_fixtures_when_present():
    path = sd.default_tests_data_path()
    if path is None:
        pytest.skip("tests/data is not next to this install")
    assert path.as_posix().endswith("tests/data")
    assert (path / "realistic_eshop_customer_account.csv").is_file()


def test_tests_data_is_a_default_root_when_present(tmp_path, monkeypatch, cfg):
    monkeypatch.delenv(sd.ENV_ROOT, raising=False)
    monkeypatch.delenv("REDIBIS_CONFIG", raising=False)
    samples = tmp_path / "webapp" / "samples"
    samples.mkdir(parents=True)
    tests_data = tmp_path / "tests" / "data"
    tests_data.mkdir(parents=True)
    (tests_data / "realistic_eshop_customer_account.csv").write_text(
        "id,email\n1,a@b.com\n", encoding="utf-8"
    )
    monkeypatch.setattr(sd, "default_root_path", lambda: samples.resolve())
    monkeypatch.setattr(sd, "default_tests_data_path", lambda: tests_data.resolve())
    roots = sd.list_roots(cfg)
    assert [r.name for r in roots] == ["samples", "tests-data"]
    assert roots[1].path == tests_data.resolve()
    found = sd.resolve("", "realistic_eshop_customer_account.csv", cfg)
    assert found == (tests_data / "realistic_eshop_customer_account.csv").resolve()
    abs_path = str(tests_data / "realistic_eshop_customer_account.csv")
    assert sd.resolve("", abs_path, cfg) == found
    monkeypatch.chdir(tmp_path)
    assert sd.resolve("", "tests/data/realistic_eshop_customer_account.csv", cfg) == found


def test_absolute_path_outside_roots_names_the_allowed_roots(library, cfg):
    outside = str(library.outside / "passwords.csv")
    with pytest.raises(sd.SampleDataError) as caught:
        sd.resolve("", outside, cfg)
    message = str(caught.value)
    assert "outside the sample data root" in message
    assert str(library.root.resolve()) in message


def test_config_roots_support_named_mappings(tmp_path, monkeypatch):
    monkeypatch.delenv(sd.ENV_ROOT, raising=False)
    monkeypatch.delenv("REDIBIS_CONFIG", raising=False)
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a.csv").write_text("a\n1\n", encoding="utf-8")
    (two / "b.csv").write_text("b\n2\n", encoding="utf-8")
    cfg = RedibisConfig.from_dict({"sample_data": {"roots": [
        str(one),
        {"name": "demo", "label": "Demo data", "path": str(two)},
    ]}}).sample_data
    names = [r.name for r in sd.list_roots(cfg)]
    assert "demo" in names
    assert "cwd" not in names
    assert {f["path"] for f in sd.list_files("demo", cfg)["files"]} == {"b.csv"}
    with pytest.raises(sd.SampleDataError, match="unknown sample data root"):
        sd.resolve("nope", "b.csv", cfg)


# ── preview ──────────────────────────────────────────────────────────────

def test_preview_returns_columns_and_rows(library, cfg):
    out = sd.preview("", "customers.csv", rows=5, cfg=cfg)
    assert out["columns"] == ["id", "name", "phone"]
    assert out["row_count"] == 2
    assert out["row_count_exact"] is True
    assert out["rows"][0] == ["1", "Mahmoud", "01012345678"]


def test_preview_goes_through_the_same_gate(library, cfg):
    with pytest.raises(sd.SampleDataError):
        sd.preview("", "../secret/passwords.csv", cfg=cfg)


# ── HTTP surface ─────────────────────────────────────────────────────────

@pytest.fixture()
def client(library, tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_AUTH_ENABLED", "false")
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_out"))
    from fastapi.testclient import TestClient

    from redibis.webapp.backend import app

    return TestClient(app)


def test_index_lists_files(client):
    body = client.get("/api/sample-data").json()
    assert body["enabled"] is True
    assert {f["path"] for f in body["files"]} == {"customers.csv", "nested/deep.csv"}


def test_index_falls_back_to_webapp_samples_when_env_unset(client, tmp_path, monkeypatch):
    monkeypatch.delenv(sd.ENV_ROOT, raising=False)
    samples = tmp_path / "webapp" / "samples"
    samples.mkdir(parents=True)
    (samples / "boot.csv").write_text("x\n1\n", encoding="utf-8")
    monkeypatch.setattr(sd, "default_root_path", lambda: samples.resolve())
    monkeypatch.setattr(sd, "default_tests_data_path", lambda: None)
    body = client.get("/api/sample-data").json()
    assert body["enabled"] is True
    assert body["root"] == "samples"
    assert {f["path"] for f in body["files"]} == {"boot.csv"}


@pytest.mark.parametrize("attack", [
    "../secret/passwords.csv", "escape/passwords.csv", "link.csv", "notes.txt",
])
def test_preview_endpoint_rejects_escapes(client, attack):
    resp = client.get("/api/sample-data/preview", params={"path": attack})
    assert resp.status_code == 400
    assert "TOPSECRET" not in resp.text


def test_scan_endpoint_creates_a_session(client):
    resp = client.post("/api/sessions/from-sample", json={"path": "customers.csv"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"]
    assert body["source"] == {"kind": "sample_data", "root": "", "path": "customers.csv"}
    cols = client.get(f"/api/sessions/{body['session_id']}/data/columns").json()
    assert [c["column"] for c in cols["columns"]] == ["id", "name", "phone"]


def test_scan_endpoint_accepts_absolute_path_inside_the_root(client, library):
    abs_path = str((library.root / "customers.csv").resolve())
    resp = client.post("/api/sessions/from-sample", json={"path": abs_path})
    assert resp.status_code == 200, resp.text


def test_scan_endpoint_rejects_escapes(client):
    resp = client.post(
        "/api/sessions/from-sample", json={"path": "../secret/passwords.csv"}
    )
    assert resp.status_code == 400


def test_allow_scan_false_blocks_scanning_but_not_browsing(client, tmp_path, monkeypatch):
    cfg_path = tmp_path / "redibis.yaml"
    cfg_path.write_text(
        "sample_data:\n  allow_scan: false\n  roots:\n    - "
        + json.dumps(str(tmp_path / "samples"))
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIBIS_CONFIG", str(cfg_path))
    monkeypatch.delenv(sd.ENV_ROOT, raising=False)
    assert client.get("/api/sample-data").json()["enabled"] is True
    resp = client.post("/api/sessions/from-sample", json={"path": "customers.csv"})
    assert resp.status_code == 403


def test_scan_button_posts_from_sample_not_a_null_file():
    """Picking a server sample must not POST FormData(file=null) to /api/sessions."""
    js = (
        Path(__file__).resolve().parents[1]
        / "redibis"
        / "webapp"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    assert "function createSessionFromCurrentSource" in js
    assert 'POST("/api/sessions/from-sample"' in js
    do_scan = js.split("async function doScan")[1].split("async function ")[0]
    assert "createSessionFromCurrentSource" in do_scan
    assert 'POST("/api/sessions",' not in do_scan
    assert js.count('fd.append("file",S.file)') == 1
    helper = js.split("async function createSessionFromCurrentSource")[1].split(
        "async function ensureSession"
    )[0]
    assert "S.sampleRef" in helper
    assert 'POST("/api/sessions/from-sample"' in helper
    assert 'POST("/api/sessions",' in helper
    ensure = js.split("async function ensureSession")[1].split("function ")[0]
    assert "createSessionFromCurrentSource" in ensure
    for fn in (
        "executeQualityProfiler",
        "executeQualityScanBypass",
        "executeQualityScanUpload",
        "executePiiScan",
        "loadSample",
    ):
        body = js.split("async function " + fn)[1].split("async function ")[0]
        assert "createSessionFromCurrentSource" in body
        assert 'fd.append("file",S.file)' not in body


def test_homepage_is_a_csv_path_box_not_a_dropzone():
    js = (
        Path(__file__).resolve().parents[1]
        / "redibis"
        / "webapp"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    assert "function csvPathPickerHtml" in js
    assert "function sampleRootEntries" in js
    assert 'DEFAULT_SAMPLE_CSV="tests/data/realistic_eshop_customer_account.csv"' in js
    assert "function loadTypedCsv" in js
    assert "function isCsvPath" in js
    assert "only .csv files are accepted" in js
    assert "getElementById('fi')" in js
    assert "Upload a CSV from this computer" in js
    assert "csv-root-print" in js
    assert ">upload</button>" in js
    assert "drop your CSV here" not in js
    assert "function uploadDropzoneHtml" not in js
