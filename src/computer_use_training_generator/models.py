from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    cwd: str | None
    returncode: int
    stdout: str
    stderr: str
    duration_s: float


@dataclass(slots=True)
class TeacherResult:
    prompt: str
    response_text: str
    command_result: CommandResult


@dataclass(slots=True)
class AgentInvocationResult:
    payload: dict
    command_result: CommandResult
    run_dir: str | None
