from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[2] / "scripts" / "polaris_factory_intake.py"
spec = importlib.util.spec_from_file_location("polaris_factory_intake", MODULE)
assert spec and spec.loader
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)

SIGNER_MODULE = Path(__file__).parents[2] / "scripts" / "polaris_factory_signer.py"
signer_spec = importlib.util.spec_from_file_location("polaris_factory_signer", SIGNER_MODULE)
assert signer_spec and signer_spec.loader
signer = importlib.util.module_from_spec(signer_spec)
sys.modules[signer_spec.name] = signer
signer_spec.loader.exec_module(signer)


def test_signature_verification() -> None:
    body = b'{"type":"Issue"}'
    secret = b"secret"
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert intake.verify_signature(body, signature, secret)
    assert not intake.verify_signature(body, "0" * 64, secret)


def test_event_issue_extracts_only_issue_and_comment() -> None:
    assert intake.event_issue({"type": "Issue", "data": {"identifier": "POL-1"}}) == "POL-1"
    assert intake.event_issue({"type": "Comment", "data": {"issue": {"identifier": "POL-2"}}}) == "POL-2"
    assert intake.event_issue({"type": "Project", "data": {"identifier": "POL-3"}}) is None


def test_bootstrap_requires_external_public_registry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(intake, "FACTORY_HOME", tmp_path)

    monkeypatch.setattr(intake, "REGISTRY_PATH", tmp_path / "authority-public-keys.json")
    monkeypatch.setattr(intake, "WEBHOOK_SECRET_PATH", tmp_path / "linear-webhook-secret")
    public_registry = {
        "schema_version": 1,
        "authorities": {
            source: {"public_key": f"public-{source}", "source_kinds": sorted(kinds)}
            for source, kinds in signer.ALLOWED.items()
        },
    }
    intake.REGISTRY_PATH.write_text(json.dumps(public_registry), encoding="utf-8")

    result = intake.bootstrap(enforce_ownership=False)
    registry = json.loads((tmp_path / "authority-public-keys.json").read_text(encoding="utf-8"))
    keys = [row["public_key"] for row in registry["authorities"].values()]
    assert result["ok"] is True
    assert len(keys) == len(set(keys)) == 7
    assert stat.S_IMODE(os.stat(tmp_path / "linear-webhook-secret").st_mode) == 0o400
    assert not (tmp_path / "authorities").exists()


def test_snapshot_mapping_binds_team_as_well_as_project_and_repo(monkeypatch) -> None:
    issue = {"identifier": "POL-1", "team_key": "POL", "project_id": "p", "repository": "o/r"}
    config = {"project_mappings": [
        {"linear_team": "OTHER", "linear_project_id": "p", "repo": "o/r", "project_id": "wrong", "owner_profile": "forge", "board": "wrong"},
        {"linear_team": "POL", "linear_project_id": "p", "repo": "o/r", "project_id": "right", "owner_profile": "forge", "board": "right"},
    ]}
    monkeypatch.setattr(intake, "_task_rows", lambda board, issue: [])
    monkeypatch.setattr(intake, "_repo_state", lambda repo, issue: {"branches": [], "pull_requests": []})
    monkeypatch.setattr(intake, "HOME", Path("/tmp/no-profiles"))
    snapshot, mapping = intake.build_snapshot(issue, config)
    assert mapping["board"] == "right"
    assert snapshot["project_registry"][0]["id"] == "right"


