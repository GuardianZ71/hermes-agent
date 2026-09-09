#!/usr/bin/env python3
"""Independent evidence verifier and Ed25519 signer for factory transitions."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
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


def authority_policy(config: dict[str, Any]) -> dict[str, Any]:
    """Remove only the operational on/off switch from trusted policy."""
    return {key: value for key, value in config.items() if key != "enabled"}


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
        "comments": [
            {"id": row["id"], "body": row["body"], "created_at": row["createdAt"],
             "actor": {"id": (row.get("user") or {}).get("id"), "is_bot": False}}
            for row in data["comments"]["nodes"]
        ],
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
    return {"branches": branches, "pull_requests": pulls}


def _task_rows(home: Path, board: str, issue: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{home / 'kanban' / 'boards' / board / 'kanban.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
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
        return rows
    finally:
        conn.close()


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
        conn = sqlite3.connect(f"file:{home / 'projects.db'}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT id,board_slug,primary_path,archived FROM projects WHERE id=?", (mapping["project_id"],)).fetchone()
        finally:
            conn.close()
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
