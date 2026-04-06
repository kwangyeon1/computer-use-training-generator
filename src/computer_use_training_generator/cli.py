from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import bootstrap_agent, run_agent_prompt
from .collector import collect_run_artifacts
from .config_utils import load_generator_config
from .teacher import run_teacher


def _default_config_path() -> Path:
    return (Path(__file__).resolve().parents[2] / "config" / "generator.default.json").resolve()


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
    _override(config, "agent_command", getattr(args, "agent_command", None))
    _override(config, "agent_model_id", getattr(args, "agent_model_id", None))
    _override(config, "agent_config_path", getattr(args, "agent_config_path", None))
    _override(config, "agent_endpoint", getattr(args, "agent_endpoint", None))
    _override(config, "agent_workdir", getattr(args, "agent_workdir", None))
    _override(config, "agent_bootstrap_timeout_s", getattr(args, "agent_bootstrap_timeout_s", None))
    _override(config, "agent_prompt_timeout_s", getattr(args, "agent_prompt_timeout_s", None))
    if getattr(args, "agent_reasoning_enabled", False):
        config["agent_reasoning_enabled"] = True
    if getattr(args, "output_dir", None) is not None:
        config["output_dir"] = str(Path(args.output_dir).resolve())
    return config, config_path


def cmd_run_session(args: argparse.Namespace) -> int:
    config, _ = _build_effective_config(args)
    teacher_prompt = args.teacher_prompt or args.task

    teacher_result = run_teacher(
        prompt=teacher_prompt,
        command_template=str(config.get("teacher_command_template", "")),
        cwd=config.get("teacher_workdir"),
        timeout_s=float(config.get("teacher_timeout_s", 300)),
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

    prompt_result = run_agent_prompt(
        agent_command=str(config["agent_command"]),
        prompt=teacher_result.response_text,
        endpoint=config.get("agent_endpoint"),
        config_path=config.get("agent_config_path"),
        reasoning_enabled=bool(config.get("agent_reasoning_enabled", False)),
        cwd=config.get("agent_workdir"),
        timeout_s=float(config.get("agent_prompt_timeout_s", 1800)),
    )

    manifest = collect_run_artifacts(
        run_dir=str(prompt_result.run_dir),
        output_dir=str(config["output_dir"]),
        task=args.task,
        teacher_prompt=teacher_prompt,
        teacher_text=teacher_result.response_text,
        teacher_result=teacher_result,
        bootstrap_result=bootstrap_result,
        prompt_result=prompt_result,
        session_outcome=args.session_outcome,
        session_note=args.session_note,
        include_unexecuted_steps=args.include_unexecuted_steps,
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
    run_parser.add_argument("--agent-command", default=None, help="Path or name of qwen-computer-use-agent.")
    run_parser.add_argument("--agent-model-id", default=None, help="Model path passed to qwen-computer-use-agent --model-id.")
    run_parser.add_argument("--agent-config-path", default=None, help="Config path passed to qwen-computer-use-agent --config.")
    run_parser.add_argument("--agent-endpoint", default=None, help="Executor endpoint passed to qwen-computer-use-agent.")
    run_parser.add_argument("--agent-workdir", default=None, help="Working directory for qwen-computer-use-agent.")
    run_parser.add_argument("--agent-bootstrap-timeout-s", type=float, default=None, help="Agent bootstrap timeout.")
    run_parser.add_argument("--agent-prompt-timeout-s", type=float, default=None, help="Agent prompt timeout.")
    run_parser.add_argument("--agent-reasoning-enabled", action="store_true", help="Enable reasoning when bootstrapping and prompting the agent.")
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
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
