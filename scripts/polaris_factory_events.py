#!/usr/bin/env python3
"""Wake the deterministic Polaris governor from events and periodic reconciliation."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable

from watchfiles import watch

BOARDS = ("polaris-ops", "acqlens", "surveyor", "apex")
RELEVANT_KINDS = frozenset({
    "created", "promoted", "unblocked", "completed", "review_requested",
    "changes_requested", "reclaimed", "stale", "timed_out", "crashed",
})
HERMES_HOME = Path("/Users/ops/.hermes")
BOARD_ROOT = HERMES_HOME / "kanban" / "boards"
STATE_PATH = HERMES_HOME / "profiles" / "forge" / "state" / "polaris_factory_events.json"
GOVERNOR = Path(__file__).with_name("polaris_execution_governor.py")
PYTHON = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python"
DEFAULT_RECONCILE_SECONDS = 60


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    os.replace(temp, path)


def _events_after(db_path: Path, cursor: int) -> tuple[list[tuple[int, str, str]], int]:
    if not db_path.exists():
        return [], cursor
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT id, task_id, kind FROM task_events WHERE id > ? ORDER BY id",
            (cursor,),
        ).fetchall()
    finally:
        conn.close()
    events = [(int(row[0]), str(row[1]), str(row[2])) for row in rows]
    return events, (events[-1][0] if events else cursor)


def _max_event_id(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def wake_governor(reasons: list[str]) -> bool:
    proc = subprocess.run(
        [str(PYTHON), str(GOVERNOR)],
        text=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "HOME": "/Users/ops", "POLARIS_HERMES_HOME": str(HERMES_HOME)},
    )
    if proc.returncode != 0:
        print(json.dumps({
            "event": "governor_failed",
            "reasons": reasons,
            "returncode": proc.returncode,
            "stderr": proc.stderr[-2000:],
        }, sort_keys=True), flush=True)
        return False
    return True


def scan_once(
    *,
    root: Path = BOARD_ROOT,
    state_path: Path = STATE_PATH,
    wake: Callable[[list[str]], bool] = wake_governor,
    force_reconcile: bool = False,
) -> dict:
    previous = _load(state_path)
    cursors_raw = previous.get("cursors")
    if not isinstance(cursors_raw, dict):
        cursors = {slug: _max_event_id(root / slug / "kanban.db") for slug in BOARDS}
        _atomic_write(state_path, {"version": 1, "cursors": cursors, "updatedAt": time.time()})
        woke = force_reconcile and wake(["startup-reconciliation"])
        return {
            "mode": "startup", "woke": woke, "relevant_events": 0,
            "trigger": "startup" if force_reconcile else "cursor-only", "cursors": cursors,
        }

    cursors = {slug: int(cursors_raw.get(slug, 0) or 0) for slug in BOARDS}
    next_cursors = dict(cursors)
    relevant: list[str] = []
    for slug in BOARDS:
        events, next_cursors[slug] = _events_after(root / slug / "kanban.db", cursors[slug])
        relevant.extend(f"{slug}:{task_id}:{kind}" for _, task_id, kind in events if kind in RELEVANT_KINDS)

    reasons = relevant or (["periodic-reconciliation"] if force_reconcile else [])
    woke = False
    if reasons:
        woke = wake(reasons)
        if not woke:
            return {
                "mode": "events", "woke": False, "relevant_events": len(relevant),
                "trigger": "failed", "cursors": cursors,
            }

    if next_cursors != cursors:
        _atomic_write(state_path, {"version": 1, "cursors": next_cursors, "updatedAt": time.time()})
    return {
        "mode": "events", "woke": woke, "relevant_events": len(relevant),
        "trigger": "events" if relevant else ("periodic" if force_reconcile else "none"),
        "cursors": next_cursors,
    }


def _emit(result: dict) -> None:
    print(json.dumps({"event": "factory_reconciliation", **result}, sort_keys=True), flush=True)


def run_watch() -> int:
    interval = max(5, int(os.environ.get("POLARIS_RECONCILE_SECONDS", DEFAULT_RECONCILE_SECONDS)))
    result = scan_once(force_reconcile=True)
    _emit(result)
    last_reconcile = time.monotonic() if result.get("woke") else 0.0
    paths = [BOARD_ROOT / slug for slug in BOARDS]
    for changes in watch(
        *paths,
        debounce=100,
        step=50,
        rust_timeout=1000,
        yield_on_timeout=True,
    ):
        now = time.monotonic()
        due = now - last_reconcile >= interval
        if not changes and not due:
            continue
        result = scan_once(force_reconcile=due)
        if result.get("woke") or result.get("relevant_events"):
            _emit(result)
        if due and result.get("woke"):
            last_reconcile = now
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reconcile", action="store_true", help="wake the governor even without a new event")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(scan_once(force_reconcile=args.reconcile), sort_keys=True))
        return 0
    return run_watch()


if __name__ == "__main__":
    raise SystemExit(main())
