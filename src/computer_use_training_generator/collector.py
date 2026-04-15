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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_session_root(*, output_dir: str, task: str, session_id: str | None = None) -> tuple[str, Path]:
    resolved_session_id = session_id or make_session_id(task)
    session_root = Path(output_dir).resolve() / resolved_session_id
    (session_root / "images").mkdir(parents=True, exist_ok=True)
    (session_root / "agent_runs").mkdir(parents=True, exist_ok=True)
    return resolved_session_id, session_root


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


def _resolved_target_code(response: dict | None, executor: dict | None) -> str | None:
    if isinstance(executor, dict):
        record = executor.get("record")
        if isinstance(record, dict):
            payload_metadata = record.get("payload_metadata")
            if isinstance(payload_metadata, dict):
                executed_python_code = payload_metadata.get("executed_python_code")
                if executed_python_code is not None:
                    return str(executed_python_code)
    if isinstance(response, dict):
        python_code = response.get("python_code")
        if python_code is not None:
            return str(python_code)
    return None


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


def _derive_session_outcome_from_runs(run_summaries: list[dict], override: str | None) -> str:
    if override:
        return override
    if not run_summaries:
        return "unknown"
    if any((summary.get("last_execution") or {}).get("return_code") not in (None, 0) for summary in run_summaries):
        return "fail"
    if any(summary.get("stopped_reason") == "empty_generation" for summary in run_summaries):
        return "fail"
    if any((summary.get("final_response") or {}).get("done") is True for summary in run_summaries):
        return "success"
    return "unknown"


def _derive_session_outcome(
    *,
    run_summaries: list[dict],
    chunk_results: list[dict],
    override: str | None,
) -> str:
    if override:
        return override
    if chunk_results:
        if all(bool(item.get("completed")) for item in chunk_results):
            return "success"
        if any(item.get("stopped_reason") == "chunk_verification_failed" for item in chunk_results):
            return "fail"
        if any(bool(item.get("started")) and not bool(item.get("completed")) for item in chunk_results):
            return "fail"
    return _derive_session_outcome_from_runs(run_summaries, None)


def _append_samples(path: Path, samples: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False))
            handle.write("\n")


