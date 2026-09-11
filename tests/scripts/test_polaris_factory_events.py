from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "polaris_factory_events.py"
SPEC = importlib.util.spec_from_file_location("polaris_factory_events", SCRIPT)
assert SPEC and SPEC.loader
factory_events = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(factory_events)


def _db(root: Path, board: str, events: list[tuple[str, str]]) -> Path:
    path = root / board / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT)")
    conn.executemany("INSERT INTO task_events(task_id, kind) VALUES (?, ?)", events)
    conn.commit()
    conn.close()
    return path


def _all_boards(root: Path) -> None:
    for board in factory_events.BOARDS:
        _db(root, board, [])


def test_startup_reconciliation_wakes_governor(tmp_path: Path) -> None:
    root = tmp_path / "boards"
    state = tmp_path / "state.json"
    _all_boards(root)
    calls: list[list[str]] = []

    result = factory_events.scan_once(
        root=root,
        state_path=state,
        force_reconcile=True,
        wake=lambda reasons: calls.append(reasons) or True,
    )

    assert result["mode"] == "startup"
    assert result["woke"] is True
    assert calls == [["startup-reconciliation"]]
    assert json.loads(state.read_text())["cursors"] == {board: 0 for board in factory_events.BOARDS}


def test_periodic_reconciliation_wakes_without_new_event(tmp_path: Path) -> None:
    root = tmp_path / "boards"
    state = tmp_path / "state.json"
    _all_boards(root)
    state.write_text(json.dumps({"cursors": {board: 0 for board in factory_events.BOARDS}}))
    calls: list[list[str]] = []

    result = factory_events.scan_once(
        root=root,
        state_path=state,
        force_reconcile=True,
        wake=lambda reasons: calls.append(reasons) or True,
    )

    assert result["trigger"] == "periodic"
    assert result["woke"] is True
    assert calls == [["periodic-reconciliation"]]


def test_unblocked_event_wakes_governor(tmp_path: Path) -> None:
    root = tmp_path / "boards"
    state = tmp_path / "state.json"
    _all_boards(root)
    conn = sqlite3.connect(root / "surveyor" / "kanban.db")
    conn.execute("INSERT INTO task_events(task_id, kind) VALUES ('t_ready', 'unblocked')")
    conn.commit()
    conn.close()
    state.write_text(json.dumps({"cursors": {board: 0 for board in factory_events.BOARDS}}))
    calls: list[list[str]] = []

    result = factory_events.scan_once(
        root=root,
        state_path=state,
        wake=lambda reasons: calls.append(reasons) or True,
    )

    assert result["trigger"] == "events"
    assert result["relevant_events"] == 1
    assert calls == [["surveyor:t_ready:unblocked"]]


def test_failed_wake_does_not_advance_event_cursor(tmp_path: Path) -> None:
    root = tmp_path / "boards"
    state = tmp_path / "state.json"
    _all_boards(root)
    conn = sqlite3.connect(root / "acqlens" / "kanban.db")
    conn.execute("INSERT INTO task_events(task_id, kind) VALUES ('t_new', 'created')")
    conn.commit()
    conn.close()
    initial = {board: 0 for board in factory_events.BOARDS}
    state.write_text(json.dumps({"cursors": initial}))

    result = factory_events.scan_once(
        root=root,
        state_path=state,
        wake=lambda reasons: False,
    )

    assert result["trigger"] == "failed"
    assert result["woke"] is False
    assert json.loads(state.read_text())["cursors"] == initial
