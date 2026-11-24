from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

from .models import BridgeConfig
from .security import (
    SafetyError,
    assert_no_reparse_components,
    assert_not_protected,
    assert_safe_child,
    inspect_workspace,
    redact_text,
    safe_changed_files,
)


ALLOWED_REMOTE = "https://github.com/BogdanMaslyuk/PharmDesk.git"


class GitWorkspaceFactory:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def _run(self, arguments: list[str], *, timeout: int = 180) -> str:
        try:
            result = subprocess.run(
                [self.config.git_command, *arguments],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SafetyError(f"Git workspace operation failed: {type(exc).__name__}") from exc
        if result.returncode != 0:
            details = redact_text(
                result.stderr or result.stdout,
                home=Path.home(),
                runtime_root=self.config.runtime_root,
                max_bytes=16_384,
            ).strip()
            raise SafetyError(f"Git workspace operation failed ({result.returncode}): {details}")
        return result.stdout

    def _validated_source(self) -> str:
        source = self.config.workspace_source
        if source.startswith("https://"):
            if source != ALLOWED_REMOTE:
                raise SafetyError("Remote workspace source is not allowlisted")
            return source
        source_path = Path(source).expanduser()
        source_path = assert_safe_child(
            source_path,
            self.config.seed_root,
            must_exist=True,
        )
        assert_not_protected(source_path, self.config.protected_roots)
        assert_no_reparse_components(source_path, start=self.config.seed_root)
        return str(source_path)

    def prepare(self, task_id: int, digest: str, run_id: str) -> tuple[Path, str]:
        self.config.workspaces_root.mkdir(parents=True, exist_ok=True)
        assert_safe_child(
            self.config.workspaces_root,
            self.config.runtime_root,
            must_exist=True,
        )
        target = self.config.workspaces_root / f"issue-{task_id}-{run_id}"
        assert_safe_child(target, self.config.workspaces_root, must_exist=False)
        if target.exists():
            raise SafetyError(f"Workspace already exists: {target.name}")

        branch = f"bridge/issue-{task_id}-{digest[:10]}"
        if not re.fullmatch(r"bridge/issue-[1-9][0-9]*-[0-9a-f]{10}", branch):
            raise SafetyError("Generated branch name is invalid")
        source = self._validated_source()
        self._run(
            [
                "clone",
                "--no-hardlinks",
                "--no-tags",
                "--single-branch",
                "--branch",
                self.config.base_ref,
                source,
                str(target),
            ]
        )
        resolved = assert_safe_child(target, self.config.workspaces_root, must_exist=True)
        inspect_workspace(
            resolved,
            workspaces_root=self.config.workspaces_root,
            protected_roots=self.config.protected_roots,
        )
        self._run(["-C", str(resolved), "switch", "-c", branch])
        return resolved, branch

    def changed_files(self, workspace: Path) -> list[str]:
        resolved = assert_safe_child(
            workspace,
            self.config.workspaces_root,
            must_exist=True,
        )
        tracked = self._run(["-C", str(resolved), "diff", "--name-only", "HEAD"])
        untracked = self._run(
            ["-C", str(resolved), "ls-files", "--others", "--exclude-standard"]
        )
        return safe_changed_files([*tracked.splitlines(), *untracked.splitlines()])

    def head_commit(self, workspace: Path) -> str:
        resolved = assert_safe_child(
            workspace,
            self.config.workspaces_root,
            must_exist=True,
        )
        return self._run(["-C", str(resolved), "rev-parse", "HEAD"]).strip()
