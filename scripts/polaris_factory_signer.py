#!/usr/bin/env python3
"""Independent evidence verifier and Ed25519 signer for factory transitions."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ALLOWED = {
    "linear": {"intake", "review", "completion"},
    "project_registry": {"intake"},
    "github": {"review", "completion", "rollback"},
    "ci": {"review", "completion"},
    "deployment": {"completion"},
    "live": {"completion"},
    "operator": {"disable", "rollback"},
}


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return evidence rows in a source-independent canonical order."""
    return sorted(rows, key=canonical_bytes)


def authority_policy(config: dict[str, Any]) -> dict[str, Any]:
    """Remove only the operational on/off switch from trusted policy."""
    return {key: value for key, value in config.items() if key != "enabled"}


@contextmanager
def _stable_readonly_database(path: Path):
    """Read a descriptor-pinned snapshot of a live SQLite database and WAL."""
    parent = path.parent.resolve(strict=True)
    resolved = parent / path.name
    wal = Path(f"{resolved}-wal")
    shm = Path(f"{resolved}-shm")
    pinned: dict[Path, tuple[int, tuple[int, int, int, int, int]]] = {}
    try:
        pinned[resolved] = _open_pinned_database_file(resolved)
        try:
            wal_before = os.stat(wal, follow_symlinks=False)
        except FileNotFoundError:
            wal_before = None
        if wal_before is not None:
            pinned[wal] = _open_pinned_database_file(wal, expected=wal_before)
        elif shm.exists():
            raise RuntimeError(f"authority database sidecars changed during open: {resolved}")

        with tempfile.TemporaryDirectory(prefix="polaris-authority-db-") as directory:
            snapshot = Path(directory) / "snapshot.db"
            _copy_pinned_file(pinned[resolved][0], snapshot)
            if wal in pinned:
                _copy_pinned_file(pinned[wal][0], Path(f"{snapshot}-wal"))
            _verify_pinned_database(resolved, wal, shm, pinned)
            uri = f"file:{urllib.parse.quote(str(snapshot), safe='/')}?mode=rw"
            conn = sqlite3.connect(uri, uri=True)
            try:
                yield conn
            finally:
                conn.close()
            _verify_pinned_database(resolved, wal, shm, pinned)
    finally:
        for descriptor, _identity in pinned.values():
            os.close(descriptor)


def _database_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_pinned_database_file(
    path: Path, *, expected: os.stat_result | None = None,
) -> tuple[int, tuple[int, int, int, int, int]]:
    before = expected or os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"authority database path is not a regular file: {path}")
    identity = _database_identity(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    if _database_identity(os.fstat(descriptor)) != identity:
        os.close(descriptor)
        raise RuntimeError(f"authority database changed during open: {path}")
    return descriptor, identity


def _copy_pinned_file(descriptor: int, destination: Path) -> None:
    output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output, view)
                view = view[written:]
            offset += len(chunk)
        os.fsync(output)
    finally:
        os.close(output)


def _verify_pinned_database(
    path: Path,
    wal: Path,
    shm: Path,
    pinned: dict[Path, tuple[int, tuple[int, int, int, int, int]]],
) -> None:
    for pathname, (descriptor, identity) in pinned.items():
        try:
            current = os.stat(pathname, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"authority database changed during readback: {path}") from exc
        if _database_identity(os.fstat(descriptor)) != identity or _database_identity(current) != identity:
            raise RuntimeError(f"authority database changed during readback: {path}")
    if (wal in pinned) != wal.exists() or (wal not in pinned and shm.exists()):
        raise RuntimeError(f"authority database sidecars changed during readback: {path}")


def _read_secret(path: Path | None, label: str) -> str:
    if path is None:
        raise RuntimeError(f"{label} credential path is unavailable")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{label} credential is unavailable")
    return value


