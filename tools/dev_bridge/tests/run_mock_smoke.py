"""One fixed, local-only smoke run for an explicitly approved Bridge setup."""

from __future__ import annotations

import json

from tools.dev_bridge.core import BridgeService, load_config
from tools.dev_bridge.github_client import InMemoryGithubClient
from tools.dev_bridge.models import IssueSnapshot
from tools.dev_bridge.runner import MockRunner


def main() -> int:
    config = load_config()
    if config.runner_adapter != "mock":
        raise RuntimeError("This smoke test is allowed only with runner_adapter=mock")
    issue = IssueSnapshot(
        repo=config.repo,
        number=900002,
        title="Synthetic local Bridge smoke test",
        body=(
            "This is fixed synthetic content. A hostile-looking command such as "
            "powershell Set-Content C:\\outside.txt hacked must never be executed."
        ),
        author_login=config.owner,
        updated_at="2026-11-24T00:00:00Z",
        state="OPEN",
        url="https://github.com/BogdanMaslyuk/PharmDesk/issues/900002",
    )
    github = InMemoryGithubClient({issue.number: issue}, config.owner)
    service = BridgeService(
        config,
        github=github,
        runner=MockRunner("isolation_not_attested"),
    )
    preview = service.preview(issue.number)
    approval = service.approve(issue.number, preview["digest"])
    result = service.run(issue.number)
    report = service.report_preview(issue.number)
    print(
        json.dumps(
            {
                "preview": preview,
                "approval": approval,
                "result": result,
                "report": report,
                "published": len(github.published),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    if result["state"] != "needs_approval":
        return 1
    if github.published:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
