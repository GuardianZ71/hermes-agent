"""Tests for the kanban dispatcher single-writer lock (issue #35240).

A ``hermes gateway run --replace`` / ``gateway restart`` from a shell on a
systemd/launchd host can leave an orphan dispatcher that escapes the
service cgroup, survives ``systemctl restart``, and becomes a second
long-lived writer on the same ``kanban.db`` — the documented root cause of
multi-writer SQLite WAL corruption. ``dispatch_once`` now wraps each tick in
a non-blocking, board-scoped dispatch lock so two dispatchers can never run
a reclaim/spawn/write tick concurrently. The losing dispatcher returns an
empty ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes.
"""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c




def test_held_lock_skips_the_tick_without_writes(conn):
    """While another holder owns the board lock, dispatch_once must skip and
    must NOT invoke spawn_fn (no DB writes happen on a skipped tick)."""
    kb.create_task(conn, title="t", assignee="w")
    db_path = kb.kanban_db_path(board="default")

    spawn_calls: list = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999

    # Hold the lock, then attempt a contended tick.
    with kb._dispatch_tick_lock(db_path) as held:
        assert held is True  # we genuinely acquired it
        result = kb.dispatch_once(conn, spawn_fn=spy_spawn)

    assert result.skipped_locked is True
    assert result.spawned == []
    assert spawn_calls == [], "spawn_fn must not run while the tick is locked out"




def test_lock_is_board_scoped(conn):
    """Holding board A's dispatch lock must not block a tick on board B —
    distinct boards have distinct DB files and tick independently."""
    db_default = kb.kanban_db_path(board="default")
    db_other = db_default.with_name("other-board-kanban.db")

    # Two different lock files → both acquirable simultaneously.
    with kb._dispatch_tick_lock(db_default) as held_a:
        assert held_a is True
        with kb._dispatch_tick_lock(db_other) as held_b:
            assert held_b is True, "a lock on a different board must be independent"


def test_fleet_cap_dispatch_uses_one_cross_board_admission_lock(conn):
    """A concurrent capped tick cannot race another board's occupancy read."""
    fleet_lock_key = kb.kanban_home() / "kanban" / ".fleet-admission"
    with kb._dispatch_tick_lock(fleet_lock_key, fail_closed=True) as held:
        assert held is True
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=5,
            max_in_progress_per_profile=2,
        )

    assert result.skipped_locked is True
    assert result.spawned == []


def test_two_real_cross_board_ticks_cannot_exceed_fleet_or_profile_cap(
    kanban_home, all_assignees_spawnable,
):
    for slug in ("a", "b"):
        kb.create_board(slug=slug, name=slug.upper())
        with kb.connect(board=slug) as board_conn:
            kb.create_task(board_conn, title=f"task-{slug}", assignee="alice")

    start = threading.Barrier(2)
    spawned: list[tuple[str, str]] = []
    results = []
    guard = threading.Lock()

    def run(slug: str) -> None:
        def fake_spawn(task, workspace_path, board=None):
            with guard:
                spawned.append((slug, task.id))
                return 1000 + len(spawned)

        with kb.connect(board=slug) as board_conn:
            start.wait()
            result = kb.dispatch_once(
                board_conn, board=slug, spawn_fn=fake_spawn,
                max_in_progress=1, max_in_progress_per_profile=1,
            )
        with guard:
            results.append(result)

    threads = [threading.Thread(target=run, args=(slug,)) for slug in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(spawned) == 1
    assert sum(len(result.spawned) for result in results) == 1
    running = 0
    for slug in ("a", "b"):
        with kb.connect(board=slug) as board_conn:
            running += kb.count_running_tasks(board_conn) or 0
    assert running == 1


