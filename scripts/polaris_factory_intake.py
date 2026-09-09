#!/usr/bin/env python3
"""Authenticated Linear webhook intake for the Polaris software factory."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("POLARIS_HERMES_HOME", Path.home() / ".hermes"))
FACTORY_HOME = HOME / "factory"
CONFIG_PATH = FACTORY_HOME / "config.json"
AUTHORITY_HOME = Path(os.environ.get("POLARIS_FACTORY_AUTHORITY_HOME", "/var/db/polaris-factory"))
REGISTRY_PATH = AUTHORITY_HOME / "authority-public-keys.json"
WEBHOOK_SECRET_PATH = FACTORY_HOME / "linear-webhook-secret"
STATE_PATH = FACTORY_HOME / "intake-state.json"
QUEUE_PATH = FACTORY_HOME / "intake-queue.db"
ENGINE_PATH = HOME / "skills" / "operations" / "polaris-software-factory" / "scripts" / "polaris_factory.py"
HERMES_ROOT = HOME / "hermes-agent"
LINEAR_ENDPOINT = "https://api.linear.app/graphql"
_LOCK = threading.Lock()
_WAKE = threading.Event()
SIGNER_COMMANDS = {
    "linear": ["/usr/bin/sudo", "-n", "-u", "polarislinear", "/usr/local/libexec/polaris-factory-sign-linear"],
    "project_registry": [
        "/usr/bin/sudo", "-n", "-u", "polarisproject", "/usr/local/libexec/polaris-factory-sign-project",
    ],
}


def _atomic(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)


def _load_engine():
    spec = importlib.util.spec_from_file_location("polaris_factory_engine", ENGINE_PATH)
    if not spec or not spec.loader:
        raise RuntimeError("factory engine is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Signature verification must use the same root-controlled registry as
    # signer command discovery, never a replaceable per-user copy.
    setattr(module, "AUTHORITY_KEYS_PATH", REGISTRY_PATH)
    return module


def _linear(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ.get("LINEAR_API_KEY", "")
    if not token:
        raise RuntimeError("LINEAR_API_KEY is unavailable")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(
        LINEAR_ENDPOINT, data=body,
        headers={"Authorization": token, "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if result.get("errors"):
        raise RuntimeError(str(result["errors"])[:500])
    return result["data"]


def fetch_issue(identifier: str, engine: Any) -> dict[str, Any]:
    data = _linear(
        """query($id:String!){ issue(id:$id){ id identifier title description createdAt team{key} project{id name} comments(first:100){nodes{id body createdAt user{id name}}} } }""",
        {"id": identifier},
    )["issue"]
    if not data or not data.get("project"):
        raise ValueError("issue must belong to a configured Linear project")
    contract = engine.validate_spec(str(data.get("description") or ""))
    return {
        "id": data["id"], "identifier": data["identifier"], "title": data["title"],
        "description": data["description"], "spec_finalized_at": data["createdAt"],
        "team_key": data["team"]["key"], "project_id": data["project"]["id"],
        "repository": contract.repository_identity,
        "comments": [
            {"id": row["id"], "body": row["body"], "created_at": row["createdAt"],
             "actor": {"id": (row.get("user") or {}).get("id"), "is_bot": False}}
            for row in data["comments"]["nodes"]
        ],
    }


def _repo_state(repo: str, issue: str) -> dict[str, Any]:
    def gh(path: str) -> Any:
        proc = subprocess.run(
            ["gh", "api", path, "--paginate"], text=True, encoding="utf-8",
            errors="replace", capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:300])
        return json.loads(proc.stdout)
    needle = issue.casefold()
    branches = [
        {"name": row["name"], "issue_identifier": issue}
        for row in gh(f"repos/{repo}/branches?per_page=100") if needle in row["name"].casefold()
    ]
    pulls = [
        {"number": row["number"], "branch": row["head"]["ref"], "state": row["state"], "issue_identifier": issue}
        for row in gh(f"repos/{repo}/pulls?state=open&per_page=100")
        if needle in (row["title"] + " " + row["head"]["ref"]).casefold()
    ]
    return {"branches": branches, "pull_requests": pulls}


def _task_rows(board: str, issue: str) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    try:
        rows = []
        prefix = f"polaris-software-factory:{issue.casefold()}:"
        for task in kb.list_tasks(conn, include_archived=True):
            text = f"{task.title}\n{task.body}"
            folded = text.casefold()
            if not (str(task.idempotency_key or "").casefold().startswith(prefix) or f"[{issue}]".casefold() in folded or f"[linear:{issue}]".casefold() in folded):
                continue
            marker = __import__("re").search(r"POLARIS_FACTORY_CHECKPOINT_V1 sha256:[0-9a-f]{64}", task.body or "")
            rows.append({"id": task.id, "idempotency_key": task.idempotency_key, "status": task.status,
                         "branch_name": task.branch_name, "body": task.body,
                         "checkpoint_fingerprint": marker.group(0) if marker else None,
                         "metadata": {"linear_identifier": issue}})
        return rows
    finally:
        conn.close()


def build_snapshot(issue: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    mapping = next((m for m in config["project_mappings"] if m["linear_team"] == issue["team_key"] and m["linear_project_id"] == issue["project_id"] and m["repo"].casefold() == issue["repository"].casefold()), None)
    if not mapping:
        raise ValueError("issue has no exact production mapping")
    snapshot = {
        "issue": issue,
        "project_registry": [{"id": mapping["project_id"], "repo": mapping["repo"], "archived": False}],
        "owner_profiles": [{"name": mapping["owner_profile"], "enabled": (HOME / "profiles" / mapping["owner_profile"]).is_dir()}],
        "kanban_tasks": _task_rows(mapping["board"], issue["identifier"]),
        "github": _repo_state(mapping["repo"], issue["identifier"]),
        "now": datetime.now(timezone.utc).isoformat(),
    }
    return snapshot, mapping


def _sign(source: str, payload: dict[str, Any]) -> str:
    command = SIGNER_COMMANDS.get(source)
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise RuntimeError(f"no independent signer command is configured for {source}")
    proc = subprocess.run(
        command,
        input=json.dumps(payload), text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=90, cwd="/",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300])
    return proc.stdout.strip()


def admit(identifier: str) -> dict[str, Any]:
    with _LOCK:
        engine = _load_engine()
        config = engine.validate_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        issue = fetch_issue(identifier, engine)
        snapshot, mapping = build_snapshot(issue, config)
        plan = engine.plan_intake(snapshot, config)
        actions = plan.get("actions", [])
        if not actions:
            return {"ok": True, "issue": identifier, "changed": False, "reason": plan.get("reason")}
        now = datetime.now(timezone.utc)
        payload = {
            "schema_version": 1, "source_kind": "intake", "issue_identifier": identifier,
            "config": config, "source_state": snapshot,
            "actions_digest": engine._actions_digest(actions),
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(seconds=120)).isoformat(),
            "nonce": uuid.uuid4().hex,
        }
        if any(action.get("metadata", {}).get("board") != mapping["board"] for action in actions):
            raise ValueError("signed action destination does not match the resolved board")
        envelope = {"payload": payload, "signatures": {source: _sign(source, payload) for source in ("linear", "project_registry")}}
        result = engine.apply_native_plan(
            plan, hermes_root=HERMES_ROOT,
            db_path=HOME / "kanban" / "boards" / mapping["board"] / "kanban.db",
            reviewed_native_adapter=True, authority_envelope=envelope,
        )
        return {"ok": True, "issue": identifier, "changed": True, "task_ids": result, "board": mapping["board"]}


def event_issue(payload: dict[str, Any]) -> str | None:
    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    if payload.get("type") == "Issue":
        return data.get("identifier")
    if payload.get("type") == "Comment":
        raw_issue = data.get("issue")
        issue: dict[str, Any] = raw_issue if isinstance(raw_issue, dict) else {}
        return issue.get("identifier") or data.get("issueIdentifier")
    return None


def verify_signature(body: bytes, signature: str, secret: bytes) -> bool:
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def process_webhook(body: bytes, signature: str) -> dict[str, Any]:
    secret = WEBHOOK_SECRET_PATH.read_bytes().strip()
    if not verify_signature(body, signature, secret):
        raise PermissionError("invalid Linear signature")
    payload = json.loads(body)
    issue = event_issue(payload)
    if not issue:
        return {"ok": True, "ignored": True}
    return admit(str(issue))


def _queue_connection() -> sqlite3.Connection:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(QUEUE_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS webhook_queue (
            event_id TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            issue_identifier TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','done')),
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT
        )"""
    )
    return conn


