from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from .models import AgentInvocationResult
from .subprocess_utils import command_to_shell_string, run_command

_RAW_AGENT_STATE_DIR = Path("/tmp/computer_use_raw_python_agent")
_RAW_AGENT_REQUESTS_DIR = _RAW_AGENT_STATE_DIR / "requests"
_RAW_AGENT_RESPONSES_DIR = _RAW_AGENT_STATE_DIR / "responses"


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


def _is_raw_agent_command(agent_command: str) -> bool:
    name = Path(str(agent_command or "")).name
    return name == "computer-use-raw-python-agent"


def _send_raw_agent_daemon_request(payload: dict, *, timeout_s: float) -> dict:
    request_id = uuid.uuid4().hex
    _RAW_AGENT_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    _RAW_AGENT_RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    request_path = _RAW_AGENT_REQUESTS_DIR / f"{request_id}.json"
    response_path = _RAW_AGENT_RESPONSES_DIR / f"{request_id}.json"
    temp_path = _RAW_AGENT_REQUESTS_DIR / f"{request_id}.tmp"
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(request_path)

    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        if response_path.exists():
            try:
                return json.loads(response_path.read_text(encoding="utf-8"))
            finally:
                response_path.unlink(missing_ok=True)
                request_path.unlink(missing_ok=True)
        time.sleep(0.05)
    request_path.unlink(missing_ok=True)
    raise RuntimeError(f"raw agent daemon did not respond within {timeout_s}s")


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
    if _is_raw_agent_command(agent_command):
        started = time.monotonic()
        response = _send_raw_agent_daemon_request(
            {
                "action": "run",
                "prompt": prompt,
                # Reuse the already-loaded daemon defaults. The user is expected
                # to start the daemon separately for external-cli workflows.
                "overrides": {},
            },
            timeout_s=timeout_s,
        )
        duration_s = round(time.monotonic() - started, 3)
        if not response.get("ok", False):
            raise RuntimeError(response.get("error", "agent run failed"))
        summary = response.get("summary") or {}
        run_dir = summary.get("run_dir")
        if not run_dir:
            raise RuntimeError("agent summary did not include run_dir")
        from .models import CommandResult

        return AgentInvocationResult(
            payload=response,
            command_result=CommandResult(
                command=command,
                cwd=cwd,
                returncode=0,
                stdout=json.dumps(response, ensure_ascii=False, indent=2),
                stderr="",
                duration_s=duration_s,
            ),
            run_dir=run_dir,
        )
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
