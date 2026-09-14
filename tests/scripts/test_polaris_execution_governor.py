from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path


import pytest

MODULE = Path(__file__).parents[2] / "scripts" / "polaris_execution_governor.py"
spec = importlib.util.spec_from_file_location("polaris_execution_governor", MODULE)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def task(task_id: str, *, board: str = "surveyor", title: str = "Build", body: str = "[linear:POL-1]",
         assignee: str = "forge", status: str = "ready", priority: int = 0,
         comments: str = "") -> mod.Task:
    return mod.Task(board, task_id, title, body, assignee, status, priority, 1, comments)


def test_same_pr_multistage_chain_is_not_duplicate_writer():
    tasks = [
        task("build", title="Build fix", body="[linear:POL-126] PR #319", status="running"),
        task("review", title="Exact-head review PR #319", body="[linear:POL-126]", status="todo"),
        task("release", title="Release PR #319", body="[linear:POL-126]", status="todo"),
    ]
    result = mod.analyze(tasks, [], [], mod.Policy())
    assert result["writerCollisions"] == []
    chain = next(item for item in result["outcomeChains"] if item["outcome"] == "POL-126")
    assert [stage["stage"] for stage in chain["stages"]] == [
        "build", "exact_head_review", "release", "live_acceptance"
    ]
    assert chain["stages"][0]["state"] == "running"


def test_post_deploy_live_acceptance_is_not_downgraded_to_release():
    assert task("accept", title="Post-deploy live acceptance").stage == "live_acceptance"


def test_canonical_outcome_title_is_projected_without_linear_marker():
    assert task("outcome", title="[POL-126] outcome: Remove radar veil", body="").outcome == "POL-126"


def test_conflicting_outcome_markers_are_held_and_repaired():
    tasks = [
        task(
            "ambiguous",
            title="[linear:POL-126] Radar helper",
            body="[linear:POL-135] Roads dependency",
        ),
        task("radar", title="[linear:POL-126] Release Radar", status="todo"),
    ]

    result = mod.analyze(
        tasks,
        [("surveyor", "ambiguous", "radar")],
        [],
        mod.Policy(),
    )

    assert tasks[0].outcome is None
    assert result["crossOutcomeDependencies"][0]["classification"] == "ambiguous_outcome"
    assert result["dependencyRepairCandidates"] == [{
        "board": "surveyor",
        "parentId": "ambiguous",
        "childId": "radar",
        "reason": "ambiguous_outcome",
    }]
    assert result["heldCandidates"][0]["domains"] == ["ambiguous-outcome"]
    assert any(item["id"].startswith("ambiguous-outcome:") for item in result["incidents"])


def test_ambiguous_review_task_is_held_from_admission(tmp_path: Path):
    tasks = [task(
        "review",
        title="Review exact head",
        body="[linear:POL-126] [linear:POL-135]",
        status="review",
    )]
    analyzed = mod.analyze(tasks, [], [], mod.Policy())
    assert analyzed["heldCandidates"] == [{
        "task": mod._task_ref(tasks[0]),
        "domains": ["ambiguous-outcome"],
    }]


@pytest.mark.parametrize("title", ["Build release automation", "Fix deploy failure"])
def test_build_titles_with_release_words_remain_build_stage(title):
    assert task("build", title=title).stage == "build"


def test_review_finding_remediation_remains_a_build_writer():
    item = task("fix", title="Remediate PR #315 review findings")
    assert item.stage == "build"
    assert item.writer is True


def test_two_active_build_writers_in_one_outcome_are_collision():
    tasks = [task("a", status="running"), task("b", status="running")]
    result = mod.analyze(tasks, [], [], mod.Policy())
    domains = {item["domain"] for item in result["writerCollisions"]}
    assert "outcome:POL-1" in domains