def enqueue_webhook(body: bytes, signature: str) -> dict[str, Any]:
    secret = WEBHOOK_SECRET_PATH.read_bytes().strip()
    if not verify_signature(body, signature, secret):
        raise PermissionError("invalid Linear signature")
    payload = json.loads(body)
    issue = event_issue(payload)
    if not issue:
        return {"ok": True, "ignored": True, "queued": False}
    event_id = hashlib.sha256(body).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with closing(_queue_connection()) as conn, conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO webhook_queue
               (event_id, body, issue_identifier, status, available_at, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
            (event_id, body.decode("utf-8"), str(issue), time.time(), now, now),
        )
    _WAKE.set()
    return {"ok": True, "queued": True, "duplicate": cursor.rowcount == 0, "event_id": event_id}


def _claim_webhook() -> tuple[str, str, str, int] | None:
    conn = _queue_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT event_id, body, issue_identifier, attempts
               FROM webhook_queue WHERE status = 'queued' AND available_at <= ?
               ORDER BY created_at LIMIT 1""",
            (time.time(),),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE webhook_queue SET status = 'processing', updated_at = ? WHERE event_id = ?",
            (datetime.now(timezone.utc).isoformat(), row[0]),
        )
        conn.commit()
        return str(row[0]), str(row[1]), str(row[2]), int(row[3])
    finally:
        conn.close()


def _finish_webhook(event_id: str, result: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with closing(_queue_connection()) as conn, conn:
        conn.execute(
            "UPDATE webhook_queue SET status = 'done', updated_at = ?, last_error = NULL WHERE event_id = ?",
            (now, event_id),
        )
    _atomic(STATE_PATH, {"updated_at": now, "last_result": result})


def _retry_webhook(event_id: str, attempts: int, exc: Exception) -> None:
    next_attempt = attempts + 1
    delay = min(300, 2 ** min(next_attempt, 8))
    now = datetime.now(timezone.utc).isoformat()
    with closing(_queue_connection()) as conn, conn:
        conn.execute(
            """UPDATE webhook_queue SET status = 'queued', attempts = ?, available_at = ?,
               updated_at = ?, last_error = ? WHERE event_id = ?""",
            (next_attempt, time.time() + delay, now, f"{type(exc).__name__}: {str(exc)[:300]}", event_id),
        )


def recover_queue() -> None:
    with closing(_queue_connection()) as conn, conn:
        conn.execute(
            "UPDATE webhook_queue SET status = 'queued', available_at = ?, updated_at = ? WHERE status = 'processing'",
            (time.time(), datetime.now(timezone.utc).isoformat()),
        )


def worker_loop(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    recover_queue()
    while not stop.is_set():
        claimed = _claim_webhook()
        if claimed is None:
            _WAKE.wait(1.0)
            _WAKE.clear()
            continue
        event_id, _body, issue, attempts = claimed
        try:
            _finish_webhook(event_id, admit(issue))
        except Exception as exc:
            _retry_webhook(event_id, attempts, exc)


class Handler(BaseHTTPRequestHandler):
    server_version = "PolarisFactoryIntake/1.0"
    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "polaris-factory-intake", "enabled": CONFIG_PATH.is_file() and REGISTRY_PATH.is_file()})
        else:
            self._json(404, {"ok": False})
    def do_POST(self) -> None:
        if self.path != "/linear":
            self._json(404, {"ok": False}); return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1_000_000:
                raise ValueError("invalid payload size")
            body = self.rfile.read(size)
            signature = self.headers.get("Linear-Signature", "")
            # Acknowledge only after the authenticated event is durably committed.
            # Admission then runs from the crash-recoverable retry queue.
            result = enqueue_webhook(body, signature)
            self._json(200, result)
        except PermissionError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        except Exception as exc:
            # A non-2xx response is intentional: Linear will retry instead of
            # silently losing an admission event.
            self._json(503, {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.log_date_time_string()} {format % args}", flush=True)


def _require_root_controlled(path: Path) -> None:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        raise PermissionError(f"authority runtime file is not a regular file: {path}")
    for component in (resolved, *resolved.parents):
        info = component.stat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError(f"authority runtime path is not root-controlled: {component}")


def _require_signer_command(source: str, command: Any) -> None:
    expected_users = {"linear": "polarislinear", "project_registry": "polarisproject"}
    user = expected_users.get(source)
    if user is None or not isinstance(command, list) or len(command) != 5:
        raise PermissionError("invalid independent signer command")
    expected_wrapper = f"/usr/local/libexec/polaris-factory-sign-{'project' if source == 'project_registry' else source}"
    if command != ["/usr/bin/sudo", "-n", "-u", user, expected_wrapper]:
        raise PermissionError(f"unexpected independent signer command for {source}")
    _require_root_controlled(Path(command[0]))
    _require_root_controlled(Path(command[-1]))


def bootstrap(*, enforce_ownership: bool = True) -> dict[str, Any]:
    FACTORY_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not REGISTRY_PATH.is_file():
        raise RuntimeError("independent authority signers must be provisioned separately")
    if enforce_ownership:
        _require_root_controlled(REGISTRY_PATH)
        for source, command in SIGNER_COMMANDS.items():
            _require_signer_command(source, command)
    if not WEBHOOK_SECRET_PATH.exists():
        WEBHOOK_SECRET_PATH.write_text(secrets.token_hex(32), encoding="utf-8"); os.chmod(WEBHOOK_SECRET_PATH, 0o400)
    return {"ok": True, "registry": str(REGISTRY_PATH), "signer_commands": "built-in-pinned"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap")
    admit_parser = sub.add_parser("admit"); admit_parser.add_argument("issue")
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=8655)
    args = parser.parse_args()
    if args.command == "bootstrap": print(json.dumps(bootstrap(), sort_keys=True)); return 0
    bootstrap()
    if args.command == "admit": print(json.dumps(admit(args.issue), sort_keys=True)); return 0
    threading.Thread(target=worker_loop, name="polaris-factory-worker", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
