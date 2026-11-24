from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any


BRIDGE_SCHEMA = "pharmdesk-dev-bridge/v1"
VALID_STATES = {
    "proposed",
    "approved",
    "running",
    "needs_approval",
    "completed",
    "failed",
    "cancelled",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class IssueSnapshot:
    repo: str
    number: int
    title: str
    body: str
    author_login: str
    updated_at: str
    state: str
    url: str

    @classmethod
    def from_gh(cls, repo: str, payload: dict[str, Any]) -> "IssueSnapshot":
        author = payload.get("author") or {}
        return cls(
            repo=repo,
            number=int(payload["number"]),
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            author_login=str(author.get("login") or ""),
            updated_at=str(payload.get("updatedAt") or ""),
            state=str(payload.get("state") or ""),
            url=str(payload.get("url") or ""),
        )

    def approval_payload(self, execution_policy: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": BRIDGE_SCHEMA,
            "source": {
                "repo": self.repo,
                "number": self.number,
                "title": self.title,
                "body": self.body,
                "author_login": self.author_login,
                "updated_at": self.updated_at,
                "state": self.state,
                "url": self.url,
            },
            "execution_policy": execution_policy,
        }


@dataclass
class RunResult:
    exit_code: int
    outcome: str
    tests_status: str
    tests_summary: dict[str, int | str] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    final_message: str = ""
    approval_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BridgeConfig:
    repo: str
    owner: str
    runtime_root: Path
    code_checkout: Path
    workspaces_root: Path
    state_root: Path
    logs_root: Path
    seed_root: Path
    workspace_source: str
    base_ref: str
    git_command: str
    gh_command: str
    codex_command: Path
    codex_home: Path
    codex_profile: str
    runner_adapter: str
    max_runtime_seconds: int
    max_log_bytes: int
    max_issue_chars: int
    protected_roots: tuple[Path, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeConfig":
        required = {
            "repo",
            "owner",
            "runtime_root",
            "code_checkout",
            "workspace_source",
            "codex_command",
            "codex_home",
            "protected_roots",
        }
        missing = sorted(required - data.keys())
        if missing:
            raise ValueError(f"Missing config fields: {', '.join(missing)}")

        runtime_root = Path(str(data["runtime_root"])).expanduser().resolve()

        def runtime_path(key: str, default: str) -> Path:
            raw = Path(str(data.get(key, default))).expanduser()
            if not raw.is_absolute():
                raw = runtime_root / raw
            return raw.resolve()

        config = cls(
            repo=str(data["repo"]),
            owner=str(data["owner"]),
            runtime_root=runtime_root,
            code_checkout=Path(str(data["code_checkout"])).expanduser().resolve(),
            workspaces_root=runtime_path("workspaces_root", "workspaces"),
            state_root=runtime_path("state_root", "state"),
            logs_root=runtime_path("logs_root", "logs"),
            seed_root=runtime_path("seed_root", "seed"),
            workspace_source=str(data["workspace_source"]),
            base_ref=str(data.get("base_ref", "main")),
            git_command=str(data.get("git_command", "git")),
            gh_command=str(data.get("gh_command", "gh")),
            codex_command=Path(str(data["codex_command"])).expanduser().resolve(),
            codex_home=Path(str(data["codex_home"])).expanduser().resolve(),
            codex_profile=str(data.get("codex_profile", "bridge-workspace")),
            runner_adapter=str(data.get("runner_adapter", "mock")),
            max_runtime_seconds=int(data.get("max_runtime_seconds", 300)),
            max_log_bytes=int(data.get("max_log_bytes", 1_048_576)),
            max_issue_chars=int(data.get("max_issue_chars", 50_000)),
            protected_roots=tuple(
                Path(str(item)).expanduser().resolve()
                for item in data["protected_roots"]
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.repo != "BogdanMaslyuk/PharmDesk":
            raise ValueError("Only BogdanMaslyuk/PharmDesk is allowed")
        if self.owner != "BogdanMaslyuk":
            raise ValueError("Only GitHub owner BogdanMaslyuk is allowed")
        if self.runner_adapter not in {"mock", "codex"}:
            raise ValueError("runner_adapter must be 'mock' or 'codex'")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", self.base_ref):
            raise ValueError("base_ref contains unsupported characters")
        if self.base_ref.startswith(("-", "/")) or ".." in self.base_ref:
            raise ValueError("base_ref is unsafe")
        if not 30 <= self.max_runtime_seconds <= 1800:
            raise ValueError("max_runtime_seconds must be between 30 and 1800")
        if not 65_536 <= self.max_log_bytes <= 10_485_760:
            raise ValueError("max_log_bytes is outside the safe range")
        if not 1_000 <= self.max_issue_chars <= 100_000:
            raise ValueError("max_issue_chars is outside the safe range")
        if not self.runtime_root.is_absolute() or not self.code_checkout.is_absolute():
            raise ValueError("Bridge paths must be absolute")

    def execution_policy(self) -> dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "runner_adapter": self.runner_adapter,
            "codex_profile": self.codex_profile,
            "network_for_agent_commands": False,
            "publish_report": False,
            "git_push": False,
            "max_runtime_seconds": self.max_runtime_seconds,
            "workspace_source": self.workspace_source,
        }
