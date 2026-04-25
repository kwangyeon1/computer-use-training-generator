from __future__ import annotations

import json
from pathlib import Path

from computer_use_training_generator.collector import append_run_artifacts


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_append_run_artifacts_prefers_executed_python_code_for_target(tmp_path: Path) -> None:
    session_root = tmp_path / "session"
    (session_root / "images").mkdir(parents=True)
    (session_root / "agent_runs").mkdir(parents=True)
    run_dir = tmp_path / "run"
    (run_dir / "payloads").mkdir(parents=True)
    (run_dir / "responses").mkdir(parents=True)

    _write_json(run_dir / "session.json", {"user_prompt": "task", "policy": {}})
    _write_json(run_dir / "loop-summary.json", {"final_response": {"done": False}})
    _write_json(
        run_dir / "payloads" / "step-000.request.json",
        {
            "request_kind": "task_step",
            "replan_requested": False,
            "strong_visual_grounding": False,
            "reasoning_enabled": False,
            "observation_text": "state",
            "last_execution": {},
        },
    )
    _write_json(
        run_dir / "responses" / "step-000.response.json",
        {
            "step_index": 0,
            "python_code": 'print("raw model code")',
            "raw_text": 'print("raw model code")',
            "notes": [],
        },
    )
    _write_json(
        run_dir / "responses" / "step-000.executor.json",
        {
            "record": {
                "return_code": 0,
                "payload_metadata": {
                    "executed_python_code": 'print("expanded final code")',
                },
            },
            "stdout_tail": "",
            "stderr_tail": "",
            "error_info": None,
        },
    )

    append_run_artifacts(
        session_root=session_root,
        session_id="session-001",
        run_dir=str(run_dir),
        task="task",
        teacher_prompt="teacher prompt",
        teacher_text="teacher text",
        agent_prompt="agent prompt",
        chunk_index=1,
        chunk_count=1,
        chunk_id="chunk-001",
        chunk_title="title",
        chunk_success_hint=None,
        chunk_preconditions=[],
        chunk_verification=None,
        chunk_max_retries=0,
        chunk_on_fail="fail_session",
        chunk_attempt=1,
        chunk_completed=False,
        chunk_verification_result=None,
        include_unexecuted_steps=False,
        agent_run_label="chunk-001",
    )

    lines = (session_root / "samples.jsonl").read_text(encoding="utf-8").splitlines()
    sample = json.loads(lines[0])
    assert sample["target_code"] == 'print("expanded final code")'
    assert sample["agent_raw_text"] == 'print("raw model code")'

    train_lines = (session_root / "train_samples.jsonl").read_text(encoding="utf-8").splitlines()
    train_sample = json.loads(train_lines[0])
    assert train_sample["output_text"] == 'print("expanded final code")'
    assert train_sample["messages"][1]["content"] == 'print("expanded final code")'
    assert "Task:\ntask" in train_sample["input_text"]
    assert "Agent Prompt:\nagent prompt" in train_sample["input_text"]