def test_cross_outcome_edges_surface_without_auto_repair():
    tasks = [task("parent", body="[linear:POL-144]", status="running"),
             task("child", body="[linear:POL-114]", status="todo")]
    result = mod.analyze(tasks, [("surveyor", "parent", "child")], [], mod.Policy())
    assert result["crossOutcomeDependencies"][0]["classification"] == "unbound_cross_outcome"
    assert result["crossOutcomeDependencies"][0]["bothNonterminal"] is True
    assert any(item["id"].startswith("cross-outcome:") for item in result["incidents"])


def test_archived_parent_cross_outcome_edge_is_historical_not_repairable():
    parent = task("parent", body="[linear:POL-144]", status="archived")
    child = task("child", body="[linear:POL-114]", status="ready")

    result = mod.analyze(
        [parent, child],
        [("surveyor", "parent", "child")],
        [],
        mod.Policy(),
    )

    edge = result["crossOutcomeDependencies"][0]
    assert edge["bothNonterminal"] is False
    assert edge["repairEligible"] is False
    assert result["dependencyRepairCandidates"] == []


def test_label_only_artifact_dependency_is_not_concrete_evidence():
    tasks = [task("parent", body="[linear:POL-144] [dependency:artifact]"),
             task("child", body="[linear:POL-114] [dependency:artifact]")]
    result = mod.analyze(tasks, [("surveyor", "parent", "child")], [], mod.Policy())
    assert result["crossOutcomeDependencies"][0]["classification"] == "unbound_cross_outcome"
    assert result["crossOutcomeDependencies"][0]["repairEligible"] is True


def test_matching_immutable_artifact_identity_allows_cross_outcome_dependency():
    marker = f"[dependency:artifact:roads.pmtiles@sha256:{'a' * 64}]"
    tasks = [task("parent", body=f"[linear:POL-135] {marker}"),
             task("child", title="Release Radar", body=f"[linear:POL-126] {marker}")]
    result = mod.analyze(tasks, [("surveyor", "parent", "child")], [], mod.Policy())
    edge = result["crossOutcomeDependencies"][0]
    assert edge["classification"] == "immutable_artifact"
    assert edge["evidence"] == {
        "kind": "artifact",
        "name": "roads.pmtiles",
        "sha256": "a" * 64,
    }
    assert edge["repairEligible"] is False
    assert not any(item["id"].startswith("cross-outcome:") for item in result["incidents"])


def test_matching_active_exclusive_collision_lease_allows_dependency():
    lease = "surveyor-production-release"
    marker = f"[dependency:collision:{lease}] [collision-domain:{lease}]"
    tasks = [task("parent", body=f"[linear:POL-135] {marker}", status="running"),
             task("child", title="Release Radar", body=f"[linear:POL-126] {marker}", status="todo")]
    result = mod.analyze(tasks, [("surveyor", "parent", "child")], [], mod.Policy())
    edge = result["crossOutcomeDependencies"][0]
    assert edge["classification"] == "active_collision_lease"
    assert edge["evidence"] == {"kind": "collision", "domain": lease}
    assert edge["repairEligible"] is False


def test_collision_marker_without_active_exclusive_holder_is_rejected():
    lease = "surveyor-production-release"
    marker = f"[dependency:collision:{lease}] [collision-domain:{lease}]"
    tasks = [task("parent", body=f"[linear:POL-135] {marker}", status="blocked"),
             task("other", body=f"[linear:POL-140] [collision-domain:{lease}]", status="running"),
             task("child", title="Release Radar", body=f"[linear:POL-126] {marker}", status="todo")]
    result = mod.analyze(tasks, [("surveyor", "parent", "child")], [], mod.Policy())
    edge = result["crossOutcomeDependencies"][0]
    assert edge["classification"] == "unbound_cross_outcome"
    assert edge["repairEligible"] is True


