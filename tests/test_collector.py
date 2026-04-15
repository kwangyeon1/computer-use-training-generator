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
