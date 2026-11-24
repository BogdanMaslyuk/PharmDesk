from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .github_client import GithubClient
from .models import BridgeConfig, IssueSnapshot, utc_now
from .runner import CodexRunner, MockRunner
from .security import (
    SafetyError,
    approval_digest,
    assert_no_reparse_components,
    assert_not_protected,
    canonical_json,
    inspect_workspace,
    public_report_from_state,
    report_digest,
)
from .storage import StateStore, process_is_alive
from .workspace import GitWorkspaceFactory


def default_config_path() -> Path:
    override = os.environ.get("PHARMDESK_BRIDGE_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return (
        Path.home()
        / "Desktop"
        / "PharmDesk_Project"
        / "_DevBridge"
        / "config.json"
    ).resolve()


def load_config(path: Path | None = None) -> BridgeConfig:
    config_path = (path or default_config_path()).expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SafetyError(f"Bridge config is missing: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise SafetyError(f"Bridge config is invalid JSON: {config_path}") from exc
    return BridgeConfig.from_dict(data)


class BridgeService:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        github: Any | None = None,
        workspace_factory: Any | None = None,
        runner: Any | None = None,
    ) -> None:
        self.config = config
        self.github = github or GithubClient(config.gh_command, config.repo, config.owner)
        self.workspace_factory = workspace_factory or GitWorkspaceFactory(config)
        if runner is not None:
            self.runner = runner
        elif config.runner_adapter == "codex":
            self.runner = CodexRunner(config)
        else:
            self.runner = MockRunner(self._mock_limitation())
        self.store = StateStore(config.state_root, config.runtime_root)

    def doctor(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "schema": "pharmdesk-dev-bridge-doctor/v1",
            "repo": self.config.repo,
            "runner_adapter": self.config.runner_adapter,
            "runtime_root": str(self.config.runtime_root),
            "code_checkout": str(self.config.code_checkout),
            "checks": {},
        }
        failures: list[str] = []
        try:
            assert_not_protected(self.config.runtime_root, self.config.protected_roots)
            assert_no_reparse_components(
                self.config.runtime_root,
                start=self.config.runtime_root,
            )
            checks["checks"]["runtime_path"] = "ok"
        except SafetyError as exc:
            checks["checks"]["runtime_path"] = str(exc)
            failures.append("runtime_path")

        for label, path in {
            "runtime_root": self.config.runtime_root,
            "code_checkout": self.config.code_checkout,
            "codex_command": self.config.codex_command,
        }.items():
            exists = path.exists()
            checks["checks"][label] = "ok" if exists else "missing"
            if not exists:
                failures.append(label)

        try:
            checks["checks"]["github"] = self.github.doctor()
        except Exception as exc:  # doctor must report, not mutate or retry
            checks["checks"]["github"] = type(exc).__name__
            failures.append("github")

        checks["checks"]["git"] = self._git_version()
        if self.config.runner_adapter == "codex":
            try:
                checks["checks"]["codex_version"] = self.runner.version()
                checks["checks"]["codex_authenticated"] = self.runner.login_status()
                checks["checks"]["isolation"] = self.runner.verify_attestation()
                if not checks["checks"]["codex_authenticated"]:
                    failures.append("codex_authenticated")
            except Exception as exc:
                checks["checks"]["codex"] = str(exc)
                failures.append("codex")
        else:
            checks["checks"]["codex"] = "mock-only"
            attestation_path = self.config.codex_home / "isolation-attestation.json"
            if attestation_path.is_file():
                try:
                    checks["checks"]["isolation_probe"] = json.loads(
                        attestation_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    checks["checks"]["isolation_probe"] = "unreadable"
        lock = self.store.read_lock() if self.config.state_root.exists() else None
        checks["checks"]["active_lock"] = lock
        checks["ready"] = not failures
        checks["failures"] = sorted(set(failures))
        return checks

    def preview(self, task_id: int) -> dict[str, Any]:
        issue = self._fetch_issue(task_id)
        digest = self._digest(issue)
        existing = self.store.load_task(task_id)
        if existing and existing.get("approved_digest") == digest:
            state = existing
        elif existing and existing.get("state") == "running":
            raise SafetyError("Running task cannot be replaced by a changed issue")
        else:
            state = self._proposed_state(issue, digest)
            self.store.save_task(state)
        return {
            "task_id": task_id,
            "source_issue": issue.url,
            "author_login": issue.author_login,
            "updated_at": issue.updated_at,
            "digest": digest,
            "state": state["state"],
            "title": issue.title,
            "body_chars": len(issue.body),
            "comments_consumed": False,
            "execution_policy": self.config.execution_policy(),
        }

    def approve(self, task_id: int, digest: str) -> dict[str, Any]:
        issue = self._fetch_issue(task_id)
        current_digest = self._digest(issue)
        if digest.casefold() != current_digest:
            raise SafetyError("Approval digest does not match the current issue")
        state = self.store.load_task(task_id) or self._proposed_state(issue, current_digest)
        if state.get("state") == "running":
            raise SafetyError("Running task cannot receive a replacement approval")
        state.update(
            {
                "state": "approved",
                "approved_digest": current_digest,
                "approval_id": uuid.uuid4().hex,
                "approval_used": False,
                "approved_at": utc_now(),
                "source_updated_at": issue.updated_at,
                "run_id": "",
                "report_url": "",
            }
        )
        self.store.save_task(state)
        return self._status_view(state)

    def run(self, task_id: int) -> dict[str, Any]:
        state = self.store.load_task(task_id)
        if not state or state.get("state") != "approved":
            raise SafetyError("Task needs an explicit current approval before run")
        if state.get("approval_used"):
            raise SafetyError("This approval has already been consumed")
        issue = self._fetch_issue(task_id)
        digest = self._digest(issue)
        if digest != state.get("approved_digest"):
            changed = self._proposed_state(issue, digest)
            self.store.save_task(changed)
            raise SafetyError("Issue changed after approval; a new approval is required")

        run_id = f"run-{task_id}-{uuid.uuid4().hex[:12]}"
        self.store.acquire_lock(task_id, run_id)
        run_dir: Path | None = None
        try:
            run_dir = self.store.run_dir(run_id, create=True)
            state.update(
                {
                    "state": "running",
                    "approval_used": True,
                    "run_id": run_id,
                    "started_at": utc_now(),
                    "finished_at": "",
                    "tests_summary": {
                        "status": "not_run",
                        "passed": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    "limitations": [],
                    "changed_files": [],
                    "commit": "",
                }
            )
            self.store.save_task(state)
            workspace, branch = self.workspace_factory.prepare(task_id, digest, run_id)
            inspection = inspect_workspace(
                workspace,
                workspaces_root=self.config.workspaces_root,
                protected_roots=self.config.protected_roots,
            )
            state["workspace"] = str(workspace)
            state["git_ref"] = branch
            state["workspace_files"] = inspection["files"]
            state["base_commit"] = self.workspace_factory.head_commit(workspace)
            self.store.save_task(state)

            result = self.runner.run(
                issue,
                workspace,
                run_dir,
                self.store.stop_path(run_id),
            )
            outcome = result.outcome
            if outcome == "completed" and (
                result.exit_code != 0 or result.tests_status != "passed"
            ):
                outcome = (
                    "needs_approval"
                    if result.tests_status == "not_run"
                    else "failed"
                )
                if result.tests_status == "not_run":
                    result.limitations.append("tests_not_run")
                else:
                    result.limitations.append("tests_failed")
            state["state"] = outcome
            state["exit_code"] = result.exit_code
            state["tests_summary"] = result.tests_summary
            state["limitations"] = sorted(set(result.limitations))
            state["approval_reason"] = result.approval_reason
            state["changed_files"] = self.workspace_factory.changed_files(workspace)
            state["finished_at"] = utc_now()
            self.store.save_task(state)
            return self._status_view(state)
        except Exception:
            current = self.store.load_task(task_id) or state
            if current.get("state") == "running":
                current["state"] = "failed"
                current["finished_at"] = utc_now()
                current["limitations"] = sorted(
                    set([*current.get("limitations", []), "unknown_result"])
                )
                self.store.save_task(current)
            raise
        finally:
            self.store.release_lock(run_id)

    def status(self, task_id: int | None = None) -> dict[str, Any]:
        lock = self.store.read_lock() if self.config.state_root.exists() else None
        if task_id is not None:
            state = self.store.load_task(task_id)
            if not state:
                return {"task_id": task_id, "state": "unknown", "active_lock": lock}
            result = self._status_view(state)
            result["active_lock"] = lock
            return result
        tasks: list[dict[str, Any]] = []
        if self.store.tasks_root.exists():
            for path in sorted(self.store.tasks_root.glob("*.json")):
                if not path.stem.isdigit():
                    continue
                state = self.store.load_task(int(path.stem))
                if state:
                    tasks.append(self._status_view(state))
        return {"tasks": tasks, "active_lock": lock}

    def stop(self) -> dict[str, Any]:
        lock = self.store.read_lock()
        if not lock:
            return {"state": "idle", "stop_requested": False}
        pid = int(lock.get("owner_pid") or 0)
        if pid and not process_is_alive(pid):
            recovered = self.store.recover_stale_lock()
            return {"state": "cancelled", "recovered_stale_lock": recovered}
        self.store.request_stop(str(lock["run_id"]))
        return {
            "state": "running",
            "run_id": lock["run_id"],
            "stop_requested": True,
        }

    def recover(self) -> dict[str, Any]:
        recovered = self.store.recover_stale_lock()
        return {"recovered": recovered}

    def report_preview(self, task_id: int) -> dict[str, Any]:
        state = self.store.load_task(task_id)
        if not state:
            raise SafetyError("Task state is missing")
        if state.get("state") in {"proposed", "approved", "running"}:
            raise SafetyError("Only a finished run can produce a report")
        report = public_report_from_state(state)
        return {"digest": report_digest(report), "report": report}

    def publish_report(self, task_id: int, digest: str) -> dict[str, Any]:
        preview = self.report_preview(task_id)
        if digest.casefold() != preview["digest"]:
            raise SafetyError("Report digest does not match the current safe report")
        url = self.github.publish_report(task_id, preview["report"])
        state = self.store.load_task(task_id)
        assert state is not None
        state["report_url"] = url
        state["report_digest"] = preview["digest"]
        state["report_published_at"] = utc_now()
        self.store.save_task(state)
        return {"report_url": url, "digest": preview["digest"]}

    def _fetch_issue(self, task_id: int) -> IssueSnapshot:
        issue = self.github.fetch_issue(task_id)
        if len(issue.body) > self.config.max_issue_chars:
            raise SafetyError("Issue body exceeds the configured size limit")
        return issue

    def _digest(self, issue: IssueSnapshot) -> str:
        return approval_digest(issue.approval_payload(self.config.execution_policy()))

    @staticmethod
    def _proposed_state(issue: IssueSnapshot, digest: str) -> dict[str, Any]:
        return {
            "task_id": issue.number,
            "source_issue": issue.url,
            "source_updated_at": issue.updated_at,
            "proposed_digest": digest,
            "approved_digest": "",
            "approval_id": "",
            "approval_used": False,
            "state": "proposed",
            "run_id": "",
            "started_at": "",
            "finished_at": "",
            "git_ref": "",
            "tests_summary": {
                "status": "not_run",
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            },
            "report_url": "",
            "limitations": [],
            "changed_files": [],
        }

    @staticmethod
    def _status_view(state: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "task_id",
            "source_issue",
            "approved_digest",
            "approval_used",
            "run_id",
            "started_at",
            "finished_at",
            "state",
            "git_ref",
            "tests_summary",
            "report_url",
            "limitations",
            "changed_files",
            "exit_code",
            "approval_reason",
        }
        return {key: state[key] for key in allowed if key in state}

    def _git_version(self) -> str:
        try:
            result = subprocess.run(
                [self.config.git_command, "--version"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except OSError:
            return "unavailable"
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    def _mock_limitation(self) -> str:
        attestation_path = self.config.codex_home / "isolation-attestation.json"
        if attestation_path.is_file():
            try:
                payload = json.loads(attestation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return "isolation_not_attested"
            if payload.get("result") != "passed":
                return "isolation_not_attested"
        if self.config.codex_command.is_file():
            return "isolation_not_attested"
        return "codex_cli_unavailable"


def render_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
