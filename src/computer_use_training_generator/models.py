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
    preconditions: list[str] = field(default_factory=list)
    verification: dict[str, object] | None = None
    max_retries: int = 0
    on_fail: str = "fail_session"
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


@dataclass(slots=True)
class ChunkVerificationResult:
    verification: dict[str, object] | None
    verification_code: str | None
    passed: bool
    return_code: int | None
    evidence: list[dict[str, object]] = field(default_factory=list)
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    error: str | None = None
    executor_payload: dict[str, object] = field(default_factory=dict)
