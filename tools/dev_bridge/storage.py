from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .models import VALID_STATES, utc_now
from .security import (
    SafetyError,
    assert_no_reparse_components,
    assert_safe_child,
    canonical_json,
)


class StateStore:
    def __init__(self, state_root: Path, runtime_root: Path) -> None:
        self.state_root = state_root
        self.runtime_root = runtime_root
        self.tasks_root = state_root / "tasks"
        self.runs_root = state_root / "runs"
        self.lock_path = state_root / "run.lock"

    def ensure_layout(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        assert_safe_child(self.state_root, self.runtime_root, must_exist=True)
        self.tasks_root.mkdir(exist_ok=True)
        self.runs_root.mkdir(exist_ok=True)
        assert_no_reparse_components(self.tasks_root, start=self.runtime_root)
        assert_no_reparse_components(self.runs_root, start=self.runtime_root)

    def task_path(self, task_id: int) -> Path:
        if task_id <= 0:
            raise ValueError("task_id must be positive")
        return self.tasks_root / f"{task_id}.json"

    def load_task(self, task_id: int) -> dict[str, Any] | None:
        path = self.task_path(task_id)
        if not path.exists():
            return None
        assert_no_reparse_components(path, start=self.runtime_root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SafetyError(f"Task state is unreadable: {path.name}") from exc
        if payload.get("state") not in VALID_STATES:
            raise SafetyError(f"Task state is invalid: {path.name}")
        return payload

    def save_task(self, payload: dict[str, Any]) -> None:
        self.ensure_layout()
        state = payload.get("state")
        if state not in VALID_STATES:
            raise ValueError(f"Invalid task state: {state}")
        payload = dict(payload)
        payload["updated_at"] = utc_now()
        self._atomic_write(self.task_path(int(payload["task_id"])), payload)

    def run_dir(self, run_id: str, *, create: bool = False) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", run_id):
            raise ValueError("Invalid run_id")
        if create:
            self.ensure_layout()
        path = self.runs_root / run_id
        if create:
            path.mkdir(exist_ok=False)
            assert_safe_child(path, self.runs_root, must_exist=True)
        return path

    def stop_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "stop.request"

    def request_stop(self, run_id: str) -> None:
        path = self.stop_path(run_id)
        if not path.parent.exists():
            raise SafetyError("Run directory is missing")
        path.write_text(utc_now() + "\n", encoding="utf-8")

    def acquire_lock(self, task_id: int, run_id: str) -> dict[str, Any]:
        self.ensure_layout()
        payload = {
            "task_id": task_id,
            "run_id": run_id,
            "owner_pid": os.getpid(),
            "created_at": utc_now(),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except FileExistsError as exc:
            existing = self.read_lock()
            raise SafetyError(f"Another run is locked: {existing}") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload) + "\n")
        return payload

    def read_lock(self) -> dict[str, Any] | None:
        if not self.lock_path.exists():
            return None
        assert_no_reparse_components(self.lock_path, start=self.runtime_root)
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SafetyError("Run lock is unreadable; manual inspection required") from exc

    def release_lock(self, run_id: str) -> None:
        existing = self.read_lock()
        if not existing:
            return
        if existing.get("run_id") != run_id:
            raise SafetyError("Refusing to release another run's lock")
        self.lock_path.unlink()

    def recover_stale_lock(self) -> dict[str, Any] | None:
        existing = self.read_lock()
        if not existing:
            return None
        pid = int(existing.get("owner_pid") or 0)
        if pid > 0 and process_is_alive(pid):
            raise SafetyError("Active run cannot be recovered; use stop first")
        task_id = int(existing["task_id"])
        task = self.load_task(task_id)
        if task and task.get("state") == "running":
            task["state"] = "cancelled"
            task["finished_at"] = utc_now()
            task.setdefault("limitations", []).append("unknown_result")
            self.save_task(task)
        self.release_lock(str(existing["run_id"]))
        return existing

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True
