#!/usr/bin/env python3
"""Deterministic fleet outcome-flow governor for canonical Polaris boards."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

BOARDS = ("polaris-ops", "acqlens", "surveyor", "apex")
NAMES = {"polaris-ops": "PolarisOS", "acqlens": "AcqLens", "surveyor": "Surveyor", "apex": "Apex"}
HOME = Path(os.environ.get("POLARIS_HERMES_HOME", Path.home() / ".hermes"))
BOARD_ROOT = HOME / "kanban" / "boards"
STATE_PATH = HOME / "profiles" / "forge" / "state" / "polaris_execution_governor.json"
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = _SOURCE_ROOT if (_SOURCE_ROOT / "hermes_cli").is_dir() else HOME / "hermes-agent"
PYTHON = HOME / "hermes-agent" / "venv" / "bin" / "python"
LINEAR_RE = re.compile(r"\[linear:(POL-\d+)\]", re.I)
COLLISION_RE = re.compile(r"\[collision-domain:([^\]]+)\]", re.I)
PR_URL_RE = re.compile(r"https://github\.com/([^\s/]+/[^\s/]+)/pull/(\d+)", re.I)
PR_NUMBER_RE = re.compile(r"\bPR\s*#(\d+)\b", re.I)
EXPLICIT_EDGE_RE = re.compile(r"\[(?:dependency|edge):(artifact|collision)\]", re.I)
TERMINAL = frozenset({"done", "archived"})


@dataclass(frozen=True)
class Policy:
    max_background_workers: int = 5
    max_workers_per_profile: int = 2
    amber_remaining_percent: float = 40.0
    red_remaining_percent: float = 15.0
    hard_stop_remaining_percent: float = 5.0
    usage_refresh_seconds: int = 300


@dataclass(frozen=True)
class Usage:
    available: bool
    remaining_percent: float | None
    used_percent: float | None
    reset_at: str | None
    plan: str | None
    fetched_at: str
    reason: str | None = None


@dataclass(frozen=True)
class Task:
    board: str
    task_id: str
    title: str
    body: str
    assignee: str
    status: str
    priority: int
    created_at: int
    comments: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}\n{self.comments}"

    @property
    def outcome(self) -> str | None:
        match = LINEAR_RE.search(self.text)
        return match.group(1).upper() if match else None

    @property
    def stage(self) -> str:
        title = self.title.lower()
        text = self.text.lower()
        if re.search(r"\b(live acceptance|acceptance|live verify|verify live)\b", title):
            return "live_acceptance"
        if re.search(r"\b(release|merge|deploy|promote)\b", title):
            return "release"
        if (
            "[review]" in title
            or re.search(r"\b(?:independent|exact-head|fresh) (?:exact-head )?review\b", title)
            or re.search(r"^(?:re-?review|review)\b", title)
            or re.search(r"\breview pr\s*#?\d+\b", title)
            or "independent exact-head review" in text
        ):
            return "exact_head_review"
        return "build"

    @property
    def writer(self) -> bool:
        return self.stage == "build"

    @property
    def domains(self) -> tuple[str, ...]:
        domains = {f"collision:{m.group(1).strip().lower()}" for m in COLLISION_RE.finditer(self.text)}
        domains.update(f"pr:{m.group(1).lower()}#{m.group(2)}" for m in PR_URL_RE.finditer(self.text))
        if not any(value.startswith("pr:") for value in domains):
            domains.update(f"pr:{self.board}#{m.group(1)}" for m in PR_NUMBER_RE.finditer(self.text))
        if self.outcome:
            domains.add(f"outcome:{self.outcome}")
        return tuple(sorted(domains))


def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else time.time(), tz=timezone.utc).isoformat()


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def cached_usage(previous: dict[str, Any], now: float, max_age: int) -> Usage | None:
    raw = previous.get("usage")
    if not isinstance(raw, dict) or not isinstance(raw.get("fetched_at"), str):
        return None
    try:
        fetched = datetime.fromisoformat(raw["fetched_at"].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    if now - fetched > max_age:
        return None
    return Usage(
        bool(raw.get("available")),
        float(raw["remaining_percent"])
        if isinstance(raw.get("remaining_percent"), (int, float)) else None,
        float(raw["used_percent"])
        if isinstance(raw.get("used_percent"), (int, float)) else None,
        str(raw["reset_at"]) if raw.get("reset_at") else None,
        str(raw["plan"]) if raw.get("plan") else None,
        raw["fetched_at"],
        str(raw["reason"]) if raw.get("reason") else None,
    )


def classify(remaining: float | None, policy: Policy) -> tuple[str, int, str]:
    if remaining is None:
        return "amber", 1, "Allowance API unavailable; background dispatch is restricted."
    if remaining <= policy.hard_stop_remaining_percent:
        return "hard_stop", 0, "Provider allowance is at the protected reserve."
    if remaining <= policy.red_remaining_percent:
        return "red", 0, "Provider allowance is low; capacity is reserved for release-blocking work."
    if remaining <= policy.amber_remaining_percent:
        return "amber", 1, "Provider allowance is below the background-work threshold."
    return "green", policy.max_background_workers, "Provider allowance is healthy."


def fetch_usage() -> Usage:
    fetched = iso()
    try:
        import sys
        sys.path.insert(0, str(HOME / "hermes-agent"))
        from agent.account_usage import _fetch_codex_account_usage  # type: ignore
        snapshot = _fetch_codex_account_usage()
        windows = [w for w in (snapshot.windows if snapshot else ()) if w.used_percent is not None]
        if not snapshot or not snapshot.available or not windows:
            return Usage(False, None, None, None, getattr(snapshot, "plan", None), fetched, "No allowance window returned")
        busiest = max(windows, key=lambda w: float(w.used_percent or 0))
        used = max(0.0, min(100.0, float(busiest.used_percent or 0)))
        return Usage(True, 100.0 - used, used, busiest.reset_at.isoformat() if busiest.reset_at else None, snapshot.plan, fetched)
    except Exception as exc:
        return Usage(False, None, None, None, None, fetched, f"{type(exc).__name__}: {str(exc)[:120]}")


def read_fleet(root: Path) -> tuple[list[Task], list[tuple[str, str, str]], list[str]]:
    tasks: list[Task] = []
    edges: list[tuple[str, str, str]] = []
    unreadable: list[str] = []
    for slug in BOARDS:
        path = root / slug / "kanban.db"
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                "SELECT t.id,t.title,t.body,t.assignee,t.status,t.priority,t.created_at," 
                "COALESCE((SELECT group_concat(c.body,'\\n') FROM task_comments c "
                "WHERE c.task_id=t.id),'') AS comments FROM tasks t"
            ).fetchall()
            for row in rows:
                tasks.append(Task(slug, str(row["id"]), str(row["title"] or ""), str(row["body"] or ""),
                                  str(row["assignee"] or ""), str(row["status"]), int(row["priority"] or 0),
                                  int(row["created_at"] or 0), str(row["comments"] or "")))
            for row in conn.execute("SELECT parent_id,child_id FROM task_links"):
                edges.append((slug, str(row["parent_id"]), str(row["child_id"])))
            conn.close()
        except (OSError, sqlite3.Error):
            unreadable.append(slug)
    return tasks, edges, unreadable


def _task_ref(task: Task) -> dict[str, Any]:
    return {"board": task.board, "taskId": task.task_id, "title": task.title[:180], "status": task.status,
            "profile": task.assignee or None, "outcome": task.outcome, "stage": task.stage}


def analyze(tasks: Iterable[Task], edges: Iterable[tuple[str, str, str]], unreadable: Iterable[str], policy: Policy) -> dict[str, Any]:
    task_list = list(tasks)
    unreadable_list = sorted(set(unreadable))
    active = [task for task in task_list if task.status == "running"]
    by_profile = Counter(task.assignee for task in active if task.assignee)
    incidents: list[dict[str, Any]] = []
    if unreadable_list:
        incidents.append({"id": "occupancy-unreadable", "severity": "error", "boards": unreadable_list,
                          "message": "Canonical board occupancy is unreadable; admission is closed."})
    if len(active) > policy.max_background_workers:
        incidents.append({"id": "fleet-occupancy-over-cap", "severity": "error", "current": len(active),
                          "limit": policy.max_background_workers, "message": "Fleet occupancy exceeds its limit; drain without killing healthy workers."})
    for profile, current in sorted(by_profile.items()):
        if current > policy.max_workers_per_profile:
            incidents.append({"id": f"profile-occupancy-over-cap:{profile}", "severity": "error", "profile": profile,
                              "current": current, "limit": policy.max_workers_per_profile,
                              "message": "Profile occupancy exceeds its fleet-wide limit; drain without killing healthy workers."})

    domain_writers: dict[str, list[Task]] = defaultdict(list)
    for task in active:
        if task.writer:
            for domain in task.domains:
                domain_writers[domain].append(task)
    writer_collisions = []
    for domain, owners in sorted(domain_writers.items()):
        if len(owners) > 1:
            item = {"domain": domain, "owners": [_task_ref(task) for task in owners]}
            writer_collisions.append(item)
            incidents.append({"id": f"duplicate-writer:{domain}", "severity": "error", **item,
                              "message": "Multiple active build writers share one collision domain."})

    by_id = {(task.board, task.task_id): task for task in task_list}
    cross_edges = []
    for board, parent_id, child_id in edges:
        parent = by_id.get((board, parent_id))
        child = by_id.get((board, child_id))
        if not parent or not child or not parent.outcome or not child.outcome or parent.outcome == child.outcome:
            continue
        explicit = bool(EXPLICIT_EDGE_RE.search(parent.text) or EXPLICIT_EDGE_RE.search(child.text))
        both_nonterminal = parent.status not in TERMINAL and child.status not in TERMINAL
        edge = {"board": board, "parent": _task_ref(parent), "child": _task_ref(child),
                "classification": "explicit_artifact_or_collision" if explicit else "unclassified_cross_outcome",
                "bothNonterminal": both_nonterminal}
        cross_edges.append(edge)
        if not explicit:
            incidents.append({"id": f"cross-outcome:{board}:{parent_id}:{child_id}",
                              "severity": "error" if both_nonterminal else "warning", **edge,
                              "message": "Cross-outcome dependency must be classified as artifact/collision or removed manually."})

    stages = ("build", "exact_head_review", "release", "live_acceptance")
    outcomes: dict[str, list[Task]] = defaultdict(list)
    for task in task_list:
        if task.outcome:
            outcomes[task.outcome].append(task)
    chains = []
    for outcome, members in sorted(outcomes.items(), key=lambda item: int(item[0].split("-")[1])):
        stage_payload = []
        for stage in stages:
            stage_tasks = [task for task in members if task.stage == stage]
            statuses = {task.status for task in stage_tasks}
            if "running" in statuses:
                state = "running"
            elif statuses and statuses <= TERMINAL:
                state = "complete"
            elif statuses & {"blocked", "triage"}:
                state = "blocked"
            elif stage_tasks:
                state = "pending"
            else:
                state = "missing"
            stage_payload.append({"stage": stage, "state": state, "tasks": [_task_ref(task) for task in stage_tasks if task.status != "archived"]})
        chains.append({"outcome": outcome, "stages": stage_payload})

    held = []
    occupied_domains = set(domain_writers)
    ready_writers = sorted(
        (task for task in task_list if task.status == "ready" and task.writer),
        key=lambda task: (-task.priority, task.created_at, task.task_id),
    )
    for task in ready_writers:
        conflicts = sorted(set(task.domains) & occupied_domains)
        if conflicts:
            held.append({"task": _task_ref(task), "domains": conflicts})
        else:
            # Reserve this candidate's domains for this admission pass so two
            # ready siblings cannot become simultaneous writers in one tick.
            occupied_domains.update(task.domains)
    return {"readable": not unreadable_list, "incidents": incidents, "writerCollisions": writer_collisions,
            "crossOutcomeDependencies": cross_edges, "heldCandidates": held,
            "occupancy": {"fleet": len(active), "fleetLimit": policy.max_background_workers,
                          "byProfile": dict(sorted(by_profile.items())), "perProfileLimit": policy.max_workers_per_profile},
            "outcomeChains": chains}


def spawned_count(detail: Any) -> int:
    if not isinstance(detail, dict):
        return 0
    spawned = detail.get("spawned")
    return len(spawned) if isinstance(spawned, list) else max(0, int(spawned or 0))


def board_dispatch_cap(slug: str, additional_slots: int, root: Path = BOARD_ROOT) -> int:
    """Convert additional fleet headroom to the dispatcher's per-board cap."""
    conn = sqlite3.connect(root / slug / "kanban.db")
    try:
        running = int(conn.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0])
    finally:
        conn.close()
    return max(1, running + max(0, int(additional_slots)))


