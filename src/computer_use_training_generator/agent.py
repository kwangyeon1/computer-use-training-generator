from __future__ import annotations

import json

from .models import AgentInvocationResult
from .subprocess_utils import command_to_shell_string, run_command


def _parse_agent_json(stdout: str) -> dict:
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("agent stdout did not contain a JSON object")
    return payload


def _format_command_failure(stage: str, result) -> str:
    stdout_tail = result.stdout.strip()[-2000:]
    stderr_tail = result.stderr.strip()[-2000:]
    details = [
        f"{stage} failed with exit code {result.returncode}",
        f"command: {command_to_shell_string(result.command)}",
        f"cwd: {result.cwd or ''}",
    ]
    if stdout_tail:
        details.append(f"stdout_tail:\n{stdout_tail}")
    if stderr_tail:
        details.append(f"stderr_tail:\n{stderr_tail}")
    return "\n".join(details)


def _base_agent_command(
    *,
    agent_command: str,
    endpoint: str | None,
    config_path: str | None,
    reasoning_enabled: bool,
    request_timeout_s: float | None = None,
) -> list[str]:
    command = [agent_command]
    if config_path:
        command.extend(["--config", config_path])
    if endpoint:
        command.extend(["--endpoint", endpoint])
    if reasoning_enabled:
        command.append("--reasoning-enabled")
    if request_timeout_s is not None:
        timeout_value = str(float(request_timeout_s))
        command.extend(["--load-request-timeout-s", timeout_value])
        command.extend(["--run-request-timeout-s", timeout_value])
    return command


def bootstrap_agent(
    *,
    agent_command: str,
    model_id: str,
    endpoint: str | None,
    config_path: str | None,
    reasoning_enabled: bool,
    cwd: str | None,
    timeout_s: float,
) -> AgentInvocationResult:
    command = _base_agent_command(
        agent_command=agent_command,
        endpoint=endpoint,
        config_path=config_path,
        reasoning_enabled=reasoning_enabled,
        request_timeout_s=timeout_s,
    )
    command.extend(["--model-id", model_id])
    result = run_command(command, cwd=cwd, timeout_s=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(_format_command_failure("agent bootstrap", result))
    payload = _parse_agent_json(result.stdout)
    if not payload.get("ok", False):
        raise RuntimeError("agent bootstrap returned ok=false")
    return AgentInvocationResult(payload=payload, command_result=result, run_dir=None)


def run_agent_prompt(
    *,
    agent_command: str,
    prompt: str,
    endpoint: str | None,
    config_path: str | None,
    reasoning_enabled: bool,
    cwd: str | None,
    timeout_s: float,
) -> AgentInvocationResult:
    command = _base_agent_command(
        agent_command=agent_command,
        endpoint=endpoint,
        config_path=config_path,
        reasoning_enabled=reasoning_enabled,
        request_timeout_s=timeout_s,
    )
    command.extend(["--prompt", prompt])
    result = run_command(command, cwd=cwd, timeout_s=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(_format_command_failure("agent prompt", result))
    payload = _parse_agent_json(result.stdout)
    if not payload.get("ok", False):
        raise RuntimeError("agent prompt returned ok=false")
    summary = payload.get("summary") or {}
    run_dir = summary.get("run_dir")
    if not run_dir:
        raise RuntimeError("agent summary did not include run_dir")
    return AgentInvocationResult(payload=payload, command_result=result, run_dir=run_dir)
