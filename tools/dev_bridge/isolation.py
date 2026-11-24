from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from .models import BridgeConfig, utc_now
from .security import SafetyError, redact_text, sha256_file


def codex_config_text(profile: str) -> str:
    return f'''approval_policy = "on-request"
default_permissions = "{profile}"
web_search = "disabled"

[permissions.{profile}]
extends = ":workspace"

[permissions.{profile}.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"

[permissions.{profile}.filesystem.":workspace_roots"]
"." = "write"
"examples/demo_inventory.csv" = "deny"
"examples/demo_multi_drawers.csv" = "deny"

[permissions.{profile}.network]
enabled = false

[windows]
sandbox = "mxc"

[features]
prefer_mxc = true
'''


def ensure_codex_config(config: BridgeConfig) -> Path:
    config.codex_home.mkdir(parents=True, exist_ok=True)
    path = config.codex_home / "config.toml"
    expected = codex_config_text(config.codex_profile)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current != expected:
            raise SafetyError("Refusing to overwrite a different Codex config")
    else:
        path.write_text(expected, encoding="utf-8", newline="\n")
    return path


def probe_isolation(config: BridgeConfig) -> dict[str, object]:
    config_path = ensure_codex_config(config)
    probe_root = config.runtime_root / "isolation-probe"
    workspace = probe_root / "workspace"
    denied_root = probe_root / "outside-denied"
    workspace.mkdir(parents=True, exist_ok=True)
    denied_root.mkdir(parents=True, exist_ok=True)
    outside_file = denied_root / "canary.txt"
    outside_file.write_text("outside-must-remain-private\n", encoding="utf-8")
    inside_file = workspace / "inside.txt"
    inside_b64 = base64.b64encode(str(inside_file).encode("utf-8")).decode("ascii")
    outside_b64 = base64.b64encode(str(outside_file).encode("utf-8")).decode("ascii")
    script = (
        f"$Inside = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{inside_b64}'))\n"
        f"$Outside = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{outside_b64}'))\n"
        "$ErrorActionPreference = 'Stop'\n"
        "[IO.File]::WriteAllText($Inside, 'inside-ok')\n"
        "if ([IO.File]::ReadAllText($Inside) -ne 'inside-ok') { exit 21 }\n"
        "$readBlocked = $false\n"
        "try { [void][IO.File]::ReadAllText($Outside) } catch { $readBlocked = $true }\n"
        "if (-not $readBlocked) { exit 22 }\n"
        "$writeBlocked = $false\n"
        "try { [IO.File]::WriteAllText($Outside, 'isolation-failed') } catch { $writeBlocked = $true }\n"
        "if (-not $writeBlocked) { exit 23 }\n"
        "exit 0\n"
    )
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    environment = dict(__import__("os").environ)
    environment["CODEX_HOME"] = str(config.codex_home)
    command = [
        str(config.codex_command),
        "sandbox",
        "windows",
        "--permission-profile",
        config.codex_profile,
        "--cd",
        str(workspace),
        "--",
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded_script,
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=90,
            check=False,
            shell=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result_payload: dict[str, object] = {
            "result": "failed",
            "reason": type(exc).__name__,
        }
        _write_attestation(config, result_payload)
        return result_payload

    outside_unchanged = outside_file.read_text(encoding="utf-8") == "outside-must-remain-private\n"
    inside_writable = inside_file.is_file() and inside_file.read_text(encoding="utf-8") == "inside-ok"
    outside_read_blocked = result.returncode in {0, 23}
    outside_write_blocked = result.returncode == 0 and outside_unchanged
    passed = (
        result.returncode == 0
        and outside_read_blocked
        and outside_write_blocked
        and inside_writable
    )
    result_payload = {
        "result": "passed" if passed else "failed",
        "exit_code": result.returncode,
        "inside_writable": inside_writable,
        "outside_read_blocked": outside_read_blocked,
        "outside_write_blocked": outside_write_blocked,
        "profile": config.codex_profile,
        "config_sha256": sha256_file(config_path),
        "codex_version": _codex_version(config),
        "checked_at": utc_now(),
        "stdout": redact_text(
            result.stdout,
            home=Path.home(),
            runtime_root=config.runtime_root,
            max_bytes=16_384,
        ),
        "stderr": redact_text(
            result.stderr,
            home=Path.home(),
            runtime_root=config.runtime_root,
            max_bytes=16_384,
        ),
    }
    _write_attestation(config, result_payload)
    return result_payload


def _codex_version(config: BridgeConfig) -> str:
    environment = dict(__import__("os").environ)
    environment["CODEX_HOME"] = str(config.codex_home)
    result = subprocess.run(
        [str(config.codex_command), "--version"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
        shell=False,
        env=environment,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def _write_attestation(config: BridgeConfig, payload: dict[str, object]) -> None:
    config.codex_home.mkdir(parents=True, exist_ok=True)
    path = config.codex_home / "isolation-attestation.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
