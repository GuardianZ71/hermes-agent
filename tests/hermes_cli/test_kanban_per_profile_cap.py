"""Regression tests for #21582 — per-profile concurrency cap in dispatcher.

When ``kanban.max_in_progress_per_profile`` is set, no single profile
gets more than N workers running at once even if the global
``max_in_progress`` cap would allow it. Prevents one profile's local
model / API quota / browser pool from being overwhelmed by a fan-out.
"""
from __future__ import annotations

import os
import json
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home_with_profiles(monkeypatch):
    """Spin up a fresh HERMES_HOME with kanban DB + alpha/beta profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_per_profile_cap_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db


def _fake_spawn(*args, **kwargs):
    return 12345




def test_cap_2_balances_two_profiles(isolated_kanban_home_with_profiles):
    """With cap=2: 2 alpha + 2 beta dispatched; remaining 3 alpha + 1 beta
    deferred to skipped_per_profile_capped."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(5):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
        for i in range(3):
            kb.create_task(conn, title=f"b{i}", assignee="beta")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=2,
        )
    spawn_assignees = [s[1] for s in res.spawned]
    capped_assignees = [c[1] for c in res.skipped_per_profile_capped]
    assert spawn_assignees.count("alpha") == 2
    assert spawn_assignees.count("beta") == 2
    assert capped_assignees.count("alpha") == 3
    assert capped_assignees.count("beta") == 1




def test_capped_tasks_dispatched_on_subsequent_tick(isolated_kanban_home_with_profiles):
    """A task deferred this tick because its profile was at cap should be
    eligible for dispatch on the next tick (after running tasks complete).
    This verifies the cap is per-tick state, not a permanent block."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ids = [kb.create_task(conn, title=f"a{i}", assignee="alpha") for i in range(3)]

    # First tick: cap=1, only 1 alpha dispatched
    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            max_in_progress_per_profile=1,
        )
    assert len(res1.spawned) == 1
    assert len(res1.skipped_per_profile_capped) == 2

    # Simulate the running task completing — set it back to done so the
    # 'running' count drops
    spawned_id = res1.spawned[0][0]
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?",
                (spawned_id,),
            )

    # Second tick: 1 more alpha should now dispatch
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            max_in_progress_per_profile=1,
        )
    assert len(res2.spawned) == 1
    assert len(res2.skipped_per_profile_capped) == 1
    assert res2.spawned[0][0] != spawned_id  # different task this time


def test_per_profile_cap_counts_running_tasks_on_other_boards(
    isolated_kanban_home_with_profiles,
):
    """Two boards must share one profile occupancy budget."""
    kb = isolated_kanban_home_with_profiles
    kb.create_board(slug="default", name="Primary")
    kb.create_board(slug="second", name="Second")

    with kb.connect_closing(board="second") as conn:
        for i in range(2):
            task_id = kb.create_task(conn, title=f"busy-{i}", assignee="alpha")
            assert kb.claim_task(conn, task_id) is not None

    with kb.connect_closing(board="default") as conn:
        waiting = kb.create_task(conn, title="waiting", assignee="alpha")
        other = kb.create_task(conn, title="other-profile", assignee="beta")
        result = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            max_in_progress_per_profile=2,
            board="default",
        )

    assert [item[0] for item in result.spawned] == [other]
    assert (waiting, "alpha", 2) in result.skipped_per_profile_capped


def test_per_profile_other_board_count_fails_closed(
    isolated_kanban_home_with_profiles, monkeypatch,
):
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert kb.count_running_tasks_by_profile_other_boards() is None

    with kb.connect_closing() as conn:
        kb.create_task(conn, title="must-wait", assignee="alpha")
        result = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            max_in_progress_per_profile=2,
        )
    assert result.spawned == []


def test_dispatch_exclusion_skips_only_named_candidate(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    kb.create_board(slug="default", name="Test")
    with kb.connect_closing() as conn:
        excluded = kb.create_task(conn, title="same-domain", assignee="alpha", priority=10)
        allowed = kb.create_task(conn, title="unrelated", assignee="beta", priority=1)
        result = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            excluded_task_ids=[excluded],
        )
    assert result.skipped_excluded == [excluded]
    assert [item[0] for item in result.spawned] == [allowed]


def test_dispatch_allowlist_holds_tasks_promoted_during_the_tick(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    kb.create_board(slug="default", name="Test")
    with kb.connect_closing() as conn:
        known = kb.create_task(conn, title="known", assignee="alpha")
        late = kb.create_task(conn, title="late", assignee="beta")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (late,))
        conn.commit()
        result = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            admitted_task_ids=[known],
        )
    assert result.promoted == 1
    assert [item[0] for item in result.spawned] == [known]
    assert late in result.skipped_excluded


def test_empty_dispatch_allowlist_blocks_ready_and_review_lanes(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    kb.create_board(slug="default", name="Test")
    with kb.connect_closing() as conn:
        ready = kb.create_task(conn, title="ready", assignee="alpha")
        review = kb.create_task(conn, title="review", assignee="beta")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        conn.commit()
        result = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            admitted_task_ids=[],
        )
    assert result.spawned == []
    assert set(result.skipped_excluded) == {ready, review}


def test_governed_board_admits_only_configured_authority(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    kb.create_board(slug="default", name="Governed")
    metadata = kb.read_board_metadata("default")
    metadata.pop("db_path", None)
    metadata["admission_authority"] = "governor-v1"
    kb.board_metadata_path("default").write_text(json.dumps(metadata))

    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(conn, title="one-owner", assignee="alpha")
        bypass = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            board="default",
            max_in_progress=5,
        )
        admitted = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            board="default",
            max_in_progress=5,
            admitted_task_ids=[task_id],
            admission_authority="governor-v1",
        )

    assert bypass.spawned == []
    assert bypass.skipped_excluded == [task_id]
    assert [row[0] for row in admitted.spawned] == [task_id]


def test_canonical_polaris_board_without_authority_fails_closed(
    isolated_kanban_home_with_profiles, monkeypatch,
):
    kb = isolated_kanban_home_with_profiles
    kb.create_board(slug="polaris-ops", name="Polaris")
    with kb.connect_closing(board="polaris-ops") as conn:
        task_id = kb.create_task(conn, title="must-not-bypass", assignee="alpha")
        direct = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            board="polaris-ops",
            max_in_progress=5,
        )
        wrong_authority = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            board="polaris-ops",
            max_in_progress=5,
            admitted_task_ids=[task_id],
            admission_authority="not-the-governor",
        )
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "polaris-ops")
        implicit_cli = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            max_in_progress=5,
        )

    assert direct.spawned == []
    assert direct.skipped_excluded == [task_id]
    assert wrong_authority.spawned == []
    assert wrong_authority.skipped_excluded == [task_id]
    assert implicit_cli.spawned == []
    assert implicit_cli.skipped_excluded == [task_id]


def test_canonical_db_path_override_cannot_disguise_board_as_default(
    isolated_kanban_home_with_profiles, monkeypatch,
):
    kb = isolated_kanban_home_with_profiles
    kb.create_board(slug="surveyor", name="Surveyor")
    db_path = kb.kanban_db_path(board="surveyor")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    with kb.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="must-not-disguise", assignee="alpha")
        explicit_default = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            board="default",
            max_in_progress=5,
            admitted_task_ids=[task_id],
        )
        implicit_default = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=True,
            max_in_progress=5,
            admitted_task_ids=[task_id],
        )

    assert explicit_default.spawned == []
    assert explicit_default.skipped_excluded == [task_id]
    assert implicit_default.spawned == []
    assert implicit_default.skipped_excluded == [task_id]