def test_http_acknowledges_after_durable_queue_before_admission(monkeypatch, tmp_path: Path) -> None:
    secret = b"secret"
    body = json.dumps({"type": "Issue", "data": {"identifier": "POL-1"}}).encode()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    secret_path = tmp_path / "linear-webhook-secret"
    secret_path.write_bytes(secret)
    monkeypatch.setattr(intake, "WEBHOOK_SECRET_PATH", secret_path)
    monkeypatch.setattr(intake, "QUEUE_PATH", tmp_path / "intake-queue.db")
    monkeypatch.setattr(
        intake,
        "admit",
        lambda _issue: pytest.fail("HTTP handler must not run admission before acknowledgement"),
    )
    server = intake.ThreadingHTTPServer(("127.0.0.1", 0), intake.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/linear",
            data=body,
            headers={"Linear-Signature": signature, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)
            assert response.status == 200
        assert result["queued"] is True
        with intake._queue_connection() as conn:
            row = conn.execute(
                "SELECT issue_identifier, status FROM webhook_queue WHERE event_id = ?",
                (result["event_id"],),
            ).fetchone()
        assert row == ("POL-1", "queued")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_queue_recovers_interrupted_processing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(intake, "QUEUE_PATH", tmp_path / "intake-queue.db")
    now = "2026-09-08T00:00:00+00:00"
    with intake._queue_connection() as conn:
        conn.execute(
            """INSERT INTO webhook_queue
               (event_id, body, issue_identifier, status, available_at, created_at, updated_at)
               VALUES ('event', '{}', 'POL-1', 'processing', 0, ?, ?)""",
            (now, now),
        )
    intake.recover_queue()
    with intake._queue_connection() as conn:
        assert conn.execute("SELECT status FROM webhook_queue WHERE event_id = 'event'").fetchone() == ("queued",)


def test_project_authority_rejects_caller_config_drift(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted.json"
    trusted.write_text(json.dumps({"project_mappings": []}), encoding="utf-8")
    payload = {
        "source_kind": "intake",
        "issue_identifier": "POL-1",
        "config": {"project_mappings": [{"project_id": "forged"}]},
        "source_state": {"issue": {}},
    }
    with pytest.raises(ValueError, match="caller config drift"):
        signer.verify_evidence("project_registry", payload, tmp_path, trusted)


def test_authority_policy_allows_only_enabled_toggle() -> None:
    base = {"enabled": False, "project_mappings": [{"project_id": "p"}]}
    enabled = {"enabled": True, "project_mappings": [{"project_id": "p"}]}
    assert signer.authority_policy(base) == signer.authority_policy(enabled)
    changed = {"enabled": True, "project_mappings": [{"project_id": "other"}]}
    assert signer.authority_policy(base) != signer.authority_policy(changed)


def test_root_control_rejects_file_beneath_replaceable_parent(tmp_path: Path) -> None:
    trust_file = tmp_path / "authority-public-keys.json"
    trust_file.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError, match="runtime path is not root-controlled"):
        intake._require_root_controlled(trust_file)


def test_signer_command_rejects_interpreter_or_argument_substitution() -> None:
    with pytest.raises(PermissionError, match="unexpected independent signer command"):
        intake._require_signer_command(
            "linear",
            ["/usr/bin/python3", "-n", "-u", "polarislinear", "/tmp/user-controlled.py"],
        )
    with pytest.raises(PermissionError, match="unexpected independent signer command"):
        intake._require_signer_command(
            "linear",
            ["/usr/bin/sudo", "-n", "-u", "polarisproject", "/usr/local/libexec/polaris-factory-sign-linear"],
        )


def test_signer_runs_from_neutral_accessible_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "signature\n"
        stderr = ""

    def fake_run(*args: object, **kwargs: object) -> Result:
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(intake.subprocess, "run", fake_run)
    assert intake._sign("linear", {"schema_version": 1}) == "signature"
    assert captured["cwd"] == "/"


def test_authority_readonly_database_avoids_wal_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "authority.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE evidence(value TEXT)")
    conn.execute("INSERT INTO evidence VALUES ('current')")
    conn.commit()
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    assert not Path(f"{db}-wal").exists()
    os.chmod(tmp_path, 0o555)
    try:
        with signer._stable_readonly_database(db) as readonly:
            assert readonly.execute("SELECT value FROM evidence").fetchone()[0] == "current"
    finally:
        os.chmod(tmp_path, 0o755)
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_authority_readonly_database_rejects_active_wal_sidecar(tmp_path: Path) -> None:
    db = tmp_path / "authority.db"
    sqlite3.connect(db).close()
    Path(f"{db}-wal").touch()
    with pytest.raises(RuntimeError, match="active WAL sidecars"):
        with signer._stable_readonly_database(db):
            pass


def test_authority_readonly_database_rejects_path_swap_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "authority.db"
    forged = tmp_path / "forged.db"
    original = tmp_path / "original.db"
    for path, value in ((db, "ORIGINAL"), (forged, "FORGED")):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE evidence(value TEXT)")
        conn.execute("INSERT INTO evidence VALUES (?)", (value,))
        conn.commit()
        conn.close()
    real_open = os.open

    def swapping_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if Path(path) == db:
            os.replace(db, original)
            os.replace(forged, db)
            descriptor = real_open(path, flags, mode)
            os.replace(db, forged)
            os.replace(original, db)
            return descriptor
        return real_open(path, flags, mode)

    monkeypatch.setattr(signer.os, "open", swapping_open)
    with pytest.raises(RuntimeError, match="changed during open"):
        with signer._stable_readonly_database(db):
            pass
    assert sqlite3.connect(db).execute("SELECT value FROM evidence").fetchone()[0] == "ORIGINAL"