def append_run_artifacts(
    *,
    session_root: Path,
    session_id: str,
    run_dir: str,
    task: str,
    teacher_prompt: str,
    teacher_text: str,
    agent_prompt: str,
    chunk_index: int | None,
    chunk_count: int | None,
    chunk_id: str | None,
    chunk_title: str | None,
    chunk_success_hint: str | None,
    chunk_preconditions: list[str] | None,
    chunk_verification: dict | None,
    chunk_max_retries: int | None,
    chunk_on_fail: str | None,
    chunk_attempt: int,
    chunk_completed: bool,
    chunk_verification_result: dict | None,
    include_unexecuted_steps: bool,
    agent_run_label: str,
) -> dict:
    run_path = Path(run_dir).resolve()
    images_dir = session_root / "images"
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
                filename=f"{agent_run_label}.{step_id}.before.png",
            )
        if executor:
            after_path, after_sha = _save_image(
                executor.get("screenshot_base64"),
                images_dir=images_dir,
                filename=f"{agent_run_label}.{step_id}.after.png",
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

        samples.append(
            {
                "session_id": session_id,
                "task": task,
                "teacher_prompt": teacher_prompt,
                "teacher_text": teacher_text,
                "agent_prompt": agent_prompt,
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
                "chunk_id": chunk_id,
                "chunk_title": chunk_title,
                "chunk_success_hint": chunk_success_hint,
                "chunk_preconditions": chunk_preconditions,
                "chunk_verification": chunk_verification,
                "chunk_max_retries": chunk_max_retries,
                "chunk_on_fail": chunk_on_fail,
                "chunk_attempt": chunk_attempt,
                "chunk_completed": chunk_completed,
                "chunk_verification_result": chunk_verification_result,
                "agent_run_label": agent_run_label,
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
                "target_code": _resolved_target_code(response, executor),
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
        )

    _append_samples(session_root / "samples.jsonl", samples)
    run_manifest = {
        "agent_run_label": agent_run_label,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "chunk_id": chunk_id,
        "chunk_title": chunk_title,
        "chunk_success_hint": chunk_success_hint,
        "chunk_preconditions": chunk_preconditions,
        "chunk_verification": chunk_verification,
        "chunk_max_retries": chunk_max_retries,
        "chunk_on_fail": chunk_on_fail,
        "chunk_attempt": chunk_attempt,
        "chunk_completed": chunk_completed,
        "chunk_verification_result": chunk_verification_result,
        "source_run_dir": str(run_path),
        "agent_user_prompt": session_payload.get("user_prompt"),
        "policy": session_payload.get("policy"),
        "sample_count": len(samples),
        "loop_summary": loop_summary,
    }
    _write_json(session_root / "agent_runs" / f"{agent_run_label}.json", run_manifest)
    return run_manifest


def write_teacher_bundle(
    *,
    session_root: Path,
    teacher_prompt: str,
    teacher_text: str,
    teacher_result: TeacherResult | None,
    teacher_plan_payload: dict | None = None,
) -> None:
    teacher_payload = {
        "prompt": teacher_prompt,
        "response_text": teacher_text,
        "command_result": asdict(teacher_result.command_result) if teacher_result else None,
    }
    _write_json(session_root / "teacher.json", teacher_payload)
    if teacher_plan_payload is not None:
        _write_json(session_root / "teacher_plan.json", teacher_plan_payload)


def write_agent_invocation_payload(
    *,
    session_root: Path,
    name: str,
    invocation: AgentInvocationResult | None,
) -> None:
    if invocation is None:
        return
    _write_json(
        session_root / name,
        {
            "payload": invocation.payload,
            "command_result": asdict(invocation.command_result),
        },
    )


def write_session_manifest(
    *,
    session_root: Path,
    session_id: str,
    task: str,
    teacher_prompt: str,
    teacher_text: str,
    teacher_chunks: list[dict],
    chunk_results: list[dict],
    run_manifests: list[dict],
    session_outcome: str | None,
    session_note: str | None,
    stopped_reason: str | None,
) -> dict:
    manifest = {
        "session_id": session_id,
        "task": task,
        "teacher_prompt": teacher_prompt,
        "teacher_text": teacher_text,
        "teacher_chunks": teacher_chunks,
        "chunk_results": chunk_results,
        "source_run_dirs": [item.get("source_run_dir") for item in run_manifests],
        "sample_count": sum(int(item.get("sample_count", 0)) for item in run_manifests),
        "run_count": len(run_manifests),
        "expected_chunk_count": len(teacher_chunks),
        "completed_chunk_count": sum(1 for item in chunk_results if bool(item.get("completed"))),
        "stopped_reason": stopped_reason,
        "session_outcome": _derive_session_outcome(
            run_summaries=[item.get("loop_summary") or {} for item in run_manifests],
            chunk_results=chunk_results,
            override=session_outcome,
        ),
        "session_note": session_note,
        "agent_runs": run_manifests,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(session_root / "session.json", manifest)
    return manifest


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
    session_id, session_root = prepare_session_root(output_dir=output_dir, task=task)
    write_teacher_bundle(
        session_root=session_root,
        teacher_prompt=teacher_prompt,
        teacher_text=teacher_text,
        teacher_result=teacher_result,
        teacher_plan_payload=None,
    )
    write_agent_invocation_payload(
        session_root=session_root,
        name="agent_bootstrap.json",
        invocation=bootstrap_result,
    )
    write_agent_invocation_payload(
        session_root=session_root,
        name="agent_prompt.json",
        invocation=prompt_result,
    )
    run_manifest = append_run_artifacts(
        session_root=session_root,
        session_id=session_id,
        run_dir=run_dir,
        task=task,
        teacher_prompt=teacher_prompt,
        teacher_text=teacher_text,
        agent_prompt=teacher_text,
        chunk_index=0,
        chunk_count=1,
        chunk_id="chunk-001",
        chunk_title=task,
        chunk_success_hint=None,
        chunk_preconditions=[],
        chunk_verification=None,
        chunk_max_retries=0,
        chunk_on_fail="fail_session",
        chunk_attempt=1,
        chunk_completed=False,
        chunk_verification_result=None,
        include_unexecuted_steps=include_unexecuted_steps,
        agent_run_label="chunk-001",
    )
    legacy_loop_summary = run_manifest.get("loop_summary") or {}
    legacy_final_response = legacy_loop_summary.get("final_response") or {}
    legacy_completed = bool(legacy_final_response.get("done")) or str(legacy_loop_summary.get("stopped_reason") or "") == "task_completed"
    return write_session_manifest(
        session_root=session_root,
        session_id=session_id,
        task=task,
        teacher_prompt=teacher_prompt,
        teacher_text=teacher_text,
        teacher_chunks=[
            {
                "chunk_id": "chunk-001",
                "title": task,
                "agent_prompt": teacher_text,
                "success_hint": None,
                "preconditions": [],
                "verification": None,
                "max_retries": 0,
                "on_fail": "fail_session",
                "notes": ["single_chunk_legacy_flow"],
            }
        ],
        chunk_results=[
            {
                "chunk_id": "chunk-001",
                "title": task,
                "completed": legacy_completed,
                "started": True,
                "attempts_used": 1,
                "max_retries": 0,
                "on_fail": "fail_session",
                "verification_result": None,
                "stopped_reason": None if legacy_completed else legacy_loop_summary.get("stopped_reason"),
            }
        ],
        run_manifests=[run_manifest],
        session_outcome=session_outcome,
        session_note=session_note,
        stopped_reason=None,
    )
