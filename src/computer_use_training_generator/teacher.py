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
        "success_hint": "short success condition"
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
        notes = [str(value).strip() for value in item.get("notes", []) if str(value).strip()] if isinstance(item.get("notes"), list) else []
        normalized.append(
            TeacherTaskChunk(
                chunk_id=chunk_id,
                title=title,
                agent_prompt=agent_prompt,
                success_hint=success_hint,
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
                notes=["fallback_due_to_split_parse_failure"],
            )
        ]
    return TeacherChunkPlanResult(
        source_task=task,
        source_text=teacher_text,
        chunks=chunks,
        command_result=result,
    )