def test_append_run_artifacts_writes_verification_contract_into_train_sample(tmp_path: Path) -> None:
    session_root = tmp_path / "session"
    (session_root / "images").mkdir(parents=True)
    (session_root / "agent_runs").mkdir(parents=True)
    run_dir = tmp_path / "run"
    (run_dir / "payloads").mkdir(parents=True)
    (run_dir / "responses").mkdir(parents=True)

    _write_json(run_dir / "session.json", {"user_prompt": "task", "policy": {}})
    _write_json(run_dir / "loop-summary.json", {"final_response": {"done": False}})
    _write_json(
        run_dir / "payloads" / "step-000.request.json",
        {
            "request_kind": "task_step",
            "replan_requested": False,
            "strong_visual_grounding": True,
            "reasoning_enabled": False,
            "observation_text": "browser shows official download page",
            "last_execution": {
                "return_code": 1,
                "stderr_tail": "download did not start",
                "error_info": {"kind": "no_download"},
            },
        },
    )
    _write_json(
        run_dir / "responses" / "step-000.response.json",
        {
            "step_index": 0,
            "python_code": 'print("raw model code")',
            "raw_text": 'print("raw model code")',
            "notes": [],
        },
    )
    _write_json(
        run_dir / "responses" / "step-000.executor.json",
        {
            "record": {
                "return_code": 0,
                "payload_metadata": {
                    "executed_python_code": 'print("expanded final code")',
                },
            },
            "stdout_tail": "",
            "stderr_tail": "",
            "error_info": None,
        },
    )

    append_run_artifacts(
        session_root=session_root,
        session_id="session-001",
        run_dir=str(run_dir),
        task="filezilla 설치해줘",
        teacher_prompt="teacher prompt",
        teacher_text="teacher text",
        agent_prompt="use visible UI first",
        chunk_index=1,
        chunk_count=1,
        chunk_id="chunk-001",
        chunk_title="Download installer",
        chunk_success_hint="installer exists",
        chunk_preconditions=["browser already open"],
        chunk_verification={
            "checks": [
                {
                    "kind": "json_marker_valid_installer",
                    "path": "~/Downloads/computer-use-agent-context.json",
                    "field": "installer_path",
                    "keywords": ["filezilla"],
                    "bytes": 1000000,
                    "allowed_suffixes": [".exe", ".msi", ".zip"],
                }
            ]
        },
        chunk_max_retries=1,
        chunk_on_fail="retry_current_chunk",
        chunk_attempt=1,
        chunk_completed=False,
        chunk_verification_result={
            "passed": False,
            "evidence": [
                {
                    "kind": "json_marker_valid_installer",
                    "resolved_path": "C:/Users/test/Downloads/FileZilla_setup.exe",
                    "suffix": ".exe",
                    "keyword_hits": ["filezilla"],
                    "passed": False,
                    "fallback_used": True,
                }
            ]
        },
        include_unexecuted_steps=False,
        agent_run_label="chunk-001",
    )

    sample = json.loads((session_root / "samples.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "target keywords `filezilla`" in sample["verification_contract_text"]
    assert "fallback candidate used" in sample["verification_result_text"]

    train_sample = json.loads((session_root / "train_samples.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "Verifier Contract:" in train_sample["input_text"]
    assert "target keywords `filezilla`" in train_sample["input_text"]
    assert "Recent Execution Result:" in train_sample["input_text"]
    assert train_sample["metadata"]["verification_result_text"].startswith("- json_marker_valid_installer: failed")


def test_append_run_artifacts_includes_retry_context_for_replan_sample(tmp_path: Path) -> None:
    session_root = tmp_path / "session"
    (session_root / "images").mkdir(parents=True)
    (session_root / "agent_runs").mkdir(parents=True)
    run_dir = tmp_path / "run"
    (run_dir / "payloads").mkdir(parents=True)
    (run_dir / "responses").mkdir(parents=True)

    _write_json(run_dir / "session.json", {"user_prompt": "task", "policy": {}})
    _write_json(run_dir / "loop-summary.json", {"final_response": {"done": False}})
    _write_json(
        run_dir / "payloads" / "step-000.request.json",
        {
            "request_kind": "task_step",
            "replan_requested": True,
            "replan_reasons": ["no_visible_download_candidates", "execution_error"],
            "strong_visual_grounding": False,
            "reasoning_enabled": False,
            "observation_text": "search results page showed no useful download candidates",
            "last_execution": {
                "return_code": 1,
                "stderr_tail": "download related fallback found no clickable candidates",
                "error_info": {"kind": "no_clickable_candidates"},
            },
        },
    )
    _write_json(
        run_dir / "responses" / "step-000.response.json",
        {
            "step_index": 0,
            "python_code": 'print("retrying with new search terms")',
            "raw_text": 'print("retrying with new search terms")',
            "notes": ["replan_reasons=no_visible_download_candidates,execution_error"],
        },
    )
    _write_json(
        run_dir / "responses" / "step-000.executor.json",
        {
            "record": {
                "return_code": 1,
                "payload_metadata": {
                    "executed_python_code": 'print("retrying with new search terms")',
                },
            },
            "stdout_tail": "download related fallback found no clickable candidates",
            "stderr_tail": "replan requested",
            "error_info": {"kind": "no_clickable_candidates"},
        },
    )

    append_run_artifacts(
        session_root=session_root,
        session_id="session-002",
        run_dir=str(run_dir),
        task="filezilla 설치해줘",
        teacher_prompt="teacher prompt",
        teacher_text="teacher text",
        agent_prompt="retry prompt",
        chunk_index=1,
        chunk_count=1,
        chunk_id="chunk-001",
        chunk_title="Download installer",
        chunk_success_hint="installer exists",
        chunk_preconditions=[],
        chunk_verification=None,
        chunk_max_retries=1,
        chunk_on_fail="retry_current_chunk",
        chunk_attempt=2,
        chunk_completed=False,
        chunk_verification_result=None,
        include_unexecuted_steps=False,
        agent_run_label="chunk-001",
    )

    sample = json.loads((session_root / "samples.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert sample["replan_reasons"] == ["no_visible_download_candidates", "execution_error"]

    train_sample = json.loads((session_root / "train_samples.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "Retry Context:" in train_sample["input_text"]
    assert "no_visible_download_candidates" in train_sample["input_text"]
    assert "Retry Attempt: 2" in train_sample["input_text"]
    assert train_sample["metadata"]["replan_reasons"] == ["no_visible_download_candidates", "execution_error"]
