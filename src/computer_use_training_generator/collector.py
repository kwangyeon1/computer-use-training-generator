from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import AgentInvocationResult, TeacherResult


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _slugify(text: str, *, max_length: int = 80) -> str:
    slug = re.sub(r"[^\w\-가-힣]+", "-", text.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        slug = "session"
    return slug[:max_length]


def make_session_id(task: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_slugify(task)}"


def _step_sort_key(step_id: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", step_id)
    key: list[Any] = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part)
    return tuple(key)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _last_nonempty_line(text: str | None) -> str | None:
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[-1]


def _summarize_failure(executor_payload: dict) -> tuple[str | None, str | None]:
    error_info = executor_payload.get("error_info")
    failure_type = None
    if isinstance(error_info, dict):
        failure_type = error_info.get("kind")
    stderr_tail = executor_payload.get("stderr_tail")
    if failure_type:
        return failure_type, _last_nonempty_line(stderr_tail) or failure_type
    if stderr_tail:
        return None, _last_nonempty_line(stderr_tail)
    return None, None


def _save_image(base64_data: str | None, *, images_dir: Path, filename: str) -> tuple[str | None, str | None]:
    if not base64_data:
        return None, None
    raw = base64.b64decode(base64_data)
    image_path = images_dir / filename
    image_path.write_bytes(raw)
    return str(image_path.relative_to(images_dir.parent)), hashlib.sha256(raw).hexdigest()


def _discover_step_maps(run_dir: Path) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    payload_dir = run_dir / "payloads"
    response_dir = run_dir / "responses"

    request_map: dict[str, dict] = {}
    response_map: dict[str, dict] = {}
    executor_map: dict[str, dict] = {}

    for path in sorted(payload_dir.glob("*.request.json")):
        request_map[path.name.removesuffix(".request.json")] = _load_json(path)
    for path in sorted(response_dir.glob("*.response.json")):
        response_map[path.name.removesuffix(".response.json")] = _load_json(path)
    for path in sorted(response_dir.glob("*.executor.json")):
        executor_map[path.name.removesuffix(".executor.json")] = _load_json(path)
    return request_map, response_map, executor_map


def _derive_session_outcome(loop_summary: dict, override: str | None) -> str:
    if override:
        return override
    final_response = loop_summary.get("final_response") or {}
    if final_response.get("done") is True:
        return "success"
    last_execution = loop_summary.get("last_execution") or {}
    if last_execution.get("return_code") not in (None, 0):
        return "fail"
    stopped_reason = loop_summary.get("stopped_reason")
    if stopped_reason in {"empty_generation"}:
        return "fail"
    return "unknown"


def collect_run_artifacts(
    *,
    run_dir: str,
    output_dir: str,
    task: str,
    teacher_prompt: str,
    teacher_text: str,
    teacher_result: TeacherResult | None,
    bootstrap_result: AgentInvocationResult | None,
    prompt_result: AgentInvocationResult | None,
    session_outcome: str | None,
    session_note: str | None,
    include_unexecuted_steps: bool,
) -> dict:
    run_path = Path(run_dir).resolve()
    session_id = make_session_id(task)
    session_root = Path(output_dir).resolve() / session_id
    images_dir = session_root / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    session_payload = _load_json(run_path / "session.json")
    loop_summary = _load_json(run_path / "loop-summary.json")
    request_map, response_map, executor_map = _discover_step_maps(run_path)

    step_ids = sorted(set(request_map) | set(response_map) | set(executor_map), key=_step_sort_key)
    samples: list[dict] = []
    for step_id in step_ids:
        request = request_map.get(step_id)
        response = response_map.get(step_id)
        executor = executor_map.get(step_id)
        if not include_unexecuted_steps and executor is None:
            continue

        before_path = None
        before_sha = None
        after_path = None
        after_sha = None
        if request:
            before_path, before_sha = _save_image(
                request.get("screenshot_base64"),
                images_dir=images_dir,
                filename=f"{step_id}.before.png",
            )
        if executor:
            after_path, after_sha = _save_image(
                executor.get("screenshot_base64"),
                images_dir=images_dir,
                filename=f"{step_id}.after.png",
            )

        record = executor.get("record") if isinstance(executor, dict) else None
        last_execution = request.get("last_execution") if isinstance(request, dict) else None
        failure_type, failure_text = _summarize_failure(executor or {})
        return_code = record.get("return_code") if isinstance(record, dict) else None
        if return_code == 0:
            outcome = "success"
        elif return_code is None:
            outcome = "unknown"
        else:
            outcome = "fail"

        sample = {
            "session_id": session_id,
            "task": task,
            "teacher_prompt": teacher_prompt,
            "teacher_text": teacher_text,
            "agent_prompt": teacher_text,
            "agent_run_dir": str(run_path),
            "step_id": step_id,
            "step_index": response.get("step_index") if isinstance(response, dict) else None,
            "request_kind": request.get("request_kind") if isinstance(request, dict) else None,
            "replan_requested": request.get("replan_requested") if isinstance(request, dict) else None,
            "strong_visual_grounding": request.get("strong_visual_grounding") if isinstance(request, dict) else None,
            "reasoning_enabled": request.get("reasoning_enabled") if isinstance(request, dict) else None,
            "before_image_path": before_path,
            "before_image_sha256": before_sha,
            "after_image_path": after_path,
            "after_image_sha256": after_sha,
            "state_text": request.get("observation_text") if isinstance(request, dict) else None,
            "recent_result": {
                "return_code": last_execution.get("return_code") if isinstance(last_execution, dict) else None,
                "stderr_tail": last_execution.get("stderr_tail") if isinstance(last_execution, dict) else None,
                "error_info": last_execution.get("error_info") if isinstance(last_execution, dict) else None,
            },
            "target_code": response.get("python_code") if isinstance(response, dict) else None,
            "agent_raw_text": response.get("raw_text") if isinstance(response, dict) else None,
            "agent_notes": response.get("notes") if isinstance(response, dict) else None,
            "executor_stdout_tail": executor.get("stdout_tail") if isinstance(executor, dict) else None,
            "executor_stderr_tail": executor.get("stderr_tail") if isinstance(executor, dict) else None,
            "executor_error_info": executor.get("error_info") if isinstance(executor, dict) else None,
            "return_code": return_code,
            "outcome": outcome,
            "failure_type": failure_type,
            "failure_text": failure_text,
        }
        samples.append(sample)

    samples_path = session_root / "samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False))
            handle.write("\n")

    teacher_payload = {
        "prompt": teacher_prompt,
        "response_text": teacher_text,
        "command_result": asdict(teacher_result.command_result) if teacher_result else None,
    }
    _write_json(session_root / "teacher.json", teacher_payload)

    if bootstrap_result:
        _write_json(
            session_root / "agent_bootstrap.json",
            {
                "payload": bootstrap_result.payload,
                "command_result": asdict(bootstrap_result.command_result),
            },
        )
    if prompt_result:
        _write_json(
            session_root / "agent_prompt.json",
            {
                "payload": prompt_result.payload,
                "command_result": asdict(prompt_result.command_result),
            },
        )

    manifest = {
        "session_id": session_id,
        "task": task,
        "teacher_prompt": teacher_prompt,
        "teacher_text": teacher_text,
        "source_run_dir": str(run_path),
        "agent_user_prompt": session_payload.get("user_prompt"),
        "policy": session_payload.get("policy"),
        "sample_count": len(samples),
        "session_outcome": _derive_session_outcome(loop_summary, session_outcome),
        "session_note": session_note,
        "loop_summary": loop_summary,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(session_root / "session.json", manifest)
    return manifest