def test_radar_release_is_released_from_unrelated_roads_outcome(tmp_path: Path):
    root = tmp_path / "boards"
    for slug in mod.BOARDS:
        conn = _make_board(root, slug)
        conn.close()
    conn = sqlite3.connect(root / "surveyor" / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", (
        "roads", "[linear:POL-135] Repair Roads lineage", "", "surveyor-agent", "running", 10, 1,
    ))
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", (
        "radar", "Release Radar", "[linear:POL-126] Reuse unchanged Roads artifact", "surveyor-agent", "todo", 20, 2,
    ))
    conn.execute("INSERT INTO task_links VALUES (?,?)", ("roads", "radar"))
    conn.commit(); conn.close()
    repaired = []

    def repair(candidates, repair_root):
        repaired.extend(candidates)
        conn = sqlite3.connect(repair_root / "surveyor" / "kanban.db")
        conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", ("roads", "radar"))
        conn.execute("UPDATE tasks SET status='ready' WHERE id='radar'")
        conn.commit(); conn.close()
        return [{"board": "surveyor", "parentId": "roads", "childId": "radar", "status": "unlinked"}]

    result = mod.run_once(
        root=root,
        state_path=tmp_path / "state.json",
        usage=mod.Usage(True, 95, 5, None, "pro", mod.iso()),
        dry_run=False,
        dispatch=lambda *args: {"spawned": []},
        repair=repair,
    )

    assert repaired == [{
        "board": "surveyor", "parentId": "roads", "childId": "radar",
        "reason": "unbound_cross_outcome",
    }]
    assert result["safeRepair"]["unlinked"] == [{
        "board": "surveyor", "parentId": "roads", "childId": "radar", "status": "unlinked",
    }]
    assert next(task for task in result["outcomeChains"] if task["outcome"] == "POL-126")["stages"][2]["state"] == "pending"


