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


def _clean_text_block(text: str | None, *, max_chars: int | None = None) -> str | None:
    value = str(text or "").strip()
    if not value:
        return None
    value = re.sub(r"\n{3,}", "\n\n", value)
    if max_chars is not None and max_chars > 0 and len(value) > max_chars:
        value = value[: max_chars - 3].rstrip() + "..."
    return value


def _quoted_csv(values: list[str] | None) -> str | None:
    cleaned = [str(value).strip() for value in values or [] if str(value).strip()]
    if not cleaned:
        return None
    return ", ".join(f"`{value}`" for value in cleaned)


def _verification_check_summary(check: dict | None) -> str | None:
    if not isinstance(check, dict):
        return None
    kind = str(check.get("kind") or "").strip()
    if not kind:
        return None
    if kind == "path_exists":
        path = str(check.get("path") or "").strip()
        return f"- Path `{path}` must exist." if path else None
    if kind == "file_exists_glob":
        pattern = str(check.get("pattern") or "").strip()
        return f"- At least one file matching `{pattern}` must exist." if pattern else None
    if kind == "file_size_gt":
        pattern = str(check.get("pattern") or "").strip()
        threshold = check.get("bytes")
        if pattern and threshold is not None:
            return f"- A file matching `{pattern}` must be larger than `{threshold}` bytes."
        return f"- A downloaded file must be larger than `{threshold}` bytes." if threshold is not None else None
    if kind == "process_exists":
        name = str(check.get("name") or check.get("process_name") or "").strip()
        return f"- Process `{name}` must be running." if name else None
    if kind == "json_marker_valid_installer":
        path = str(check.get("path") or "").strip()
        field = str(check.get("field") or "installer_path").strip()
        keywords = _quoted_csv(check.get("keywords") if isinstance(check.get("keywords"), list) else [])
        suffixes = _quoted_csv(check.get("allowed_suffixes") if isinstance(check.get("allowed_suffixes"), list) else [])
        threshold = check.get("bytes")
        parts = [f"installer marker `{path}` field `{field}`" if path else f"installer marker field `{field}`"]
        if keywords:
            parts.append(f"target keywords {keywords}")
        if suffixes:
            parts.append(f"allowed suffixes {suffixes}")
        if threshold is not None:
            parts.append(f"size > `{threshold}` bytes")
        return "- " + "; ".join(parts) + "."
    if kind == "json_marker_valid_exe":
        path = str(check.get("path") or "").strip()
        field = str(check.get("field") or "installed_exe").strip()
        keywords = _quoted_csv(check.get("keywords") if isinstance(check.get("keywords"), list) else [])
        parts = [f"exe marker `{path}` field `{field}`" if path else f"exe marker field `{field}`"]
        if keywords:
            parts.append(f"target keywords {keywords}")
        return "- " + "; ".join(parts) + "."
    return None


def _verification_contract_text(verification: dict | None) -> str | None:
    if not isinstance(verification, dict):
        return None
    checks = verification.get("checks")
    if not isinstance(checks, list):
        return None
    lines = [line for line in (_verification_check_summary(item) for item in checks) if line]
    if not lines:
        return None
    return "\n".join(lines)


def _verification_result_summary_entry(entry: dict | None) -> str | None:
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("kind") or "").strip()
    passed = bool(entry.get("passed"))
    status = "passed" if passed else "failed"
    if kind in {"json_marker_valid_installer", "json_marker_valid_exe"}:
        candidate = str(entry.get("resolved_path") or entry.get("value") or "").strip()
        keyword_hits = _quoted_csv(entry.get("keyword_hits") if isinstance(entry.get("keyword_hits"), list) else [])
        suffix = str(entry.get("suffix") or "").strip()
        parts = [f"{kind}: {status}"]
        if candidate:
            parts.append(f"candidate `{candidate}`")
        if suffix:
            parts.append(f"suffix `{suffix}`")
        if keyword_hits:
            parts.append(f"keyword hits {keyword_hits}")
        if entry.get("fallback_used"):
            parts.append("fallback candidate used")
        return "- " + "; ".join(parts) + "."
    if kind in {"file_exists_glob", "file_size_gt"}:
        pattern = str(entry.get("pattern") or "").strip()
        matches = entry.get("matches") if isinstance(entry.get("matches"), list) else []
        return f"- {kind}: {status}; pattern `{pattern}`; matches `{len(matches)}`."
    if kind == "path_exists":
        path = str(entry.get("path") or "").strip()
        return f"- path_exists: {status}; path `{path}`."
    if kind == "process_exists":
        name = str(entry.get("name") or "").strip()
        return f"- process_exists: {status}; process `{name}`."
    return f"- {kind or 'unknown'}: {status}."


def _verification_result_text(verification_result: dict | None) -> str | None:
    if not isinstance(verification_result, dict):
        return None
    evidence = verification_result.get("evidence")
    if not isinstance(evidence, list):
        return None
    lines = [line for line in (_verification_result_summary_entry(item) for item in evidence[:5]) if line]
    if not lines:
        return None
    return "\n".join(lines)


