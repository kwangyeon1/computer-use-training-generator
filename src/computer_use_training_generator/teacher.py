from __future__ import annotations

from .models import TeacherResult
from .subprocess_utils import render_command_template, run_command


def run_teacher(*, prompt: str, command_template: str, cwd: str | None, timeout_s: float) -> TeacherResult:
    if not command_template.strip():
        raise ValueError("teacher_command_template is required")
    command = render_command_template(command_template, prompt)
    result = run_command(command, cwd=cwd, timeout_s=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(f"teacher command failed with exit code {result.returncode}")
    response_text = result.stdout.strip()
    if not response_text:
        raise RuntimeError("teacher command returned empty stdout")
    return TeacherResult(prompt=prompt, response_text=response_text, command_result=result)
