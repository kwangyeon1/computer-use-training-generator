from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .agent import bootstrap_agent, run_agent_prompt
from .collector import (
    append_run_artifacts,
    collect_run_artifacts,
    prepare_session_root,
    write_agent_invocation_payload,
    write_session_manifest,
    write_teacher_bundle,
)
from .config_utils import load_generator_config
from .models import TeacherChunkPlanResult, TeacherTaskChunk
from .teacher import build_local_teacher_fallback, run_teacher, split_teacher_response
from .verification import run_chunk_verification, write_verification_artifact


def _find_existing_path(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.exists():
            return resolved
    return None


def _default_config_path() -> Path:
    candidates: list[Path] = []
    cwd = Path.cwd()
    candidates.append(cwd / "config" / "generator.default.json")

    argv0 = Path(sys.argv[0]).resolve()
    for parent in argv0.parents:
        candidates.append(parent / "config" / "generator.default.json")

    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        candidates.append(parent / "config" / "generator.default.json")

    resolved = _find_existing_path(candidates)
    if resolved is not None:
        return resolved
    return (cwd / "config" / "generator.default.json").resolve()


def _load_config(path: str | None) -> tuple[dict, Path | None]:
    resolved = path or str(_default_config_path())
    return load_generator_config(resolved)


def _override(config: dict, key: str, value):
    if value is None:
        return
    config[key] = value


def _session_outcome_arg(value: str) -> str:
    normalized = value.lower()
    if normalized not in {"success", "fail", "unknown"}:
        raise argparse.ArgumentTypeError("session outcome must be one of: success, fail, unknown")
    return normalized


def _build_effective_config(args: argparse.Namespace) -> tuple[dict, Path | None]:
    config, config_path = _load_config(args.config)
    _override(config, "teacher_command_template", getattr(args, "teacher_command_template", None))
    _override(config, "teacher_timeout_s", getattr(args, "teacher_timeout_s", None))
    _override(config, "teacher_workdir", getattr(args, "teacher_workdir", None))
    _override(config, "teacher_split_timeout_s", getattr(args, "teacher_split_timeout_s", None))
    _override(config, "agent_command", getattr(args, "agent_command", None))
    _override(config, "agent_model_id", getattr(args, "agent_model_id", None))
    _override(config, "agent_config_path", getattr(args, "agent_config_path", None))
    _override(config, "agent_endpoint", getattr(args, "agent_endpoint", None))
    _override(config, "agent_workdir", getattr(args, "agent_workdir", None))
    _override(config, "agent_bootstrap_timeout_s", getattr(args, "agent_bootstrap_timeout_s", None))
    _override(config, "agent_prompt_timeout_s", getattr(args, "agent_prompt_timeout_s", None))
    _override(config, "chunk_verification_timeout_s", getattr(args, "chunk_verification_timeout_s", None))
    if getattr(args, "teacher_split_enabled", False):
        config["teacher_split_enabled"] = True
    if getattr(args, "agent_reasoning_enabled", False):
        config["agent_reasoning_enabled"] = True
    if getattr(args, "chunk_verification_enabled", False):
        config["chunk_verification_enabled"] = True
    if getattr(args, "output_dir", None) is not None:
        config["output_dir"] = str(Path(args.output_dir).resolve())
    return config, config_path


def _serialize_chunk(chunk: TeacherTaskChunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "title": chunk.title,
        "agent_prompt": chunk.agent_prompt,
        "success_hint": chunk.success_hint,
        "preconditions": list(chunk.preconditions),
        "verification": chunk.verification,
        "max_retries": chunk.max_retries,
        "on_fail": chunk.on_fail,
        "notes": list(chunk.notes),
    }


def _compose_chunk_prompt(chunk: TeacherTaskChunk) -> str:
    parts = [
        "Return executable Python only for this chunk. Do not ask a human to perform manual GUI actions outside the generated Python.",
        chunk.agent_prompt.strip(),
    ]
    if chunk.success_hint:
        parts.append(f"Current chunk success target: {chunk.success_hint}")
    if chunk.preconditions:
        parts.append("Preconditions expected before or during this chunk:\n- " + "\n- ".join(chunk.preconditions))
    parts.append("Do only this chunk. Do not skip ahead to later chunks.")
    return "\n\n".join(part for part in parts if part)


def _compose_retry_prompt(*, chunk: TeacherTaskChunk, verification_result: dict, attempt_index: int) -> str:
    evidence = verification_result.get("evidence")
    error = verification_result.get("error")
    evidence_text = json.dumps(evidence, ensure_ascii=False, indent=2) if evidence else "[]"
    retry_header = f"Previous attempt {attempt_index} did not satisfy the chunk verifier. Retry only this chunk."
    details = [retry_header]
    if error:
        details.append(f"Verifier error: {error}")
    details.append(f"Verifier evidence:\n{evidence_text}")
    return _compose_chunk_prompt(chunk) + "\n\n" + "\n\n".join(details)


def _chunk_completed_from_agent_payload(payload: dict) -> bool:
    summary = payload.get("summary") or {}
    final_response = summary.get("final_response") or {}
    return bool(final_response.get("done")) or str(summary.get("stopped_reason") or "") == "task_completed"


def _compose_teacher_prompt(*, task: str, config: dict) -> str:
    context = str(config.get("teacher_task_context") or "").strip()
    if not context:
        return task
    return f"{context}\n\nActual task for the target machine:\n{task.strip()}"


def _request_terminal_attention(message: str) -> None:
    stream = sys.stderr if sys.stderr.isatty() else None
    owned_stream = None
    if stream is None:
        tty_path = "CONOUT$" if os.name == "nt" else "/dev/tty"
        try:
            owned_stream = open(tty_path, "w", encoding="utf-8", buffering=1)
            stream = owned_stream
        except OSError:
            return
    try:
        title = f"training-generator finished: {message}"
        # Set a distinctive title first so external window-raise helpers can find it.
        stream.write(f"\x1b]0;{title}\x07")
        # Best-effort terminal attention request for interactive shells:
        # de-iconify, raise window, then ring the bell.
        stream.write("\n\x1b[1t\x1b[5t\a")
        stream.write(f"[training-generator] {message}\n")
        stream.flush()
        _raise_terminal_window(title)
    finally:
        if owned_stream is not None:
            owned_stream.close()


def _raise_terminal_window(title: str) -> None:
    if _raise_terminal_window_wsl(title):
        return
    if _raise_terminal_window_x11(title):
        return


def _raise_terminal_window_x11(title: str) -> bool:
    if not os.environ.get("DISPLAY"):
        return False
    xdotool = shutil.which("xdotool")
    if xdotool:
        try:
            subprocess.run(
                [xdotool, "search", "--name", title, "windowraise", "--sync"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            pass
    wmctrl = shutil.which("wmctrl")
    if wmctrl:
        try:
            subprocess.run(
                [wmctrl, "-a", title],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            pass
    return False


def _raise_terminal_window_wsl(title: str) -> bool:
    if not os.environ.get("WSL_DISTRO_NAME"):
        return False
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return False
    script = rf"""
$title = {title!r}
$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class Win32Raise {{
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
}}
'@
Add-Type -TypeDefinition $sig -ErrorAction SilentlyContinue | Out-Null
$HWND_TOPMOST = [IntPtr](-1)
$HWND_NOTOPMOST = [IntPtr](-2)
$SWP_NOMOVE = 0x0002
$SWP_NOSIZE = 0x0001
$SWP_SHOWWINDOW = 0x0040
$SWP_NOACTIVATE = 0x0010
$flags = $SWP_NOMOVE -bor $SWP_NOSIZE -bor $SWP_SHOWWINDOW -bor $SWP_NOACTIVATE
[Win32Raise]::EnumWindows({{
  param($hWnd, $lParam)
  if (-not [Win32Raise]::IsWindowVisible($hWnd)) {{ return $true }}
  $sb = New-Object System.Text.StringBuilder 512
  [void][Win32Raise]::GetWindowText($hWnd, $sb, $sb.Capacity)
  $text = $sb.ToString()
  if ($text -and $text.Contains($title)) {{
    [void][Win32Raise]::ShowWindowAsync($hWnd, 9)
    [void][Win32Raise]::SetWindowPos($hWnd, $HWND_TOPMOST, 0, 0, 0, 0, $flags)
    [void][Win32Raise]::SetWindowPos($hWnd, $HWND_NOTOPMOST, 0, 0, 0, 0, $flags)
    return $false
  }}
  return $true
}}, [IntPtr]::Zero) | Out-Null
"""
    try:
        subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def cmd_run_session(args: argparse.Namespace) -> int:
    config, _ = _build_effective_config(args)
    teacher_prompt = args.teacher_prompt or _compose_teacher_prompt(task=args.task, config=config)
    teacher_command_template = str(config.get("teacher_command_template", ""))
    teacher_workdir = config.get("teacher_workdir")
    teacher_timeout_s = float(config.get("teacher_timeout_s", 300))
    teacher_split_enabled = bool(config.get("teacher_split_enabled", True))
    try:
        teacher_result = run_teacher(
            prompt=teacher_prompt,
            command_template=teacher_command_template,
            cwd=teacher_workdir,
            timeout_s=teacher_timeout_s,
        )
        if teacher_split_enabled:
            teacher_plan = split_teacher_response(
                task=args.task,
                teacher_text=teacher_result.response_text,
                command_template=teacher_command_template,
                cwd=teacher_workdir,
                timeout_s=float(config.get("teacher_split_timeout_s", teacher_timeout_s)),
            )
        else:
            teacher_plan = TeacherChunkPlanResult(
                source_task=args.task,
                source_text=teacher_result.response_text,
                chunks=[
                    TeacherTaskChunk(
                        chunk_id="chunk-001",
                        title=args.task,
                        agent_prompt=teacher_result.response_text,
                        success_hint=None,
                        preconditions=[],
                        verification=None,
                        max_retries=0,
                        on_fail="fail_session",
                        notes=["teacher_split_disabled"],
                    )
                ],
                command_result=teacher_result.command_result,
            )
    except Exception as exc:
        teacher_result, teacher_plan = build_local_teacher_fallback(
            task=args.task,
            prompt=teacher_prompt,
            command_template=teacher_command_template,
            cwd=teacher_workdir,
            error=str(exc),
        )

    session_id, session_root = prepare_session_root(output_dir=str(config["output_dir"]), task=args.task)
    write_teacher_bundle(
        session_root=session_root,
        teacher_prompt=teacher_prompt,
        teacher_text=teacher_result.response_text,
        teacher_result=teacher_result,
        teacher_plan_payload={
            "source_task": teacher_plan.source_task,
            "source_text": teacher_plan.source_text,
            "chunks": [
                _serialize_chunk(chunk)
                for chunk in teacher_plan.chunks
            ],
            "command_result": {
                "command": list(teacher_plan.command_result.command),
                "cwd": teacher_plan.command_result.cwd,
                "returncode": teacher_plan.command_result.returncode,
                "stdout": teacher_plan.command_result.stdout,
                "stderr": teacher_plan.command_result.stderr,
                "duration_s": teacher_plan.command_result.duration_s,
            },
        },
    )

    bootstrap_result = None
    agent_model_id = config.get("agent_model_id")
    if not args.skip_bootstrap:
        if not agent_model_id:
            raise SystemExit("agent_model_id is required unless --skip-bootstrap is set")
        bootstrap_result = bootstrap_agent(
            agent_command=str(config["agent_command"]),
            model_id=str(agent_model_id),
            endpoint=config.get("agent_endpoint"),
            config_path=config.get("agent_config_path"),
            reasoning_enabled=bool(config.get("agent_reasoning_enabled", False)),
            cwd=config.get("agent_workdir"),
            timeout_s=float(config.get("agent_bootstrap_timeout_s", 600)),
        )
        write_agent_invocation_payload(
            session_root=session_root,
            name="agent_bootstrap.json",
            invocation=bootstrap_result,
        )

    run_manifests: list[dict] = []
    chunk_results: list[dict] = []
    total_chunks = len(teacher_plan.chunks)
    stopped_reason: str | None = None
    chunk_verification_enabled = bool(config.get("chunk_verification_enabled", True))
    for chunk_index, chunk in enumerate(teacher_plan.chunks, start=1):
        attempt_index = 0
        chunk_completed = False
        final_verification_result: dict | None = None
        final_run_label: str | None = None
        final_run_dir: str | None = None
        attempts_used = 0
        if chunk_verification_enabled:
            precheck_label = f"chunk-{chunk_index:03d}.precheck"
            precheck_result_obj = run_chunk_verification(
                endpoint=config.get("agent_endpoint"),
                timeout_s=float(config.get("chunk_verification_timeout_s", 60)),
                session_id=session_id,
                agent_run_label=precheck_label,
                chunk=chunk,
            )
            if precheck_result_obj is not None:
                write_verification_artifact(
                    session_root=session_root,
                    agent_run_label=precheck_label,
                    result=precheck_result_obj,
                )
                precheck_payload = asdict(precheck_result_obj)
                final_verification_result = precheck_payload
                if bool(precheck_payload.get("passed")):
                    chunk_completed = True
                    final_run_label = precheck_label
        while True:
            if chunk_completed:
                break
            attempts_used += 1
            prompt_text = _compose_chunk_prompt(chunk)
            if attempt_index > 0 and final_verification_result is not None:
                prompt_text = _compose_retry_prompt(
                    chunk=chunk,
                    verification_result=final_verification_result,
                    attempt_index=attempt_index,
                )
            prompt_result = run_agent_prompt(
                agent_command=str(config["agent_command"]),
                prompt=prompt_text,
                endpoint=config.get("agent_endpoint"),
                config_path=config.get("agent_config_path"),
                reasoning_enabled=bool(config.get("agent_reasoning_enabled", False)),
                cwd=config.get("agent_workdir"),
                timeout_s=float(config.get("agent_prompt_timeout_s", 1800)),
            )
            agent_run_label = f"chunk-{chunk_index:03d}.attempt-{attempt_index + 1:02d}"
            final_run_label = agent_run_label
            final_run_dir = str(prompt_result.run_dir)
            write_agent_invocation_payload(
                session_root=session_root,
                name=f"agent_runs/{agent_run_label}.prompt.json",
                invocation=prompt_result,
            )
            verification_result_obj = None
            if chunk_verification_enabled:
                verification_result_obj = run_chunk_verification(
                    endpoint=config.get("agent_endpoint"),
                    timeout_s=float(config.get("chunk_verification_timeout_s", 60)),
                    session_id=session_id,
                    agent_run_label=agent_run_label,
                    chunk=chunk,
                )
                if verification_result_obj is not None:
                    write_verification_artifact(
                        session_root=session_root,
                        agent_run_label=agent_run_label,
                        result=verification_result_obj,
                    )
            verification_payload = asdict(verification_result_obj) if verification_result_obj is not None else None
            final_verification_result = verification_payload
            chunk_completed = (
                bool(verification_payload.get("passed"))
                if verification_payload is not None
                else _chunk_completed_from_agent_payload(prompt_result.payload)
            )
            run_manifests.append(
                append_run_artifacts(
                    session_root=session_root,
                    session_id=session_id,
                    run_dir=str(prompt_result.run_dir),
                    task=args.task,
                    teacher_prompt=teacher_prompt,
                    teacher_text=teacher_result.response_text,
                    agent_prompt=prompt_text,
                    chunk_index=chunk_index,
                    chunk_count=total_chunks,
                    chunk_id=chunk.chunk_id,
                    chunk_title=chunk.title,
                    chunk_success_hint=chunk.success_hint,
                    chunk_preconditions=list(chunk.preconditions),
                    chunk_verification=chunk.verification,
                    chunk_max_retries=chunk.max_retries,
                    chunk_on_fail=chunk.on_fail,
                    chunk_attempt=attempt_index + 1,
                    chunk_completed=chunk_completed,
                    chunk_verification_result=verification_payload,
                    include_unexecuted_steps=args.include_unexecuted_steps,
                    agent_run_label=agent_run_label,
                )
            )
            if chunk_completed:
                break
            if chunk.on_fail == "retry_current_chunk" and attempt_index < chunk.max_retries:
                attempt_index += 1
                continue
            stopped_reason = "chunk_verification_failed" if verification_payload is not None else "chunk_incomplete"
            break

        chunk_results.append(
            {
                "chunk_index": chunk_index,
                "chunk_count": total_chunks,
                "chunk_id": chunk.chunk_id,
                "title": chunk.title,
                "success_hint": chunk.success_hint,
                "preconditions": list(chunk.preconditions),
                "verification": chunk.verification,
                "max_retries": chunk.max_retries,
                "on_fail": chunk.on_fail,
                "started": True,
                "completed": chunk_completed,
                "attempts_used": attempts_used,
                "final_agent_run_label": final_run_label,
                "final_source_run_dir": final_run_dir,
                "verification_result": final_verification_result,
                "stopped_reason": None if chunk_completed else stopped_reason,
            }
        )
        if not chunk_completed:
            break

    completed_chunks = len(chunk_results)
    for remaining_index, chunk in enumerate(teacher_plan.chunks[completed_chunks:], start=completed_chunks + 1):
        chunk_results.append(
            {
                "chunk_index": remaining_index,
                "chunk_count": total_chunks,
                "chunk_id": chunk.chunk_id,
                "title": chunk.title,
                "success_hint": chunk.success_hint,
                "preconditions": list(chunk.preconditions),
                "verification": chunk.verification,
                "max_retries": chunk.max_retries,
                "on_fail": chunk.on_fail,
                "started": False,
                "completed": False,
                "attempts_used": 0,
                "final_agent_run_label": None,
                "final_source_run_dir": None,
                "verification_result": None,
                "stopped_reason": "not_started_due_to_previous_chunk_failure" if stopped_reason else None,
            }
        )

    manifest = write_session_manifest(
        session_root=session_root,
        session_id=session_id,
        task=args.task,
        teacher_prompt=teacher_prompt,
        teacher_text=teacher_result.response_text,
        teacher_chunks=[_serialize_chunk(chunk) for chunk in teacher_plan.chunks],
        chunk_results=chunk_results,
        run_manifests=run_manifests,
        session_outcome=args.session_outcome,
        session_note=args.session_note,
        stopped_reason=stopped_reason,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _read_teacher_text(args: argparse.Namespace) -> str:
    if args.teacher_text:
        return args.teacher_text
    if args.teacher_text_file:
        return Path(args.teacher_text_file).read_text(encoding="utf-8").strip()
    raise SystemExit("either --teacher-text or --teacher-text-file is required")


def cmd_collect_run(args: argparse.Namespace) -> int:
    config, _ = _build_effective_config(args)
    teacher_text = _read_teacher_text(args)
    teacher_prompt = args.teacher_prompt or args.task
    manifest = collect_run_artifacts(
        run_dir=args.run_dir,
        output_dir=str(config["output_dir"]),
        task=args.task,
        teacher_prompt=teacher_prompt,
        teacher_text=teacher_text,
        teacher_result=None,
        bootstrap_result=None,
        prompt_result=None,
        session_outcome=args.session_outcome,
        session_note=args.session_note,
        include_unexecuted_steps=args.include_unexecuted_steps,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate training data from qwen-computer-use-agent runs.")
    parser.add_argument("--config", default=None, help="Path to generator config JSON.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-session", help="Run teacher -> agent -> dataset collection.")
    run_parser.add_argument("--task", required=True, help="High-level task or teacher question.")
    run_parser.add_argument("--teacher-prompt", default=None, help="Prompt sent to the external teacher. Defaults to --task.")
    run_parser.add_argument("--teacher-command-template", default=None, help="External teacher command template. Use {prompt} placeholder.")
    run_parser.add_argument("--teacher-timeout-s", type=float, default=None, help="Teacher command timeout.")
    run_parser.add_argument("--teacher-workdir", default=None, help="Teacher command working directory.")
    run_parser.add_argument("--teacher-split-enabled", action="store_true", help="Ask the teacher to split its own response into sequential agent chunks.")
    run_parser.add_argument("--teacher-split-timeout-s", type=float, default=None, help="Teacher split command timeout.")
    run_parser.add_argument("--agent-command", default=None, help="Path or name of qwen-computer-use-agent.")
    run_parser.add_argument("--agent-model-id", default=None, help="Model path passed to qwen-computer-use-agent --model-id.")
    run_parser.add_argument("--agent-config-path", default=None, help="Config path passed to qwen-computer-use-agent --config.")
    run_parser.add_argument("--agent-endpoint", default=None, help="Executor endpoint passed to qwen-computer-use-agent.")
    run_parser.add_argument("--agent-workdir", default=None, help="Working directory for qwen-computer-use-agent.")
    run_parser.add_argument("--agent-bootstrap-timeout-s", type=float, default=None, help="Agent bootstrap timeout.")
    run_parser.add_argument("--agent-prompt-timeout-s", type=float, default=None, help="Agent prompt timeout.")
    run_parser.add_argument("--agent-reasoning-enabled", action="store_true", help="Enable reasoning when bootstrapping and prompting the agent.")
    run_parser.add_argument("--chunk-verification-enabled", action="store_true", help="Run teacher-provided verifier checks after each chunk execution.")
    run_parser.add_argument("--chunk-verification-timeout-s", type=float, default=None, help="Timeout for post-chunk verification execution.")
    run_parser.add_argument("--skip-bootstrap", action="store_true", help="Skip qwen-computer-use-agent --model-id bootstrap and only send --prompt.")
    run_parser.add_argument("--output-dir", default=None, help="Directory that will receive generated datasets.")
    run_parser.add_argument("--session-outcome", type=_session_outcome_arg, default=None, help="Optional manual session label.")
    run_parser.add_argument("--session-note", default=None, help="Optional short session note.")
    run_parser.add_argument("--include-unexecuted-steps", action="store_true", help="Include steps without executor artifacts.")
    run_parser.set_defaults(func=cmd_run_session)

    collect_parser = subparsers.add_parser("collect-run", help="Convert an existing agent run dir into training data.")
    collect_parser.add_argument("--run-dir", required=True, help="Existing qwen-computer-use-agent run directory.")
    collect_parser.add_argument("--task", required=True, help="Task associated with the run.")
    collect_parser.add_argument("--teacher-prompt", default=None, help="Original external teacher prompt.")
    collect_parser.add_argument("--teacher-text", default=None, help="Teacher response text.")
    collect_parser.add_argument("--teacher-text-file", default=None, help="Path to a file containing teacher response text.")
    collect_parser.add_argument("--output-dir", default=None, help="Directory that will receive generated datasets.")
    collect_parser.add_argument("--session-outcome", type=_session_outcome_arg, default=None, help="Optional manual session label.")
    collect_parser.add_argument("--session-note", default=None, help="Optional short session note.")
    collect_parser.add_argument("--include-unexecuted-steps", action="store_true", help="Include steps without executor artifacts.")
    collect_parser.set_defaults(func=cmd_collect_run)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    finally:
        if getattr(args, "command", None) == "run-session":
            _request_terminal_attention("run-session finished")


if __name__ == "__main__":
    raise SystemExit(main())