def test_dependency_repair_uses_idempotent_unlink_lifecycle(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "different-hermes-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    root = tmp_path / "custom-polaris-boards"
    db_path = root / "surveyor" / "kanban.db"
    db_path.parent.mkdir(parents=True)
    (db_path.parent / "board.json").write_text(json.dumps({"slug": "surveyor"}))
    with kb.connect_closing(db_path=db_path) as conn:
        parent = kb.create_task(conn, title="Roads")
        child = kb.create_task(conn, title="Radar", parents=[parent])
        kb.add_comment(conn, parent, "test", "[linear:POL-135]")
        kb.add_comment(conn, child, "test", "[linear:POL-126]")
        child_task = kb.get_task(conn, child)
        assert child_task is not None
        assert child_task.status == "todo"
    candidate = {
        "board": "surveyor",
        "parentId": parent,
        "childId": child,
        "reason": "unbound_cross_outcome",
    }
    for slug in mod.BOARDS:
        with kb.connect_closing(db_path=root / slug / "kanban.db"):
            pass

    original_boundary = kb._execute_boundary_with_retry
    failed_target_commit = False

    def fail_one_target_commit(conn, sql):
        nonlocal failed_target_commit
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        if not failed_target_commit and sql == "COMMIT" and db_file.endswith("/surveyor/kanban.db"):
            failed_target_commit = True
            raise sqlite3.OperationalError("injected target commit failure")
        return original_boundary(conn, sql)

    monkeypatch.setattr(kb, "_execute_boundary_with_retry", fail_one_target_commit)
    failed = mod.repair_cross_outcome_edges([candidate], root)
    assert failed[0]["status"] == "error"
    with kb.connect_closing(db_path=db_path) as conn:
        assert kb.parent_ids(conn, child) == [parent]
    monkeypatch.setattr(kb, "_execute_boundary_with_retry", original_boundary)

    original_check = kb._check_file_length_invariant
    failed_peer_check = False

    def fail_one_read_only_peer_check(conn):
        nonlocal failed_peer_check
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        if not failed_peer_check and db_file.endswith("/apex/kanban.db"):
            failed_peer_check = True
            raise RuntimeError("injected read-only peer post-commit failure")
        original_check(conn)

    monkeypatch.setattr(kb, "_check_file_length_invariant", fail_one_read_only_peer_check)

    first = mod.repair_cross_outcome_edges([candidate], root)
    second = mod.repair_cross_outcome_edges([candidate], root)

    assert first == [{
        "board": "surveyor", "parentId": parent, "childId": child, "status": "unlinked",
    }]
    assert second == [{
        "board": "surveyor", "parentId": parent, "childId": child, "status": "already_absent",
    }]
    with kb.connect_closing(db_path=db_path) as conn:
        child_task = kb.get_task(conn, child)
        assert child_task is not None
        assert child_task.status == "ready"
        assert [event.kind for event in kb.list_events(conn, child)].count("unlinked") == 1


def test_dependency_repair_revalidates_evidence_under_lock(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    root = tmp_path / "kanban" / "boards"
    db_path = root / "surveyor" / "kanban.db"
    db_path.parent.mkdir(parents=True)
    (db_path.parent / "board.json").write_text(json.dumps({"slug": "surveyor"}))
    marker = f"[dependency:artifact:roads.pmtiles@sha256:{'b' * 64}]"
    with kb.connect_closing(db_path=db_path) as conn:
        parent = kb.create_task(conn, title="Roads")
        child = kb.create_task(conn, title="Radar", parents=[parent])
        kb.add_comment(conn, parent, "test", f"[linear:POL-135] {marker}")
        kb.add_comment(conn, child, "test", f"[linear:POL-126] {marker}")

    candidate = {
        "board": "surveyor",
        "parentId": parent,
        "childId": child,
        "reason": "unbound_cross_outcome",
    }
    result = mod.repair_cross_outcome_edges([candidate], root)

    assert result == [{
        "board": "surveyor",
        "parentId": parent,
        "childId": child,
        "status": "retained_valid",
    }]
    with kb.connect_closing(db_path=db_path) as conn:
        assert kb.parent_ids(conn, child) == [parent]
        child_task = kb.get_task(conn, child)
        assert child_task is not None
        assert child_task.status == "todo"


def test_dependency_repair_revalidates_terminal_status_under_lock(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    root = tmp_path / "kanban" / "boards"
    db_path = root / "surveyor" / "kanban.db"
    with kb.connect_closing(db_path=db_path) as conn:
        parent = kb.create_task(conn, title="Roads")
        child = kb.create_task(conn, title="Radar", parents=[parent])
        kb.add_comment(conn, parent, "test", "[linear:POL-135]")
        kb.add_comment(conn, child, "test", "[linear:POL-126]")
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (parent,))
        conn.commit()

    result = mod.repair_cross_outcome_edges([{
        "board": "surveyor",
        "parentId": parent,
        "childId": child,
        "reason": "unbound_cross_outcome",
    }], root)

    assert result == [{
        "board": "surveyor",
        "parentId": parent,
        "childId": child,
        "status": "retained_historical",
    }]
    with kb.connect_closing(db_path=db_path) as conn:
        assert kb.parent_ids(conn, child) == [parent]


@pytest.mark.parametrize(
    ("second_holder", "expected_status", "expected_linked"),
    [(False, "retained_valid", True), (True, "unlinked", False)],
)
def test_collision_repair_uses_fleet_wide_exclusive_snapshot(
    tmp_path: Path,
    monkeypatch,
    second_holder: bool,
    expected_status: str,
    expected_linked: bool,
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    root = tmp_path / "kanban" / "boards"
    domain = "surveyor-release"
    marker = f"[dependency:collision:{domain}] [collision-domain:{domain}]"
    surveyor_db = root / "surveyor" / "kanban.db"
    with kb.connect_closing(db_path=surveyor_db) as conn:
        parent = kb.create_task(conn, title="Roads")
        child = kb.create_task(conn, title="Radar", parents=[parent])
        kb.add_comment(conn, parent, "test", f"[linear:POL-135] {marker}")
        kb.add_comment(conn, child, "test", f"[linear:POL-126] {marker}")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        conn.commit()
    if second_holder:
        with kb.connect_closing(db_path=root / "apex" / "kanban.db") as conn:
            other = kb.create_task(conn, title="Other writer", body=f"[collision-domain:{domain}]")
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
            conn.commit()

    result = mod.repair_cross_outcome_edges([{
        "board": "surveyor",
        "parentId": parent,
        "childId": child,
        "reason": "unbound_cross_outcome",
    }], root)

    assert result[0]["status"] == expected_status
    with kb.connect_closing(db_path=surveyor_db) as conn:
        assert (parent in kb.parent_ids(conn, child)) is expected_linked


def test_profile_occupancy_is_fleet_wide_and_overage_drains_without_kill():
    tasks = [task("a", board="polaris-ops", status="running"),
             task("b", board="surveyor", status="running"),
             task("c", board="acqlens", status="running")]
    result = mod.analyze(tasks, [], [], mod.Policy())
    assert result["occupancy"]["byProfile"] == {"forge": 3}
    incident = next(item for item in result["incidents"] if item["id"] == "profile-occupancy-over-cap:forge")
    assert incident["current"] == 3
    assert "drain without killing" in incident["message"]


def test_unreadable_board_closes_admission_with_stable_incident(tmp_path: Path):
    usage = mod.Usage(True, 95, 5, None, "pro", mod.iso())
    calls = []
    result = mod.run_once(root=tmp_path, state_path=tmp_path / "state.json", usage=usage,
                          dry_run=True, dispatch=lambda *args: calls.append(args) or {})
    assert result["state"] == "red"
    assert result["capacity"] == 0
    assert calls == []
    assert [item["id"] for item in result["integrity"]["incidents"]].count("occupancy-unreadable") == 1


def test_missing_or_mismatched_board_authority_closes_admission(tmp_path: Path):
    root = tmp_path / "boards"
    for slug in mod.BOARDS:
        conn = _make_board(root, slug)
        conn.close()
    (root / "surveyor" / "board.json").write_text(json.dumps({"slug": "surveyor"}))
    usage = mod.Usage(True, 95, 5, None, "pro", mod.iso())
    calls = []
    result = mod.run_once(
        root=root, state_path=tmp_path / "state.json", usage=usage, dry_run=True,
        dispatch=lambda *args: calls.append(args) or {},
    )
    assert result["state"] == "red"
    assert result["capacity"] == 0
    assert all(args[0] != "surveyor" for args in calls)
    assert "surveyor:authority-mismatch" in result["integrity"]["incidents"][0]["boards"]


def _make_board(root: Path, slug: str) -> sqlite3.Connection:
    folder = root / slug
    folder.mkdir(parents=True)
    (folder / "board.json").write_text(json.dumps({
        "slug": slug,
        "admission_authority": mod.ADMISSION_AUTHORITY,
    }))
    conn = sqlite3.connect(folder / "kanban.db")
    conn.executescript("""
      CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT,body TEXT,assignee TEXT,status TEXT,priority INTEGER,created_at INTEGER);
      CREATE TABLE task_links(parent_id TEXT,child_id TEXT);
      CREATE TABLE task_comments(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,body TEXT);
    """)
    return conn


def test_real_schema_dry_run_exports_canonical_payload_without_dispatch(tmp_path: Path):
    root = tmp_path / "boards"
    for slug in mod.BOARDS:
        conn = _make_board(root, slug)
        conn.close()
    conn = sqlite3.connect(root / "surveyor" / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", ("writer", "Build PR #319", "[linear:POL-126]", "forge", "running", 0, 1))
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", ("review", "Review PR #319", "[linear:POL-126]", "default", "todo", 0, 2))
    conn.commit(); conn.close()
    usage = mod.Usage(True, 95, 5, None, "pro", mod.iso())
    state_path = tmp_path / "state.json"
    calls = []

    def dispatch(slug, slots, dry_run, excluded, admitted, policy):
        calls.append((slug, slots, list(excluded), list(admitted)))
        return {"spawned": []}

    result = mod.run_once(root=root, state_path=state_path, usage=usage, dry_run=True,
                          dispatch=dispatch)
    assert result["version"] == 2
    assert result["activeWorkers"] == 1
    assert result["outcomeChains"][0]["outcome"] == "POL-126"
    assert [call[0] for call in calls] == list(mod.BOARDS)
    assert all(call[3] == [] for call in calls)
    assert state_path.exists()


def test_ready_writer_siblings_reserve_one_candidate_and_hold_the_rest():
    tasks = [task("first", priority=10), task("second", priority=1),
             task("review", title="Review PR #282", body="[linear:POL-1]", priority=20)]
    result = mod.analyze(tasks, [], [], mod.Policy())
    assert [item["task"]["taskId"] for item in result["heldCandidates"]] == ["second"]


def test_pr_domain_can_come_from_comments(tmp_path: Path):
    root = tmp_path / "boards"
    for slug in mod.BOARDS:
        conn = _make_board(root, slug)
        conn.close()
    conn = sqlite3.connect(root / "surveyor" / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", ("a", "Build A", "[linear:POL-1]", "forge", "running", 2, 1))
    conn.execute("INSERT INTO task_comments(task_id,body) VALUES (?,?)", ("a", "Opened https://github.com/example/repo/pull/319"))
    conn.commit(); conn.close()
    tasks, _, unreadable = mod.read_fleet(root)
    assert unreadable == []
    assert "pr:example/repo#319" in next(task for task in tasks if task.task_id == "a").domains


def test_collision_exclusion_does_not_close_unrelated_admission(tmp_path: Path):
    root = tmp_path / "boards"
    for slug in mod.BOARDS:
        conn = _make_board(root, slug)
        conn.close()
    conn = sqlite3.connect(root / "surveyor" / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", ("owner", "Build", "[linear:POL-1]", "forge", "running", 10, 1))
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", ("duplicate", "Build", "[linear:POL-1]", "forge", "ready", 9, 2))
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", ("unrelated", "Build", "[linear:POL-2]", "beta", "ready", 8, 3))
    conn.commit(); conn.close()
    calls = []

    def dispatch(slug, slots, dry_run, excluded, admitted, policy):
        calls.append(
            (slug, slots, dry_run, list(excluded), list(admitted), policy)
        )
        return {"spawned": []}

    usage = mod.Usage(True, 95, 5, None, "pro", mod.iso())
    result = mod.run_once(root=root, state_path=tmp_path / "state.json", usage=usage,
                          dry_run=True, dispatch=dispatch)
    assert [call[0] for call in calls] == list(mod.BOARDS)
    surveyor = next(call for call in calls if call[0] == "surveyor")
    assert surveyor == (
        "surveyor", 4, True, ["duplicate"], ["unrelated"], mod.Policy()
    )
    assert all(call[4] == [] for call in calls if call[0] != "surveyor")
    assert result["safeRepair"] == {
        "mode": "supported_dependency_unlink_and_dispatch_exclusion",
        "mutatedCards": False,
        "mutatedDependencies": False,
        "candidates": [],
        "unlinked": [],
    }


def test_native_dispatch_pins_fleet_caps_and_exclusions(monkeypatch):
    from hermes_cli import kanban_db as kb

    captured = {}

    class Connection:
        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kb, "connect", lambda **kwargs: Connection())

    def dispatch_once(conn, **kwargs):
        captured["conn"] = conn
        captured["kwargs"] = kwargs
        return kb.DispatchResult()

    monkeypatch.setattr(mod, "board_dispatch_cap", lambda slug, slots: 7)
    monkeypatch.setattr(mod, "admission_authority_errors", lambda root=mod.BOARD_ROOT: [])
    monkeypatch.setattr(kb, "dispatch_once", dispatch_once)
    policy = mod.Policy(max_background_workers=7, max_workers_per_profile=1)
    result = mod.native_dispatch(
        "surveyor",
        3,
        True,
        ["t_two", "t_one"],
        ["t_three"],
        policy,
    )

    assert result["status"] == "ok"
    assert captured["closed"] is True
    assert captured["kwargs"] == {
        "board": "surveyor",
        "dry_run": True,
        "max_spawn": 7,
        "max_in_progress": 7,
        "failure_limit": 2,
        "max_in_progress_per_profile": 1,
        "excluded_task_ids": ["t_one", "t_two"],
        "admitted_task_ids": ["t_three"],
        "admission_authority": mod.ADMISSION_AUTHORITY,
        "fleet_admission_lock_held": True,
        "maintenance": False,
    }


def test_native_dispatch_ignores_inherited_kanban_path_overrides(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    inherited = tmp_path / "wrong.db"
    inherited_home = tmp_path / "wrong-home"
    inherited_workspaces = tmp_path / "wrong-workspaces"
    inherited_attachments = tmp_path / "wrong-attachments"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(inherited))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(inherited_home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(inherited_workspaces))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(inherited_attachments))
    captured = {}

    class Connection:
        def close(self):
            pass

    def connect(*, board):
        captured["board"] = board
        captured["override_during_connect"] = os.environ.get("HERMES_KANBAN_DB")
        captured["home_override_during_connect"] = os.environ.get("HERMES_KANBAN_HOME")
        captured["workspaces_during_connect"] = os.environ.get(
            "HERMES_KANBAN_WORKSPACES_ROOT"
        )
        captured["attachments_during_connect"] = os.environ.get(
            "HERMES_KANBAN_ATTACHMENTS_ROOT"
        )
        return Connection()

    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(kb, "dispatch_once", lambda conn, **kwargs: kb.DispatchResult())
    monkeypatch.setattr(mod, "board_dispatch_cap", lambda slug, slots: slots)
    monkeypatch.setattr(mod, "admission_authority_errors", lambda root=mod.BOARD_ROOT: [])

    mod.native_dispatch("surveyor", 1, True)

    assert captured == {
        "board": "surveyor",
        "override_during_connect": None,
        "home_override_during_connect": None,
        "workspaces_during_connect": None,
        "attachments_during_connect": None,
    }
    assert os.environ["HERMES_KANBAN_DB"] == str(inherited)
    assert os.environ["HERMES_KANBAN_HOME"] == str(inherited_home)
    assert os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] == str(inherited_workspaces)
    assert os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"] == str(inherited_attachments)


def test_fleet_lock_path_ignores_inherited_kanban_roots(monkeypatch, tmp_path):
    canonical_home = tmp_path / "canonical"
    monkeypatch.setattr(mod, "HOME", canonical_home)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-profile"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "wrong-kanban"))

    assert mod.fleet_admission_lock_path() == (
        canonical_home / "kanban" / ".fleet-admission"
    )


def test_busy_shared_admission_lock_is_retryable(monkeypatch):
    from hermes_cli import kanban_db as kb

    class BusyLock:
        def __enter__(self):
            return False

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(kb, "_dispatch_tick_lock", lambda *args, **kwargs: BusyLock())
    monkeypatch.setattr(sys, "argv", ["polaris_execution_governor.py", "--dry-run"])
    assert mod.main() == 75


def test_final_unreadable_snapshot_overrides_earlier_green_capacity(
    tmp_path: Path, monkeypatch,
):
    reads = iter([([], [], []), ([], [], ["surveyor"])])
    monkeypatch.setattr(mod, "read_fleet", lambda root: next(reads))
    usage = mod.Usage(True, 95, 5, None, "pro", mod.iso())
    result = mod.run_once(
        root=tmp_path,
        state_path=tmp_path / "state.json",
        usage=usage,
        dry_run=True,
        dispatch=lambda *args: {"spawned": []},
    )
    assert result["state"] == "red"
    assert result["capacity"] == 0
    assert result["availableSlots"] == 0
    assert result["integrity"]["readable"] is False


def test_cached_usage_is_bounded_by_policy_age():
    usage = mod.Usage(True, 88, 12, None, "pro", "1970-01-01T00:16:30+00:00")
    previous = {"usage": mod.asdict(usage)}
    assert mod.cached_usage(previous, 1000, 300).remaining_percent == 88
    assert mod.cached_usage(previous, 2000, 300) is None


def test_positive_int_accepts_canary_limit_and_rejects_zero():
    assert mod.positive_int("1") == 1
    with pytest.raises(Exception, match="at least 1"):
        mod.positive_int("0")
