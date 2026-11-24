from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


class SafetyError(RuntimeError):
    """Raised when a fail-closed Bridge guard rejects an operation."""


FORBIDDEN_SUFFIXES = {
    ".csv",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".xls",
    ".xlsx",
}
FORBIDDEN_DIRECTORY_NAMES = {
    "backups",
    "data",
    "exports",
    "private_data",
    "reports",
}
ALLOWED_SYNTHETIC_DATA_FILES = {
    "examples/demo_inventory.csv",
    "examples/demo_multi_drawers.csv",
}
SAFE_LIMITATION_CODES = {
    "approval_required",
    "authentication_unavailable",
    "codex_cli_unavailable",
    "isolation_not_attested",
    "network_unavailable",
    "tests_failed",
    "tests_not_run",
    "timeout",
    "unknown_result",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def approval_digest(payload: dict[str, Any]) -> str:
    return sha256_text(canonical_json(payload))


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_reparse_attribute(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def assert_no_reparse_components(path: Path, *, start: Path) -> None:
    raw_path = _absolute(path)
    raw_start = _absolute(start)
    if not _is_relative_to(raw_path, raw_start):
        raise SafetyError(f"Path leaves the registered root: {raw_path}")

    current = raw_start
    if _has_reparse_attribute(current):
        raise SafetyError(f"Registered root is a reparse point: {current}")
    for part in raw_path.relative_to(raw_start).parts:
        current = current / part
        if current.exists() and _has_reparse_attribute(current):
            raise SafetyError(f"Reparse point is not allowed: {current}")


def assert_safe_child(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    raw_path = _absolute(path)
    raw_root = _absolute(root)
    if raw_path == raw_root or not _is_relative_to(raw_path, raw_root):
        raise SafetyError(f"Path is outside the allowed child area: {raw_path}")
    assert_no_reparse_components(raw_path, start=raw_root)
    resolved_root = Path(os.path.realpath(raw_root))
    resolved_path = Path(os.path.realpath(raw_path))
    if resolved_path == resolved_root or not _is_relative_to(resolved_path, resolved_root):
        raise SafetyError(f"Resolved path escaped the allowed root: {resolved_path}")
    if must_exist and not resolved_path.exists():
        raise SafetyError(f"Required path does not exist: {resolved_path}")
    return resolved_path


def assert_not_protected(path: Path, protected_roots: Iterable[Path]) -> None:
    resolved = Path(os.path.realpath(_absolute(path)))
    for protected in protected_roots:
        protected_resolved = Path(os.path.realpath(_absolute(protected)))
        if resolved == protected_resolved or _is_relative_to(resolved, protected_resolved):
            raise SafetyError(f"Protected path rejected: {resolved}")


def inspect_workspace(
    workspace: Path,
    *,
    workspaces_root: Path,
    protected_roots: Iterable[Path],
    max_files: int = 20_000,
    max_file_bytes: int = 25 * 1024 * 1024,
) -> dict[str, int]:
    resolved = assert_safe_child(workspace, workspaces_root, must_exist=True)
    assert_not_protected(resolved, protected_roots)
    if not resolved.is_dir():
        raise SafetyError(f"Workspace is not a directory: {resolved}")

    file_count = 0
    total_bytes = 0
    for current_root, directories, files in os.walk(resolved, followlinks=False):
        current = Path(current_root)
        assert_no_reparse_components(current, start=resolved)
        directories[:] = [
            name
            for name in directories
            if name not in {".git", ".venv", "node_modules", "__pycache__"}
        ]
        for name in directories:
            if name.casefold() in FORBIDDEN_DIRECTORY_NAMES:
                raise SafetyError(f"Private-data directory rejected: {current / name}")
        for name in files:
            candidate = current / name
            if _has_reparse_attribute(candidate):
                raise SafetyError(f"Linked file rejected: {candidate}")
            relative = candidate.relative_to(resolved).as_posix()
            if (
                candidate.suffix.casefold() in FORBIDDEN_SUFFIXES
                and relative not in ALLOWED_SYNTHETIC_DATA_FILES
            ):
                raise SafetyError(f"Private-data file type rejected: {candidate}")
            size = candidate.stat().st_size
            if size > max_file_bytes:
                raise SafetyError(f"Oversized workspace file rejected: {candidate}")
            file_count += 1
            total_bytes += size
            if file_count > max_files:
                raise SafetyError("Workspace file-count limit exceeded")
    return {"files": file_count, "bytes": total_bytes}


def safe_changed_files(paths: Iterable[str]) -> list[str]:
    safe: list[str] = []
    for raw in paths:
        normalized = raw.replace("\\", "/").strip()
        parsed = PurePosixPath(normalized)
        if not normalized or parsed.is_absolute() or ".." in parsed.parts:
            continue
        if parsed.suffix.casefold() in FORBIDDEN_SUFFIXES:
            continue
        if any(part.casefold() in FORBIDDEN_DIRECTORY_NAMES for part in parsed.parts):
            continue
        if re.search(r"(^|/)(\.env|auth\.json|credentials?)(/|$)", normalized, re.I):
            continue
        safe.append(str(parsed))
    return sorted(set(safe))[:50]


def redact_text(
    text: str,
    *,
    home: Path | None = None,
    runtime_root: Path | None = None,
    max_bytes: int = 1_048_576,
) -> str:
    redacted = text
    replacements = []
    if home:
        replacements.append((str(home), "<HOME>"))
    if runtime_root:
        replacements.append((str(runtime_root), "<RUNTIME>"))
    for value, marker in replacements:
        redacted = re.sub(re.escape(value), marker, redacted, flags=re.I)
        redacted = re.sub(re.escape(value.replace("\\", "/")), marker, redacted, flags=re.I)

    secret_patterns = [
        r"(?i)\b(gh[opsu]_[A-Za-z0-9_]{12,})\b",
        r"(?i)\b(sk-[A-Za-z0-9_-]{12,})\b",
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+",
        r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[^\s]+",
    ]
    for pattern in secret_patterns:
        redacted = re.sub(pattern, "<REDACTED>", redacted)

    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes]
        redacted = encoded.decode("utf-8", errors="ignore") + "\n<TRUNCATED>"
    return redacted


def public_report_from_state(state: dict[str, Any]) -> dict[str, Any]:
    tests = state.get("tests_summary") or {}
    test_status = str(tests.get("status") or "unknown")
    if test_status not in {"passed", "failed", "not_run", "unknown"}:
        test_status = "unknown"

    def safe_count(name: str) -> int:
        try:
            value = int(tests.get(name) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, min(value, 10_000_000))

    safe_tests = {
        "status": test_status,
        "passed": safe_count("passed"),
        "failed": safe_count("failed"),
        "skipped": safe_count("skipped"),
    }
    limitations = [
        code
        for code in state.get("limitations", [])
        if code in SAFE_LIMITATION_CODES
    ]
    commit = str(state.get("commit") or "")
    if commit and not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        commit = ""
    source_issue = str(state.get("source_issue") or "")
    if not re.fullmatch(
        r"https://github\.com/BogdanMaslyuk/PharmDesk/issues/[1-9][0-9]*",
        source_issue,
    ):
        raise SafetyError("Unsafe source_issue in local state")
    git_ref = str(state.get("git_ref") or "")
    if git_ref and not re.fullmatch(
        r"bridge/issue-[1-9][0-9]*-[0-9a-f]{10}",
        git_ref,
    ):
        raise SafetyError("Unsafe git_ref in local state")
    return {
        "schema": "pharmdesk-dev-bridge-report/v1",
        "task_id": int(state["task_id"]),
        "source_issue": source_issue,
        "run_id": str(state.get("run_id") or ""),
        "state": str(state["state"]),
        "git_ref": git_ref,
        "commit": commit,
        "changed_files": safe_changed_files(state.get("changed_files", [])),
        "tests_summary": safe_tests,
        "limitations": sorted(set(limitations)),
    }


def report_digest(report: dict[str, Any]) -> str:
    return sha256_text(canonical_json(report))
