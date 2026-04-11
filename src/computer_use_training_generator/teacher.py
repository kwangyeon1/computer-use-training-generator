from __future__ import annotations

import json
import re

from .models import TeacherChunkPlanResult, TeacherResult, TeacherTaskChunk
from .subprocess_utils import render_command_template, run_command


_SPLIT_PROMPT_TEMPLATE = """You are preparing executable GUI task chunks for a computer-use agent.

Split the following overall task and teacher answer into a short ordered JSON plan.

Requirements:
- Output strict JSON only.
- Return one object with exactly this top-level shape:
  {{
    "chunks": [
      {{
        "chunk_id": "chunk-001",
        "title": "short title",
        "agent_prompt": "natural-language instruction to send directly to the agent for only this chunk",
        "success_hint": "short success condition",
        "preconditions": ["short prerequisite that should already hold before this chunk"],
        "verification": {{
          "checks": [
            {{"kind": "path_exists", "path": "~/Downloads/example.exe"}},
            {{"kind": "file_exists_glob", "pattern": "~/Downloads/example-*.exe"}},
            {{"kind": "file_size_gt", "pattern": "~/Downloads/example-*.exe", "bytes": 1000000}},
            {{"kind": "process_exists", "name": "KakaoTalk.exe"}}
          ]
        }},
        "max_retries": 1,
        "on_fail": "retry_current_chunk"
      }}
    ]
  }}
- Each chunk must be sequential and focused.
- Each chunk must only ask the agent to do one stage of the task from the current computer state.
- Do not combine the whole procedure into one chunk.
- Do not include future chunks inside the current chunk prompt.
- Keep chunk prompts self-contained enough for the agent, but assume previous chunks have already run.
- Use as many chunks as needed, but keep them compact and stage-focused.
- If the original answer contains official URLs or important warnings, keep them in the relevant chunk prompt.
- Each chunk must include a read-only verification plan.
- Verification must use only the allowed check kinds: `path_exists`, `file_exists_glob`, `file_size_gt`, `process_exists`.
- Do not output raw Python for verification.
- `preconditions` should describe what must already be true before the chunk starts.
- `max_retries` should be a small integer, usually 0, 1, or 2.
- `on_fail` must be either `retry_current_chunk` or `fail_session`.
- Do not add commentary outside JSON.

Overall task:
{task}

Teacher answer:
{teacher_text}
"""


def _run_teacher_command(*, prompt: str, command_template: str, cwd: str | None, timeout_s: float):
    if not command_template.strip():
        raise ValueError("teacher_command_template is required")
    command = render_command_template(command_template, prompt)
    result = run_command(command, cwd=cwd, timeout_s=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(f"teacher command failed with exit code {result.returncode}")
    response_text = result.stdout.strip()
    if not response_text:
        raise RuntimeError("teacher command returned empty stdout")
    return result, response_text


def run_teacher(*, prompt: str, command_template: str, cwd: str | None, timeout_s: float) -> TeacherResult:
    result, response_text = _run_teacher_command(
        prompt=prompt,
        command_template=command_template,
        cwd=cwd,
        timeout_s=timeout_s,
    )
    return TeacherResult(prompt=prompt, response_text=response_text, command_result=result)


def _extract_json_object(text: str) -> dict:
    stripped = text.strip()
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        stripped = stripped[start : end + 1]
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise RuntimeError("teacher split output must be a JSON object")
    return payload


def _normalize_chunks(payload: dict, *, source_task: str, source_text: str) -> list[TeacherTaskChunk]:
    raw_chunks = payload.get("chunks")
    if not isinstance(raw_chunks, list):
        raise RuntimeError("teacher split output did not contain a chunks list")
    normalized: list[TeacherTaskChunk] = []
    for index, item in enumerate(raw_chunks, start=1):
        if not isinstance(item, dict):
            continue
        agent_prompt = str(item.get("agent_prompt", "")).strip()
        if not agent_prompt:
            continue
        chunk_id = str(item.get("chunk_id") or f"chunk-{index:03d}").strip() or f"chunk-{index:03d}"
        title = str(item.get("title") or f"Chunk {index}").strip() or f"Chunk {index}"
        success_hint = str(item.get("success_hint") or "").strip() or None
        preconditions = [str(value).strip() for value in item.get("preconditions", []) if str(value).strip()] if isinstance(item.get("preconditions"), list) else []
        raw_verification = item.get("verification")
        verification = raw_verification if isinstance(raw_verification, dict) else None
        raw_max_retries = item.get("max_retries")
        try:
            max_retries = int(raw_max_retries) if raw_max_retries is not None else (1 if verification else 0)
        except (TypeError, ValueError):
            max_retries = 1 if verification else 0
        max_retries = max(0, min(2, max_retries))
        on_fail = str(item.get("on_fail") or ("retry_current_chunk" if max_retries > 0 else "fail_session")).strip().lower()
        if on_fail not in {"retry_current_chunk", "fail_session"}:
            on_fail = "retry_current_chunk" if max_retries > 0 else "fail_session"
        notes = [str(value).strip() for value in item.get("notes", []) if str(value).strip()] if isinstance(item.get("notes"), list) else []
        normalized.append(
            TeacherTaskChunk(
                chunk_id=chunk_id,
                title=title,
                agent_prompt=agent_prompt,
                success_hint=success_hint,
                preconditions=preconditions,
                verification=verification,
                max_retries=max_retries,
                on_fail=on_fail,
                notes=notes,
            )
        )
    if normalized:
        return normalized
    return [
        TeacherTaskChunk(
            chunk_id="chunk-001",
            title=source_task,
            agent_prompt=source_text,
            success_hint=None,
            preconditions=[],
            verification=None,
            max_retries=0,
            on_fail="fail_session",
            notes=["fallback_single_chunk"],
        )
    ]


def split_teacher_response(
    *,
    task: str,
    teacher_text: str,
    command_template: str,
    cwd: str | None,
    timeout_s: float,
) -> TeacherChunkPlanResult:
    split_prompt = _SPLIT_PROMPT_TEMPLATE.format(
        task=task.strip(),
        teacher_text=teacher_text.strip(),
    )
    result, response_text = _run_teacher_command(
        prompt=split_prompt,
        command_template=command_template,
        cwd=cwd,
        timeout_s=timeout_s,
    )
    try:
        payload = _extract_json_object(response_text)
        chunks = _normalize_chunks(payload, source_task=task, source_text=teacher_text)
    except Exception:
        chunks = [
            TeacherTaskChunk(
                chunk_id="chunk-001",
                title=task,
                agent_prompt=teacher_text,
                success_hint=None,
                preconditions=[],
                verification=None,
                max_retries=0,
                on_fail="fail_session",
                notes=["fallback_due_to_split_parse_failure"],
            )
        ]
    return TeacherChunkPlanResult(
        source_task=task,
        source_text=teacher_text,
        chunks=chunks,
        command_result=result,
    )
