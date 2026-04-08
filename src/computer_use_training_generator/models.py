from __future__ import annotations

from dataclasses import dataclass, field


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
class TeacherTaskChunk:
    chunk_id: str
    title: str
    agent_prompt: str
    success_hint: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TeacherChunkPlanResult:
    source_task: str
    source_text: str
    chunks: list[TeacherTaskChunk]
    command_result: CommandResult


@dataclass(slots=True)
class AgentInvocationResult:
    payload: dict
    command_result: CommandResult
    run_dir: str | None
