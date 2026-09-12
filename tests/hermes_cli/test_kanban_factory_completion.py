"""Fail-closed completion tests for Polaris factory outcome tasks."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        kb,
        "_FACTORY_AUTHORITY_KEYS_PATH",
        home / "factory" / "authority-public-keys.json",
    )
    monkeypatch.setattr(kb, "_validate_factory_trust_path", lambda _path: None)
    kb.init_db()
    return home


def _factory_task(conn, *, issue: str = "POL-159") -> str:
    return kb.create_task(
        conn,
        title="Factory outcome",
        body="POLARIS_FACTORY_CHECKPOINT_V1 sha256:" + "a" * 64,
        created_by="polaris-software-factory",
        idempotency_key=f"polaris-software-factory:{issue.casefold()}:outcome",
        initial_status="running",
    )


def _completion_evidence(task_id: str, *, issue: str = "POL-159") -> dict:
    head = "b" * 40
    merge_sha = "c" * 40
    deployment_id = "deploy-159"
    checkpoint = {
        "schema_version": 1,
        "issue_identifier": issue,
        "outcome_idempotency_key": f"polaris-software-factory:{issue.casefold()}:outcome",
        "task_id": task_id,
        "repo": "GuardianZ71/hermes-agent",
        "branch": None,
        "pr_number": 9,
        "reviewer_id": "independent-reviewer",
        "reviewed_head_sha": head,
        "review_evidence_digest": "d" * 64,
        "deployment_mapping": "local-hermes-runtime",
    }
    return {
        "delivery_checkpoint": checkpoint,
        "hosted_ci": {"status": "success", "head_sha": head, "run_id": "ci-159"},
        "merge": {"status": "merged", "pr_number": 9, "head_sha": head, "merge_sha": merge_sha},
        "deployment": {
            "status": "success",
            "deployment_mapping": "local-hermes-runtime",
            "source_sha": merge_sha,
            "deployment_id": deployment_id,
        },
        "live_acceptance": {
            "status": "passed",
            "deployment_id": deployment_id,
            "production_readback": "healthy",
            "user_path": "factory completion reconciliation",
            "checked_at": "2026-09-11T23:00:00Z",
        },
    }


def _signed_completion_authority(
    home: Path, task_id: str, evidence: dict
) -> tuple[dict, list[dict]]:
    actions = [{
        "kind": "kanban.complete_outcome",
        "task_id": task_id,
        "expected_idempotency_key": "polaris-software-factory:pol-159:outcome",
        "completion_evidence": evidence,
    }]
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "source_kind": "completion",
        "issue_identifier": "POL-159",
        "config": {},
        "source_state": {"issue_identifier": "POL-159"},
        "actions_digest": hashlib.sha256(
            json.dumps(actions, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "issued_at": (now - timedelta(seconds=5)).isoformat(),
        "expires_at": (now + timedelta(seconds=295)).isoformat(),
        "nonce": "test-nonce",
    }
    payload_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    authorities = {}
    signatures = {}
    for source in ("linear", "github", "ci", "deployment", "live"):
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        authorities[source] = {
            "public_key": base64.b64encode(public).decode(),
            "source_kinds": ["completion"],
        }
        signatures[source] = base64.b64encode(private.sign(payload_bytes)).decode()
    registry_dir = home / "factory"
    registry_dir.mkdir(mode=0o700)
    registry = registry_dir / "authority-public-keys.json"
    registry.write_text(
        json.dumps({"schema_version": 1, "authorities": authorities}),
        encoding="utf-8",
    )
    registry.chmod(0o600)
    return {"payload": payload, "signatures": signatures}, actions


def test_worker_completion_cannot_close_factory_outcome(kanban_home):
    with kb.connect() as conn:
        task_id = _factory_task(conn)
        with pytest.raises(kb.FactoryCompletionRequiredError):
            kb.complete_task(conn, task_id, result="Reviewer found unresolved blockers.")
        assert kb.get_task(conn, task_id).status == "ready"
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event["kind"] == "completion_blocked_factory"


def test_completion_api_exposes_no_capability_token_bypass():
    parameters = inspect.signature(kb.complete_task).parameters
    assert "_factory_completion_token" not in parameters
    assert not hasattr(kb, "_FACTORY_COMPLETION_TOKEN")


def test_factory_identity_signal_alone_fails_closed(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Malformed factory task",
            created_by="worker",
            idempotency_key="polaris-software-factory:pol-159:outcome",
            initial_status="running",
        )
        with pytest.raises(kb.FactoryCompletionRequiredError):
            kb.complete_task(conn, task_id, result="done")
        with pytest.raises(kb.FactoryCompletionRequiredError):
            kb.complete_factory_outcome(conn, task_id, metadata={})
        assert kb.get_task(conn, task_id).status == "ready"


def test_canonical_reconciler_requires_persisted_checkpoint(kanban_home):
    with kb.connect() as conn:
        task_id = _factory_task(conn)
        evidence = _completion_evidence(task_id)
        envelope, actions = _signed_completion_authority(kanban_home, task_id, evidence)
        with pytest.raises(kb.FactoryCompletionEvidenceError):
            kb.complete_factory_outcome(
                conn,
                task_id,
                metadata=evidence,
                authority_envelope=envelope,
                actions=actions,
            )
        assert kb.get_task(conn, task_id).status == "ready"


def test_canonical_reconciler_completes_linked_delivery_chain(kanban_home):
    with kb.connect() as conn:
        task_id = _factory_task(conn)
        evidence = _completion_evidence(task_id)
        envelope, actions = _signed_completion_authority(kanban_home, task_id, evidence)
        marker = kb._factory_delivery_checkpoint_marker(evidence["delivery_checkpoint"])
        kb.add_comment(conn, task_id, "polaris-software-factory", marker)
        assert kb.complete_factory_outcome(
            conn,
            task_id,
            result="Merged, deployed, and accepted live.",
            metadata=evidence,
            authority_envelope=envelope,
            actions=actions,
        )
        assert kb.get_task(conn, task_id).status == "done"


def test_non_factory_completion_is_unchanged(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Ordinary task", initial_status="running")
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.get_task(conn, task_id).status == "done"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership check")
def test_factory_trust_registry_rejects_user_owned_ancestry(tmp_path):
    registry = tmp_path / "factory" / "authority-public-keys.json"
    registry.parent.mkdir(mode=0o700)
    registry.write_text("{}", encoding="utf-8")
    registry.chmod(0o400)
    with pytest.raises(kb.FactoryCompletionEvidenceError, match="root-owned"):
        kb._validate_factory_trust_path(registry)


def test_factory_completion_reservation_is_not_claimable(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = _factory_task(conn)
        evidence = _completion_evidence(task_id)
        envelope, actions = _signed_completion_authority(kanban_home, task_id, evidence)
        marker = kb._factory_delivery_checkpoint_marker(evidence["delivery_checkpoint"])
        kb.add_comment(conn, task_id, "polaris-software-factory", marker)
        original_complete = kb.complete_task
        claim_attempted = False

        def complete_after_claim_attempt(check_conn, check_task_id, **kwargs):
            nonlocal claim_attempted
            claim_attempted = True
            with kb.connect() as racing_conn:
                assert kb.claim_task(
                    racing_conn, check_task_id, claimer="racing-worker"
                ) is None
                assert kb.claim_review_task(
                    racing_conn, check_task_id, claimer="racing-reviewer"
                ) is None
            return original_complete(check_conn, check_task_id, **kwargs)

        monkeypatch.setattr(kb, "complete_task", complete_after_claim_attempt)
        assert kb.complete_factory_outcome(
            conn,
            task_id,
            metadata=evidence,
            authority_envelope=envelope,
            actions=actions,
        )
        assert claim_attempted
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"


def test_factory_completion_final_write_cas_rejects_reservation_change(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = _factory_task(conn)
        evidence = _completion_evidence(task_id)
        envelope, actions = _signed_completion_authority(kanban_home, task_id, evidence)
        marker = kb._factory_delivery_checkpoint_marker(evidence["delivery_checkpoint"])
        kb.add_comment(conn, task_id, "polaris-software-factory", marker)
        original_merge = kb._merge_completion_prose_artifacts

        def tamper_with_reservation(*args, **kwargs):
            with kb.connect() as racing_conn, kb.write_txn(racing_conn):
                racing_conn.execute(
                    "UPDATE tasks SET claim_lock = ? WHERE id = ?",
                    ("racing-review-claim", task_id),
                )
            return original_merge(*args, **kwargs)

        monkeypatch.setattr(
            kb, "_merge_completion_prose_artifacts", tamper_with_reservation
        )
        assert not kb.complete_factory_outcome(
            conn,
            task_id,
            metadata=evidence,
            authority_envelope=envelope,
            actions=actions,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.claim_lock == "racing-review-claim"