def _recent_result_text(recent_result: dict | None) -> str | None:
    if not isinstance(recent_result, dict):
        return None
    parts: list[str] = []
    return_code = recent_result.get("return_code")
    if return_code is not None:
        parts.append(f"return_code={return_code}")
    stderr_tail = _clean_text_block(recent_result.get("stderr_tail"), max_chars=400)
    if stderr_tail:
        parts.append(f"stderr_tail={stderr_tail}")
    error_info = recent_result.get("error_info")
    if error_info:
        parts.append(f"error_info={json.dumps(error_info, ensure_ascii=False)}")
    if not parts:
        return None
    return "\n".join(parts)


def _retry_context_text(sample: dict) -> str | None:
    reasons = [str(item).strip() for item in (sample.get("replan_reasons") or []) if str(item).strip()]
    if not reasons and not bool(sample.get("replan_requested")) and int(sample.get("chunk_attempt") or 0) <= 1:
        return None
    lines: list[str] = []
    if reasons:
        lines.append("Replan Reasons:")
        lines.extend(f"- {reason}" for reason in reasons[:8])
    if int(sample.get("chunk_attempt") or 0) > 1:
        lines.append(f"Retry Attempt: {int(sample.get('chunk_attempt') or 0)}")
    if bool(sample.get("replan_requested")):
        lines.append("This sample comes from a replan/retry attempt.")
    return "\n".join(lines) if lines else None


def _build_train_input_text(sample: dict) -> str | None:
    task = _clean_text_block(sample.get("task"), max_chars=800)
    agent_prompt = _clean_text_block(sample.get("agent_prompt"), max_chars=6000)
    if not task or not agent_prompt:
        return None
    sections = [f"Task:\n{task}"]
    chunk_title = _clean_text_block(sample.get("chunk_title"), max_chars=400)
    if chunk_title:
        sections.append(f"Chunk:\n{chunk_title}")
    sections.append(f"Agent Prompt:\n{agent_prompt}")
    verification_contract_text = _clean_text_block(sample.get("verification_contract_text"), max_chars=2000)
    if verification_contract_text:
        sections.append(f"Verifier Contract:\n{verification_contract_text}")
    state_text = _clean_text_block(sample.get("state_text"), max_chars=2000)
    if state_text:
        sections.append(f"Current Observation:\n{state_text}")
    recent_result_text = _clean_text_block(_recent_result_text(sample.get("recent_result")), max_chars=1000)
    if recent_result_text:
        sections.append(f"Recent Execution Result:\n{recent_result_text}")
    retry_context_text = _clean_text_block(_retry_context_text(sample), max_chars=1000)
    if retry_context_text:
        sections.append(f"Retry Context:\n{retry_context_text}")
    sections.append("Return executable Python only.")
    return "\n\n".join(sections)


def _build_train_sample(sample: dict) -> dict | None:
    target_code = _clean_text_block(sample.get("target_code"))
    input_text = _build_train_input_text(sample)
    if not target_code or not input_text:
        return None
    return {
        "session_id": sample.get("session_id"),
        "task": sample.get("task"),
        "chunk_id": sample.get("chunk_id"),
        "chunk_title": sample.get("chunk_title"),
        "chunk_attempt": sample.get("chunk_attempt"),
        "step_id": sample.get("step_id"),
        "step_index": sample.get("step_index"),
        "before_image_path": sample.get("before_image_path"),
        "after_image_path": sample.get("after_image_path"),
        "input_text": input_text,
        "output_text": target_code,
        "messages": [
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": target_code},
        ],
        "metadata": {
            "agent_run_label": sample.get("agent_run_label"),
            "agent_run_dir": sample.get("agent_run_dir"),
            "outcome": sample.get("outcome"),
            "failure_type": sample.get("failure_type"),
            "failure_text": sample.get("failure_text"),
            "request_kind": sample.get("request_kind"),
            "replan_requested": sample.get("replan_requested"),
            "replan_reasons": sample.get("replan_reasons"),
            "strong_visual_grounding": sample.get("strong_visual_grounding"),
            "reasoning_enabled": sample.get("reasoning_enabled"),
            "verification_contract_text": sample.get("verification_contract_text"),
            "verification_result_text": sample.get("verification_result_text"),
        },
    }


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
    train_samples: list[dict] = []
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

        verification_contract_text = _verification_contract_text(chunk_verification)
        verification_result_text = _verification_result_text(chunk_verification_result)

        sample = {
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
            "verification_contract_text": verification_contract_text,
            "verification_result_text": verification_result_text,
            "agent_run_label": agent_run_label,
            "agent_run_dir": str(run_path),
            "step_id": step_id,
            "step_index": response.get("step_index") if isinstance(response, dict) else None,
            "request_kind": request.get("request_kind") if isinstance(request, dict) else None,
            "replan_requested": request.get("replan_requested") if isinstance(request, dict) else None,
            "replan_reasons": request.get("replan_reasons") if isinstance(request, dict) else None,
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
        samples.append(sample)
        train_sample = _build_train_sample(sample)
        if train_sample is not None:
            train_samples.append(train_sample)

    _append_samples(session_root / "samples.jsonl", samples)
    _append_samples(session_root / "train_samples.jsonl", train_samples)
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
        "train_sample_count": len(train_samples),
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
        "train_sample_count": sum(int(item.get("train_sample_count", 0)) for item in run_manifests),
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
