from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .core import BridgeService, load_config, render_json
from .github_client import GithubError
from .isolation import probe_isolation
from .security import SafetyError


def _digest(value: str) -> str:
    normalized = value.casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise argparse.ArgumentTypeError("digest must be a 64-character SHA-256")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pharmdesk-bridge",
        description="Fail-closed local dispatcher for approved GitHub tasks.",
    )
    parser.add_argument("--config", type=Path, help="Absolute path to config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Read-only diagnostics")

    preview = subparsers.add_parser("preview", help="Fetch and hash an issue without running it")
    preview.add_argument("issue", type=int)

    approve = subparsers.add_parser("approve", help="Approve one exact issue digest")
    approve.add_argument("issue", type=int)
    approve.add_argument("digest", type=_digest)

    run = subparsers.add_parser("run", help="Consume an approval and run once")
    run.add_argument("issue", type=int)

    status = subparsers.add_parser("status", help="Show local structured state")
    status.add_argument("issue", type=int, nargs="?")

    subparsers.add_parser("stop", help="Request graceful stop of the active run")
    subparsers.add_parser("recover", help="Recover only a provably stale lock")

    report_preview = subparsers.add_parser(
        "report-preview",
        help="Build the deterministic public report without publishing it",
    )
    report_preview.add_argument("issue", type=int)

    publish = subparsers.add_parser(
        "publish-report",
        help="Publish one exact safe report after separate digest approval",
    )
    publish.add_argument("issue", type=int)
    publish.add_argument("digest", type=_digest)

    subparsers.add_parser(
        "probe-isolation",
        help="Run a fixed local filesystem sandbox probe; never invokes a model",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        service = BridgeService(config)
        if arguments.command == "doctor":
            result = service.doctor()
        elif arguments.command == "preview":
            result = service.preview(arguments.issue)
        elif arguments.command == "approve":
            result = service.approve(arguments.issue, arguments.digest)
        elif arguments.command == "run":
            result = service.run(arguments.issue)
        elif arguments.command == "status":
            result = service.status(arguments.issue)
        elif arguments.command == "stop":
            result = service.stop()
        elif arguments.command == "recover":
            result = service.recover()
        elif arguments.command == "report-preview":
            result = service.report_preview(arguments.issue)
        elif arguments.command == "publish-report":
            result = service.publish_report(arguments.issue, arguments.digest)
        elif arguments.command == "probe-isolation":
            result = probe_isolation(config)
        else:  # pragma: no cover - argparse prevents this branch
            parser.error(f"Unknown command: {arguments.command}")
            return 2
    except (SafetyError, GithubError, ValueError) as exc:
        print(render_json({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(render_json(result))
    if arguments.command == "doctor" and not result.get("ready", False):
        return 1
    if arguments.command == "probe-isolation" and result.get("result") != "passed":
        return 1
    if arguments.command == "run":
        state = result.get("state")
        if state == "needs_approval":
            return 4
        if state == "cancelled":
            return 130
        if state != "completed":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
