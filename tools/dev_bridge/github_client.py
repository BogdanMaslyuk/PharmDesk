from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from .models import IssueSnapshot
from .security import SafetyError, canonical_json, redact_text


@dataclass
class GithubError(RuntimeError):
    message: str

    def __str__(self) -> str:
        return self.message


class GithubClient:
    """Small, fixed-argument wrapper around the authenticated GitHub CLI."""

    def __init__(self, command: str, repo: str, owner: str) -> None:
        self.command = command
        self.repo = repo
        self.owner = owner

    def _run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.command, *arguments],
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GithubError(f"GitHub CLI unavailable: {type(exc).__name__}") from exc
        if result.returncode != 0:
            message = redact_text(result.stderr or result.stdout, max_bytes=8_192).strip()
            raise GithubError(f"GitHub CLI failed ({result.returncode}): {message}")
        return result

    def doctor(self) -> dict[str, Any]:
        version = self._run(["--version"]).stdout.splitlines()[0].strip()
        auth = self._run(["auth", "status", "--hostname", "github.com"])
        return {
            "available": True,
            "version": version,
            "authenticated": auth.returncode == 0,
            "repo": self.repo,
            "owner": self.owner,
        }

    def fetch_issue(self, issue_number: int) -> IssueSnapshot:
        if issue_number <= 0:
            raise ValueError("Issue number must be positive")
        result = self._run(
            [
                "issue",
                "view",
                str(issue_number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,state,author,updatedAt,url",
            ]
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GithubError("GitHub CLI returned invalid issue JSON") from exc
        issue = IssueSnapshot.from_gh(self.repo, payload)
        self.validate_issue(issue)
        return issue

    def validate_issue(self, issue: IssueSnapshot) -> None:
        if issue.repo != self.repo:
            raise SafetyError("Issue repository is not allowlisted")
        if issue.author_login != self.owner:
            raise SafetyError("Issue author is not the allowlisted GitHub owner")
        if issue.state.upper() != "OPEN":
            raise SafetyError("Only open issues can be approved or run")
        if not issue.url.startswith(f"https://github.com/{self.repo}/issues/"):
            raise SafetyError("Issue URL does not match the allowlisted repository")

    def publish_report(self, issue_number: int, report: dict[str, Any]) -> str:
        body = (
            "### PharmDesk Development Bridge — обезличенный итог\n\n"
            "```json\n"
            f"{json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)}\n"
            "```\n"
        )
        result = self._run(
            [
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                self.repo,
                "--body-file",
                "-",
            ],
            input_text=body,
        )
        output = result.stdout.strip()
        if output and not output.startswith("https://github.com/"):
            raise GithubError("GitHub CLI returned an unexpected comment URL")
        return output


class InMemoryGithubClient:
    """Test adapter. It never performs network or shell operations."""

    def __init__(self, issues: dict[int, IssueSnapshot], owner: str) -> None:
        self.issues = issues
        self.owner = owner
        self.published: list[tuple[int, dict[str, Any]]] = []

    def doctor(self) -> dict[str, Any]:
        return {
            "available": True,
            "version": "in-memory",
            "authenticated": True,
        }

    def fetch_issue(self, issue_number: int) -> IssueSnapshot:
        issue = self.issues[issue_number]
        if issue.author_login != self.owner:
            raise SafetyError("Issue author is not the allowlisted GitHub owner")
        if issue.state.upper() != "OPEN":
            raise SafetyError("Only open issues can be approved or run")
        return issue

    def publish_report(self, issue_number: int, report: dict[str, Any]) -> str:
        self.published.append((issue_number, json.loads(canonical_json(report))))
        return f"https://github.com/BogdanMaslyuk/PharmDesk/issues/{issue_number}#issuecomment-test"
