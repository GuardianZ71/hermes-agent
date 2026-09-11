"""Fail-closed completion tests for Polaris factory outcome tasks."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
        with pytest.raises(kb.FactoryCompletionEvidenceError):
            kb.complete_factory_outcome(conn, task_id, metadata={})
        assert kb.get_task(conn, task_id).status == "ready"


def test_canonical_reconciler_requires_persisted_checkpoint(kanban_home):
    with kb.connect() as conn:
        task_id = _factory_task(conn)
        evidence = _completion_evidence(task_id)
        with pytest.raises(kb.FactoryCompletionEvidenceError):
            kb.complete_factory_outcome(conn, task_id, metadata=evidence)
        assert kb.get_task(conn, task_id).status == "ready"


def test_canonical_reconciler_completes_linked_delivery_chain(kanban_home):
    with kb.connect() as conn:
        task_id = _factory_task(conn)
        evidence = _completion_evidence(task_id)
        marker = kb._factory_delivery_checkpoint_marker(evidence["delivery_checkpoint"])
        kb.add_comment(conn, task_id, "polaris-software-factory", marker)
        assert kb.complete_factory_outcome(
            conn,
            task_id,
            result="Merged, deployed, and accepted live.",
            metadata=evidence,
        )
        assert kb.get_task(conn, task_id).status == "done"


def test_non_factory_completion_is_unchanged(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Ordinary task", initial_status="running")
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.get_task(conn, task_id).status == "done"
