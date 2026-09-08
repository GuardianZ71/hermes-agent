from __future__ import annotations

import importlib.util
import fcntl
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
         assignee: str = "forge", status: str = "ready", priority: int = 0) -> mod.Task:
    return mod.Task(board, task_id, title, body, assignee, status, priority, 1)


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
    assert result["crossOutcomeDependencies"][0]["classification"] == "unclassified_cross_outcome"
    assert result["crossOutcomeDependencies"][0]["bothNonterminal"] is True
    assert any(item["id"].startswith("cross-outcome:") for item in result["incidents"])


def test_explicit_artifact_dependency_is_classified_not_incident():
    tasks = [task("parent", body="[linear:POL-144] [dependency:artifact]"),
             task("child", body="[linear:POL-114]")]
    result = mod.analyze(tasks, [("surveyor", "parent", "child")], [], mod.Policy())
    assert result["crossOutcomeDependencies"][0]["classification"] == "explicit_artifact_or_collision"
    assert not any(item["id"].startswith("cross-outcome:") for item in result["incidents"])


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


def _make_board(root: Path, slug: str) -> sqlite3.Connection:
    folder = root / slug
    folder.mkdir(parents=True)
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
    assert result["safeRepair"] == {"mode": "dispatch_exclusion", "mutatedCards": False}


def test_cron_and_event_wakeups_share_admission_lock(tmp_path: Path):
    lock_path = tmp_path / "admission.lock"
    with lock_path.open("a+") as first, lock_path.open("a+") as second:
        assert mod.acquire_admission_lock(first)
        assert not mod.acquire_admission_lock(second)
        fcntl.flock(first.fileno(), fcntl.LOCK_UN)
        assert mod.acquire_admission_lock(second, timeout_seconds=0.1)


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
    }


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