def native_dispatch(
    slug: str,
    slots: int,
    dry_run: bool,
    excluded: Iterable[str] = (),
    admitted: Iterable[str] = (),
    policy: Policy = Policy(),
) -> dict[str, Any]:
    # Invoke the module from the canonical source checkout rather than the
    # generated console-script wrapper.  A copied runtime governor lives under
    # the Forge profile, while the dispatcher flags ship in AGENT_ROOT; pinning
    # cwd makes both the repository copy and installed copy load that exact
    # source tree.
    command = [str(PYTHON), "-m", "hermes_cli.main", "kanban", "--board", slug, "dispatch"]
    if dry_run:
        command.append("--dry-run")
    command += [
        "--max", str(board_dispatch_cap(slug, slots)),
        "--max-in-progress", str(policy.max_background_workers),
        "--max-in-progress-per-profile", str(policy.max_workers_per_profile),
        "--failure-limit", "2",
        "--json",
    ]
    for task_id in sorted(set(excluded)):
        command += ["--exclude-task", task_id]
    command.append("--admit-only")
    for task_id in sorted(set(admitted)):
        command += ["--admit-task", task_id]
    env = dict(os.environ)
    env.pop("HERMES_PROFILE", None)
    env.update({"HOME": str(Path.home()), "HERMES_HOME": str(HOME), "HERMES_KANBAN_BOARD": slug})
    proc = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
        cwd=AGENT_ROOT,
    )
    try:
        detail = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        detail = None
    return {"board": slug, "requested": slots, "spawned": spawned_count(detail),
            "status": "ok" if proc.returncode == 0 else "error", "detail": detail,
            **({"error": (proc.stderr or proc.stdout).strip()[:240]} if proc.returncode else {})}


