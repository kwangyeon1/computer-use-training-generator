from __future__ import annotations

import shlex
import subprocess
import time

from .models import CommandResult


def render_command_template(template: str, prompt: str) -> list[str]:
    args = shlex.split(template)
    rendered: list[str] = []
    inserted = False
    for token in args:
        if "{prompt}" in token:
            rendered.append(token.replace("{prompt}", prompt))
            inserted = True
        else:
            rendered.append(token)
    if not inserted:
        rendered.append(prompt)
    return rendered


def command_to_shell_string(args: list[str]) -> str:
    return shlex.join(args)


def run_command(command: list[str], *, cwd: str | None, timeout_s: float | None) -> CommandResult:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    duration_s = time.monotonic() - started
    return CommandResult(
        command=command,
        cwd=cwd,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_s=round(duration_s, 3),
    )
