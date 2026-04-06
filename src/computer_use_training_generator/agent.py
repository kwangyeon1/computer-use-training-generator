from __future__ import annotations

import json

from .models import AgentInvocationResult
from .subprocess_utils import run_command


def _parse_agent_json(stdout: str) -> dict:
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("agent stdout did not contain a JSON object")
    return payload


def _base_agent_command(
    *,
    agent_command: str,
    endpoint: str | None,
    config_path: str | None,
    reasoning_enabled: bool,
) -> list[str]:
    command = [agent_command]
    if config_path:
        command.extend(["--config", config_path])
    if endpoint:
        command.extend(["--endpoint", endpoint])
    if reasoning_enabled:
        command.append("--reasoning-enabled")
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
    )
    command.extend(["--model-id", model_id])
    result = run_command(command, cwd=cwd, timeout_s=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(f"agent bootstrap failed with exit code {result.returncode}")
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
    )
    command.extend(["--prompt", prompt])
    result = run_command(command, cwd=cwd, timeout_s=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(f"agent prompt failed with exit code {result.returncode}")
    payload = _parse_agent_json(result.stdout)
    if not payload.get("ok", False):
        raise RuntimeError("agent prompt returned ok=false")
    summary = payload.get("summary") or {}
    run_dir = summary.get("run_dir")
    if not run_dir:
        raise RuntimeError("agent summary did not include run_dir")
    return AgentInvocationResult(payload=payload, command_result=result, run_dir=run_dir)