def run_once(*, root: Path = BOARD_ROOT, state_path: Path = STATE_PATH, policy: Policy = Policy(),
             usage: Usage | None = None, dry_run: bool = False,
             dispatch: Callable[[str, int, bool, Iterable[str], Iterable[str], Policy], dict[str, Any]] = native_dispatch) -> dict[str, Any]:
    now = time.time()
    previous = load(state_path)
    current_usage = usage or cached_usage(previous, now, policy.usage_refresh_seconds) or fetch_usage()
    state, capacity, reason = classify(current_usage.remaining_percent, policy)
    tasks, edges, unreadable = read_fleet(root)
    integrity = analyze(tasks, edges, unreadable, policy)
    occupancy = integrity["occupancy"]
    actions: list[dict[str, Any]] = []
    admission_closed = bool(unreadable or occupancy["fleet"] >= capacity)
    if unreadable:
        state, capacity, reason = "red", 0, "Canonical occupancy is unreadable; admission failed closed."
    slots = max(0, capacity - occupancy["fleet"])
    cursor = str(previous.get("nextBoard") or BOARDS[0])
    start = BOARDS.index(cursor) if cursor in BOARDS else 0
    order = BOARDS[start:] + BOARDS[:start]
    next_board = cursor
    held_by_board = {
        slug: {
            item["task"]["taskId"]
            for item in integrity["heldCandidates"]
            if item["task"]["board"] == slug
        }
        for slug in BOARDS
    }
    unreadable_boards = set(unreadable)
    for slug in order:
        if slug in unreadable_boards:
            continue
        excluded = held_by_board[slug]
        admitted = []
        if not admission_closed and slots > 0:
            admitted = [
                task.task_id
                for task in tasks
                if (
                    task.board == slug
                    and task.status in {"ready", "review"}
                    and task.task_id not in excluded
                )
            ]
        action = dispatch(slug, slots, dry_run, excluded, admitted, policy)
        actions.append(action)
        count = spawned_count(action)
        slots = max(0, slots - count)
        if count:
            next_board = BOARDS[(BOARDS.index(slug) + 1) % len(BOARDS)]
    # Always re-read after dispatch so one canonical payload is authoritative.
    tasks, edges, unreadable = read_fleet(root)
    integrity = analyze(tasks, edges, unreadable, policy)
    occupancy = integrity["occupancy"]
    payload = {"version": 2, "generatedAt": iso(), "state": state, "reason": reason,
               "nextBoard": next_board,
               "policy": asdict(policy), "usage": asdict(current_usage), "capacity": capacity,
               "activeWorkers": occupancy["fleet"], "availableSlots": max(0, capacity - occupancy["fleet"]),
               "queuedReady": sum(1 for task in tasks if task.status == "ready"),
               "activeExecutions": [_task_ref(task) for task in tasks if task.status == "running"],
               "integrity": integrity, "outcomeChains": integrity["outcomeChains"],
               "safeRepair": {"mode": "dispatch_exclusion", "mutatedCards": False},
               "actions": actions, "dryRun": dry_run}
    atomic_write(state_path, payload)
    return payload


def acquire_admission_lock(lock: Any, timeout_seconds: float = 0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=0.0)
    parser.add_argument(
        "--max-background-workers",
        type=positive_int,
        default=positive_int(os.environ.get("POLARIS_MAX_BACKGROUND_WORKERS", "5")),
        help="Fleet-wide admission limit (default 5; set to 1 for canary activation).",
    )
    parser.add_argument(
        "--max-workers-per-profile",
        type=positive_int,
        default=positive_int(os.environ.get("POLARIS_MAX_WORKERS_PER_PROFILE", "2")),
        help="Fleet-wide per-profile admission limit (default 2).",
    )
    args = parser.parse_args()
    policy = Policy(
        max_background_workers=args.max_background_workers,
        max_workers_per_profile=args.max_workers_per_profile,
    )
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a+") as lock:
        if not acquire_admission_lock(lock, args.lock_timeout):
            return 0
        print(json.dumps(run_once(dry_run=args.dry_run, policy=policy), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