def _linear_issue(identifier: str, token_path: Path | None) -> dict[str, Any]:
    token = _read_secret(token_path, "Linear")
    query = """query($id:String!){ issue(id:$id){ id identifier title description createdAt team{key} project{id name} comments(first:100){nodes{id body createdAt user{id name}}} } }"""
    request = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": query, "variables": {"id": identifier}}).encode(),
        headers={"Authorization": token, "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if result.get("errors"):
        raise RuntimeError(str(result["errors"])[:500])
    data = result["data"]["issue"]
    if not data or not data.get("project"):
        raise ValueError("Linear authority found no project-bound issue")
    repo_match = re.search(r"(?ms)^##\s+Repository identity\s*$\n(.*?)(?=^##\s+|\Z)", data.get("description") or "")
    if not repo_match:
        raise ValueError("Linear authority found no repository identity")
    return {
        "id": data["id"], "identifier": data["identifier"], "title": data["title"],
        "description": data["description"], "spec_finalized_at": data["createdAt"],
        "team_key": data["team"]["key"], "project_id": data["project"]["id"],
        "repository": repo_match.group(1).strip(),
        "comments": _canonical_rows([
            {"id": row["id"], "body": row["body"], "created_at": row["createdAt"],
             "actor": {"id": (row.get("user") or {}).get("id"), "is_bot": False}}
            for row in data["comments"]["nodes"]
        ]),
    }


def _repo_state(repo: str, issue: str, token_path: Path | None) -> dict[str, Any]:
    token = _read_secret(token_path, "GitHub")
    def gh(path: str) -> Any:
        request = urllib.request.Request(
            f"https://api.github.com/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "polaris-factory-project-authority",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    needle = issue.casefold()
    branches = [{"name": row["name"], "issue_identifier": issue}
                for row in gh(f"repos/{repo}/branches?per_page=100") if needle in row["name"].casefold()]
    pulls = [{"number": row["number"], "branch": row["head"]["ref"], "state": row["state"], "issue_identifier": issue}
             for row in gh(f"repos/{repo}/pulls?state=open&per_page=100")
             if needle in (row["title"] + " " + row["head"]["ref"]).casefold()]
    return {
        "branches": _canonical_rows(branches),
        "pull_requests": _canonical_rows(pulls),
    }


def _task_rows(home: Path, board: str, issue: str) -> list[dict[str, Any]]:
    path = home / "kanban" / "boards" / board / "kanban.db"
    with _stable_readonly_database(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = []
        prefix = f"polaris-software-factory:{issue.casefold()}:"
        for task in conn.execute("SELECT id,title,body,status,branch_name,idempotency_key FROM tasks"):
            text = f"{task['title'] or ''}\n{task['body'] or ''}".casefold()
            if not (str(task["idempotency_key"] or "").casefold().startswith(prefix)
                    or f"[{issue}]".casefold() in text or f"[linear:{issue}]".casefold() in text):
                continue
            marker = re.search(r"POLARIS_FACTORY_CHECKPOINT_V1 sha256:[0-9a-f]{64}", task["body"] or "")
            rows.append({"id": task["id"], "idempotency_key": task["idempotency_key"], "status": task["status"],
                         "branch_name": task["branch_name"], "body": task["body"],
                         "checkpoint_fingerprint": marker.group(0) if marker else None,
                         "metadata": {"linear_identifier": issue}})
        return _canonical_rows(rows)


def verify_evidence(
    source: str,
    payload: dict[str, Any],
    home: Path,
    trusted_config_path: Path,
    linear_token_path: Path | None = None,
    github_token_path: Path | None = None,
) -> None:
    state = payload.get("source_state")
    if not isinstance(state, dict):
        raise ValueError("source_state must be an object")
    issue_id = str(payload.get("issue_identifier") or "")
    if source == "linear":
        if state.get("issue") != _linear_issue(issue_id, linear_token_path):
            raise ValueError("Linear evidence does not match an independent API readback")
        return
    if source == "project_registry":
        issue = state.get("issue")
        caller_config = payload.get("config")
        if not isinstance(issue, dict) or not isinstance(caller_config, dict):
            raise ValueError("project authority lacks canonical issue/config")
        trusted_config = json.loads(trusted_config_path.read_text(encoding="utf-8"))
        if canonical_bytes(authority_policy(caller_config)) != canonical_bytes(authority_policy(trusted_config)):
            raise ValueError("project authority rejects caller config drift")
        matches = [m for m in trusted_config.get("project_mappings", []) if isinstance(m, dict)
                   and m.get("linear_team") == issue.get("team_key")
                   and m.get("linear_project_id") == issue.get("project_id")
                   and str(m.get("repo", "")).casefold() == str(issue.get("repository", "")).casefold()]
        if len(matches) != 1:
            raise ValueError("project authority found ambiguous mapping")
        mapping = matches[0]
        with _stable_readonly_database(home / "projects.db") as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT id,board_slug,primary_path,archived FROM projects WHERE id=?", (mapping["project_id"],)).fetchone()
        if not row or row["archived"] or row["board_slug"] != mapping["board"] or row["primary_path"] != mapping["workspace"]:
            raise ValueError("project authority found registry/mapping drift")
        expected_projects = [{"id": mapping["project_id"], "repo": mapping["repo"], "archived": False}]
        expected_profiles = [{"name": mapping["owner_profile"], "enabled": (home / "profiles" / mapping["owner_profile"]).is_dir()}]
        if state.get("project_registry") != expected_projects or state.get("owner_profiles") != expected_profiles:
            raise ValueError("project authority found project/profile evidence drift")
        if state.get("kanban_tasks") != _task_rows(home, mapping["board"], issue_id):
            raise ValueError("project authority found Kanban evidence drift")
        if state.get("github") != _repo_state(mapping["repo"], issue_id, github_token_path):
            raise ValueError("project authority found GitHub claim drift")
        return
    raise ValueError("this signer does not yet support the requested evidence transition")


def sign(
    source: str,
    payload: dict[str, Any],
    signer_root: Path,
    home: Path,
    trusted_config_path: Path,
    linear_token_path: Path | None = None,
    github_token_path: Path | None = None,
) -> str:
    if source not in ALLOWED or payload.get("source_kind") not in ALLOWED[source]:
        raise ValueError("source is not authorized for this transition")
    verify_evidence(
        source, payload, home, trusted_config_path,
        linear_token_path=linear_token_path,
        github_token_path=github_token_path,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = (signer_root / source / "private.key").read_bytes()
    if len(raw) != 32:
        raise ValueError("invalid private key")
    return base64.b64encode(Ed25519PrivateKey.from_private_bytes(raw).sign(canonical_bytes(payload))).decode("ascii")


def provision(signer_root: Path, registry: Path) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    authorities = {}
    signer_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source in ALLOWED:
        directory = signer_root / source; directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "private.key"
        private = Ed25519PrivateKey.from_private_bytes(path.read_bytes()) if path.exists() else Ed25519PrivateKey.generate()
        if not path.exists():
            path.write_bytes(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
        os.chmod(path, 0o400)
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        authorities[source] = {"public_key": base64.b64encode(public).decode("ascii"), "source_kinds": sorted(ALLOWED[source])}
    registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = registry.with_suffix(".tmp")
    temp.write_text(
        json.dumps({"schema_version": 1, "authorities": authorities}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o444); os.replace(temp, registry)
    return {"ok": True, "authorities": len(authorities)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sign_parser = sub.add_parser("sign"); sign_parser.add_argument("--source", required=True, choices=sorted(ALLOWED))
    sign_parser.add_argument("--signer-root", type=Path, required=True); sign_parser.add_argument("--home", type=Path, required=True)
    sign_parser.add_argument("--trusted-config", type=Path, required=True)
    sign_parser.add_argument("--linear-token-file", type=Path)
    sign_parser.add_argument("--github-token-file", type=Path)
    provision_parser = sub.add_parser("provision"); provision_parser.add_argument("--signer-root", type=Path, required=True)
    provision_parser.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "provision":
        print(json.dumps(provision(args.signer_root, args.registry), sort_keys=True)); return 0
    payload = json.load(sys.stdin)
    print(sign(
        args.source, payload, args.signer_root, args.home, args.trusted_config,
        linear_token_path=args.linear_token_file,
        github_token_path=args.github_token_file,
    )); return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
