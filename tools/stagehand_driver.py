"""Persistent Stagehand v4 sidecar adapter for the existing browser_* contract.

The public browser tools remain in :mod:`tools.browser_tool`.  This module only
translates its internal agent-browser command envelope to the line-delimited
JSON protocol implemented by ``stagehand_sidecar.mjs``.
"""
from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli._subprocess_compat import windows_hide_flags

logger = logging.getLogger(__name__)

# Stagehand covers the contract-bearing commands. Recording and annotated
# screenshots intentionally stay on agent-browser until Stagehand v4 exposes
# equivalent deterministic primitives.
_SUPPORTED_COMMANDS = frozenset({
    "open", "snapshot", "click", "fill", "scroll", "back", "press",
    "eval", "console", "errors", "screenshot", "close",
})


class _Worker:
    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.lock = threading.Lock()
        self.next_id = 1


_workers: dict[str, _Worker] = {}
_workers_lock = threading.RLock()


def sidecar_path() -> Path:
    return Path(__file__).with_name("stagehand_sidecar.mjs")


def package_available() -> bool:
    root = Path(__file__).resolve().parent.parent
    return sidecar_path().is_file() and (root / "node_modules" / "@browserbasehq" / "stagehand").is_dir()


def supports(command: str, args: list[str]) -> bool:
    if command not in _SUPPORTED_COMMANDS:
        return False
    # Stagehand can capture screenshots, but does not provide agent-browser's
    # numbered annotation overlay. Preserve that exact behavior via fallback.
    if command == "screenshot" and "--annotate" in args:
        return False
    return True


def _start_worker(session_key: str, env: dict[str, str]) -> _Worker:
    node = env.get("HERMES_NODE_BINARY") or "node"
    popen_extra: dict[str, Any] = {}
    if os.name == "nt":
        popen_extra["creationflags"] = windows_hide_flags()
    process = subprocess.Popen(
        [node, str(sidecar_path())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
        **popen_extra,
    )
    worker = _Worker(process)
    logger.debug("Started Stagehand sidecar pid=%s task=%s", process.pid, session_key)
    return worker


def _worker_for(session_key: str, env: dict[str, str]) -> _Worker:
    with _workers_lock:
        worker = _workers.get(session_key)
        if worker is None or worker.process.poll() is not None:
            if worker is not None:
                _workers.pop(session_key, None)
            worker = _start_worker(session_key, env)
            _workers[session_key] = worker
        return worker


def _readline_with_timeout(stream: Any, timeout: float) -> str:
    if os.name == "nt":
        # select() cannot wait on Windows pipes. A daemon thread is bounded by
        # the process termination path in ``request``.
        result: list[str] = []
        done = threading.Event()

        def read() -> None:
            result.append(stream.readline())
            done.set()

        threading.Thread(target=read, daemon=True).start()
        if not done.wait(timeout):
            raise TimeoutError(f"Stagehand response timed out after {timeout:g} seconds")
        return result[0]

    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        raise TimeoutError(f"Stagehand response timed out after {timeout:g} seconds")
    return stream.readline()


def _terminate(worker: _Worker) -> None:
    process = worker.process
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def request(
    session_key: str,
    command: str,
    args: list[str],
    config: dict[str, Any],
    env: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    worker = _worker_for(session_key, env)
    with worker.lock:
        process = worker.process
        request_id = worker.next_id
        worker.next_id += 1
        payload = {"id": request_id, "command": command, "args": args, "config": config}
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
            line = _readline_with_timeout(process.stdout, timeout)
            if not line:
                stderr = ""
                if process.stderr is not None and process.poll() is not None:
                    stderr = process.stderr.read().strip()
                raise RuntimeError(stderr or f"Stagehand sidecar exited with code {process.poll()}")
            response = json.loads(line)
            if response.get("id") != request_id:
                raise RuntimeError("Stagehand sidecar returned a mismatched response id")
            response.pop("id", None)
            return response
        except Exception:
            _terminate(worker)
            with _workers_lock:
                if _workers.get(session_key) is worker:
                    _workers.pop(session_key, None)
            raise


def close(session_key: str, timeout: float = 8) -> None:
    with _workers_lock:
        worker = _workers.pop(session_key, None)
    if worker is None:
        return
    try:
        with worker.lock:
            process = worker.process
            if process.poll() is None:
                assert process.stdin is not None and process.stdout is not None
                request_id = worker.next_id
                process.stdin.write(json.dumps({"id": request_id, "command": "close"}) + "\n")
                process.stdin.flush()
                line = _readline_with_timeout(process.stdout, timeout)
                if line:
                    response = json.loads(line)
                    if not response.get("success"):
                        logger.warning("Stagehand close failed for %s: %s", session_key, response.get("error"))
                process.wait(timeout=max(1, timeout))
    except Exception as exc:
        logger.warning("Stagehand sidecar did not close cleanly for %s: %s", session_key, exc)
    finally:
        _terminate(worker)


def close_all() -> None:
    with _workers_lock:
        keys = list(_workers)
    for key in keys:
        close(key)


def active_worker_count() -> int:
    with _workers_lock:
        return sum(worker.process.poll() is None for worker in _workers.values())
