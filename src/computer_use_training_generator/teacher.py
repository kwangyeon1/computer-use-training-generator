from __future__ import annotations

import json
import re

from .models import CommandResult, TeacherChunkPlanResult, TeacherResult, TeacherTaskChunk
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
        "success_hint": "short success condition",
        "preconditions": ["short prerequisite that should already hold before this chunk"],
        "verification": {{
          "checks": [
            {{"kind": "path_exists", "path": "~/Downloads/example.exe"}},
            {{"kind": "file_exists_glob", "pattern": "~/Downloads/example-*.exe"}},
            {{"kind": "file_size_gt", "pattern": "~/Downloads/example-*.exe", "bytes": 1000000}},
            {{"kind": "process_exists", "name": "KakaoTalk.exe"}}
          ]
        }},
        "max_retries": 1,
        "on_fail": "retry_current_chunk"
      }}
    ]
  }}
- Each chunk must be sequential and focused.
- Each chunk must only ask the agent to do one stage of the task from the current computer state.
- Do not combine the whole procedure into one chunk.
- Do not include future chunks inside the current chunk prompt.
- Keep chunk prompts self-contained enough for the agent, but assume previous chunks have already run.
- Write chunk prompts for a Python-code-generating computer-use agent, not for a human operator.
- Each chunk prompt should describe what the agent must accomplish with executable Python on the target machine.
- Do not rely on a human to manually click, type, confirm dialogs, or inspect pages outside the Python automation flow.
- Use as many chunks as needed, but keep them compact and stage-focused.
- If the original answer contains official URLs or important warnings, keep them in the relevant chunk prompt.
- Each chunk must include a read-only verification plan.
- Verification must use only the allowed check kinds: `path_exists`, `file_exists_glob`, `file_size_gt`, `process_exists`.
- All verification checks are combined with logical AND.
- If you need to allow multiple possible installer filenames, use one broad glob that matches all acceptable names instead of multiple alternative checks.
- For Windows installer download chunks, prefer a broad `.exe` glob such as `~/Downloads/*vendor*.exe` instead of a brittle pattern that requires exact words like `installer` or `setup`.
- Do not output raw Python for verification.
- `preconditions` should describe what must already be true before the chunk starts.
- `max_retries` should be a small integer, usually 0, 1, or 2.
- `on_fail` must be either `retry_current_chunk` or `fail_session`.
- Do not add commentary outside JSON.
- Plan for the target machine described by the task and teacher answer, not for your own CLI sandbox.
- If the task is about a Windows desktop or Windows software install, use Windows-oriented chunk prompts and Windows-friendly verifier checks.
- For Windows software installation tasks, prefer the official `.exe` installer build, not `.zip`, portable, or archive downloads unless the task explicitly asks for those formats.
- For Windows installer download chunks, state explicitly in `agent_prompt` that the agent must download the installer `.exe` and must avoid `.zip` or archive builds.
- For Windows installer download chunks, prefer deterministic Python-first flows: direct official URL discovery, Python download to `Downloads`, file-size/path verification, and Python-launched installer execution before browser-click-heavy flows.
- Only fall back to browser GUI navigation when a direct official installer URL cannot be determined from the teacher answer or current task context.
- For general GUI operation tasks after installation, continue from the current desktop/app state instead of restarting setup or redownloading software unless the task explicitly asks for it.
- For general GUI operation tasks, prefer prompts and verifiers that reflect the intended app state, open window/process, created file, or changed project/workspace state.
- For general GUI operation tasks, prefer Python GUI automation or process/file inspection over instructions written as if a human will take the next action.
- Do not emit Linux or macOS verification paths unless the task explicitly requires those platforms.

Overall task:
{task}

Teacher answer:
{teacher_text}
"""

_GENERIC_INSTALLER_TOKENS = {
    "downloads",
    "download",
    "windows",
    "window",
    "win",
    "installer",
    "setup",
    "latest",
    "community",
    "lite",
    "x64",
    "x86",
    "x86_64",
    "exe",
    "zip",
    "archive",
    "http",
    "https",
    "www",
    "com",
    "io",
    "docs",
    "edition",
}

_GENERIC_ACTION_TOKENS = {
    "open",
    "page",
    "official",
    "downloads",
    "folder",
    "save",
    "saveas",
    "run",
    "launch",
    "execute",
    "locate",
    "finish",
    "default",
    "defaults",
    "start",
    "using",
    "choose",
    "click",
    "complete",
    "confirm",
    "prompted",
    "suitable",
    "edition",
}


def _looks_like_install_task(task: str) -> bool:
    lowered = str(task or "").lower()
    return any(token in lowered for token in ("설치", "install", "installer", "setup"))


def _fallback_command_result(*, prompt: str, command_template: str, cwd: str | None, error: str) -> CommandResult:
    try:
        command = render_command_template(command_template, prompt) if str(command_template or "").strip() else ["<local-teacher-fallback>"]
    except Exception:
        command = ["<local-teacher-fallback>"]
    return CommandResult(
        command=command,
        cwd=cwd,
        returncode=1,
        stdout="",
        stderr=str(error or "local teacher fallback"),
        duration_s=0.0,
    )


def _target_installer_keywords(*parts: str, limit: int = 2) -> list[str]:
    tokens: list[str] = []
    for part in parts:
        for token in re.split(r"[^a-z0-9]+", str(part or "").lower()):
            if (
                not token
                or token in _GENERIC_INSTALLER_TOKENS
                or token in _GENERIC_ACTION_TOKENS
                or token.isdigit()
                or len(token) <= 2
            ):
                continue
            if token not in tokens:
                tokens.append(token)
            if len(tokens) >= limit:
                return tokens
    return tokens


def _matching_installer_hint(*, source_task: str, title: str, agent_prompt: str, action: str) -> str:
    keywords = _target_installer_keywords(source_task, title, agent_prompt)
    if keywords:
        joined = ", ".join(f"`{keyword}`" for keyword in keywords)
        return (
            f"대상 앱과 일치하는 installer만 사용하세요. 파일명은 가능하면 {joined} 같은 대상 앱 키워드를 포함해야 하며, "
            f"무관한 다른 installer `.exe`는 {action}하지 마세요."
        )
    return f"대상 앱과 일치하는 installer만 사용하고, 무관한 다른 installer `.exe`는 {action}하지 마세요."


def _official_source_hint(*parts: str) -> str:
    keywords = _target_installer_keywords(*parts, limit=3)
    lowered = " ".join(str(part or "").lower() for part in parts)
    if "dbeaver" in lowered or "dbeaver" in keywords:
        return (
            "이 작업에서 확인할 수 있는 공식 source 후보는 `https://dbeaver.com/download/` 와 "
            "`https://dbeaver.com/files/dbeaver-le-latest-x86_64-setup.exe` 와 "
            "`https://github.com/dbeaver/dbeaver/releases/latest` 입니다. "
            "DBeaver download page의 Windows Installer direct URL은 현재 "
            "`https://dbeaver.com/files/dbeaver-le-latest-x86_64-setup.exe` 입니다. "
            "현재 latest GitHub release tag는 `26.0.2`이며 Windows installer asset은 "
            "`dbeaver-ce-26.0.2-x86_64-setup.exe` 형태입니다. "
            "먼저 vendor direct installer URL을 시도하고, 실패하면 "
            "`https://github.com/dbeaver/dbeaver/releases/download/26.0.2/dbeaver-ce-26.0.2-x86_64-setup.exe` "
            "를 먼저 시도하고, 실패하면 release page HTML에서 최신 `.exe` asset을 다시 추출하세요."
        )
    return ""


def _simplify_windows_installer_glob(pattern: str) -> str:
    raw = str(pattern or "").strip()
    lowered = raw.lower()
    if not raw or not lowered.endswith(".exe"):
        return raw
    prefix, separator, filename = raw.rpartition("/")
    filename_tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", filename.lower())
        if token and token not in _GENERIC_INSTALLER_TOKENS and not token.isdigit()
    ]
    if not filename_tokens:
        return raw
    deduped: list[str] = []
    for token in filename_tokens:
        if token not in deduped:
            deduped.append(token)
    vendor_tokens = deduped[:2]
    vendor_glob = f"*{'*'.join(vendor_tokens)}*.exe"
    return f"{prefix}{separator}{vendor_glob}" if separator else vendor_glob


def _normalize_windows_installer_verification(
    *,
    title: str,
    agent_prompt: str,
    verification: dict | None,
) -> dict | None:
    if not isinstance(verification, dict):
        return verification
    combined = f"{title}\n{agent_prompt}".lower()
    if "windows" not in combined or ".exe" not in combined:
        return verification
    if not any(keyword in combined for keyword in ("installer", "install", "설치", "download", "다운로드")):
        return verification
    checks = verification.get("checks")
    if not isinstance(checks, list):
        return verification
    normalized_checks: list[dict] = []
    changed = False
    for item in checks:
        if not isinstance(item, dict):
            normalized_checks.append(item)
            continue
        updated = dict(item)
        if str(updated.get("kind") or "").strip() == "file_exists_glob":
            pattern = str(updated.get("pattern") or "").strip()
            simplified = _simplify_windows_installer_glob(pattern)
            if simplified and simplified != pattern:
                updated["pattern"] = simplified
                changed = True
        normalized_checks.append(updated)
    if not changed:
        return verification
    return {**verification, "checks": normalized_checks}


def _normalize_windows_installer_agent_prompt(*, source_task: str, title: str, agent_prompt: str) -> str:
    raw = str(agent_prompt or "").strip()
    if not raw:
        return raw
    combined = f"{title}\n{raw}".lower()
    normalized = raw
    common_python_hint = (
        "이 chunk는 실행 가능한 Python 코드만으로 수행하세요. "
        "다운로드는 curl, wget, powershell, http.server 같은 외부 도구 대신 Python HTTP와 파일 I/O를 사용하세요. "
        "Windows 사용자 폴더 경로는 %USERPROFILE% 문자열을 그대로 쓰지 말고 os.environ, os.path.expandvars, Path.home() 등으로 실제 경로를 해석하세요. "
        "공식 landing page에 raw installer 링크가 바로 없으면 같은 스크립트 안에서 다른 공식 페이지나 공식 release 페이지도 확인하세요. "
        "HTML에서는 href만 보지 말고 absolute https .exe URL 후보도 찾고, 선택한 URL은 실제 HTTP 요청으로 검증하세요. "
        "다운로드나 설치가 실패하면 예외를 발생시키거나 non-zero로 종료하세요."
    )
    install_python_hint = (
        "이 install chunk에서는 새 다운로드 helper나 URL 탐색 로직을 만들지 말고, 이미 내려받은 installer `.exe`를 바로 찾는 코드부터 시작하세요. "
        "함수 여러 개나 main()을 만들지 말고 top-level 직선 코드로 작성하세요. "
        "처음 25줄 안에서 Downloads 안의 installer 경로를 찾고, silent install 시도를 시작하세요. "
        "경로는 Path.home() 이나 os.environ 으로 실제 Windows 경로를 해석하세요. "
        "silent install은 `/VERYSILENT`, `/SILENT`, `/SP-`, `/NORESTART` 같은 일반적인 Windows installer switch 조합을 우선 시도하세요. "
        "설치 후에는 `%LOCALAPPDATA%`, `%ProgramFiles%`, `%ProgramFiles(x86)%` 아래의 일반적인 설치 경로에서 대상 앱 `.exe`를 찾고, 찾으면 즉시 실행한 뒤 프로세스가 뜰 때까지 확인하세요. "
        "silent install이 분명히 실패하거나 timeout이 나면 같은 silent command를 반복하지 말고 Python GUI 자동화로 현재 설치 창을 진행하세요."
    )
    source_hint = _official_source_hint(source_task, title, raw)
    install_action_markers = (
        "run it",
        "run the",
        "launch",
        "execute",
        "finish",
        "uac",
        "wizard",
        "실행",
        "설치 완료",
        "설치가 완료",
        "마법사",
        "finish로",
    )
    if "windows" in combined and ".exe" in combined and any(keyword in combined for keyword in install_action_markers):
        install_hint = (
            "실행할 installer는 현재 작업 대상 앱과 일치하는 `.exe`만 고르세요.\n"
            + _matching_installer_hint(source_task=source_task, title=title, agent_prompt=raw, action="실행")
        )
        if install_python_hint not in normalized:
            normalized = f"{install_python_hint}\n\n{normalized}"
        if install_hint not in normalized:
            normalized = f"{install_hint}\n\n{normalized}"
        return normalized
    if "windows" in combined and ".exe" in combined and any(keyword in combined for keyword in ("download", "다운로드")):
        reuse_hint = (
            "이미 Downloads 폴더에 사용할 수 있는 대상 앱의 Windows installer `.exe`가 있으면 새로 받지 말고 그 파일을 그대로 사용해도 됩니다.\n"
            + _matching_installer_hint(source_task=source_task, title=title, agent_prompt=raw, action="사용")
        )
        if common_python_hint not in normalized:
            normalized = f"{common_python_hint}\n\n{normalized}"
        if source_hint and source_hint not in normalized:
            normalized = f"{normalized}\n\n{source_hint}"
        if reuse_hint not in normalized:
            normalized = f"{normalized}\n\n{reuse_hint}"
    return normalized


def _normalize_general_gui_agent_prompt(*, title: str, agent_prompt: str) -> str:
    raw = str(agent_prompt or "").strip()
    if not raw:
        return raw
    combined = f"{title}\n{raw}".lower()
    if any(keyword in combined for keyword in ("download", "다운로드", "installer", ".exe", "setup", "설치")):
        return raw
    if not any(keyword in combined for keyword in ("open", "launch", "create", "click", "project", "window", "menu", "dialog", "파일", "열", "실행", "생성", "프로젝트", "창", "메뉴")):
        return raw
    continue_hint = (
        "현재 앱이나 창이 이미 열려 있으면 그 상태를 이어서 사용하고, 작업과 무관한 재설치나 재다운로드는 하지 마세요.\n"
        "사람이 수동으로 조작한다고 가정하지 말고, 캡처 화면을 보고 판단한 뒤 실행 가능한 Python 코드만으로 GUI 상태 확인과 조작을 수행하세요."
    )
    if continue_hint in raw:
        return raw
    return f"{raw}\n\n{continue_hint}"


def _local_install_chunks(task: str) -> list[TeacherTaskChunk]:
    keyword = (_target_installer_keywords(task, limit=1) or ["targetapp"])[0]
    downloads_subdir = keyword.capitalize()
    installed_exe_glob = f"~/AppData/Local/**/*{keyword}*/{keyword}.exe"
    download_title = f"Download {keyword} installer"
    install_title = f"Install {keyword}"
    launch_title = f"Launch {keyword}"
    download_prompt = (
        f"Use executable Python on the Windows machine to download the official Windows installer `.exe` for the target app from this task: {task}. "
        f"Prefer the official vendor site or official release pages, resolve the current Windows x86_64 setup `.exe` URL in Python, and save it to `%USERPROFILE%\\\\Downloads\\\\{downloads_subdir}\\\\`. "
        f"If one official page does not expose a raw `.exe` link, inspect another official page or official release page in the same script before failing. Do not use `.zip`, portable, or archive downloads."
    )
    install_prompt = (
        f"{_matching_installer_hint(source_task=task, title=install_title, agent_prompt=task, action='실행')} "
        f"Use executable Python only. Do not download anything in this chunk. "
        f"First inspect the current screenshot and desktop state for an installer wizard, UAC prompt, license dialog, destination dialog, or completion dialog, and drive that UI forward if it is visible. "
        f"Launching only the final app executable is not enough for this chunk. If the app is not already installed, the script must either launch the installer `.exe` itself or operate a visible installer window with Python GUI automation. "
        f"If no installer UI is visible, find the existing installer `.exe` in `%USERPROFILE%\\\\Downloads\\\\{downloads_subdir}\\\\`, launch it once, wait long enough for setup to appear or continue, and then check only likely install directories such as `%LOCALAPPDATA%\\\\DBeaver`, `%LOCALAPPDATA%\\\\Programs\\\\DBeaver`, `%ProgramFiles%\\\\DBeaver`, and `%ProgramFiles(x86)%\\\\DBeaver` for `{keyword}.exe`. Avoid recursively scanning the whole of `%LOCALAPPDATA%` or `%ProgramFiles%`. "
        f"Do not import `pywin32`, `pywinauto`, `win32gui`, `win32con`, `win32api`, or `pythoncom`. Prefer the standard library, `psutil`, `pyautogui`, and `pygetwindow` only if clearly needed. "
        f"Fail explicitly if no valid installer is found or if the install still has not produced the app executable. End only when the installed app `.exe` exists on disk."
    )
    launch_prompt = (
        f"Use executable Python on the Windows machine to locate the already-installed app executable for the target app from this task: {task}, "
        f"launch it once, bring the app window to the foreground if needed, and end only after the app process is running. "
        f"Do not redownload or reinstall the app in this chunk. Prefer existing install paths under `%LOCALAPPDATA%`, `%ProgramFiles%`, and `%ProgramFiles(x86)%`, and if the app is already running just verify that process and focus the window."
    )
    return [
        TeacherTaskChunk(
            chunk_id="chunk-001",
            title=download_title,
            agent_prompt=_normalize_windows_installer_agent_prompt(
                source_task=task,
                title=download_title,
                agent_prompt=download_prompt,
            ),
            success_hint=f"A target-app installer `.exe` exists in Downloads\\{downloads_subdir} and is non-empty.",
            preconditions=[
                "Windows desktop session is available and Python can run.",
                "The machine has network access to the official vendor or release pages.",
            ],
            verification={
                "checks": [
                    {"kind": "file_exists_glob", "pattern": f"~/Downloads/{downloads_subdir}/*{keyword}*.exe"},
                    {"kind": "file_size_gt", "pattern": f"~/Downloads/{downloads_subdir}/*{keyword}*.exe", "bytes": 1000000},
                ]
            },
            max_retries=1,
            on_fail="retry_current_chunk",
            notes=["local_teacher_fallback", "python_first_download_chunk"],
        ),
        TeacherTaskChunk(
            chunk_id="chunk-002",
            title=install_title,
            agent_prompt=install_prompt,
            success_hint=f"The installed app executable for `{keyword}` exists under AppData Local or another common install path.",
            preconditions=[
                f"A non-empty target-app installer `.exe` already exists in Downloads\\{downloads_subdir}.",
            ],
            verification={
                "checks": [
                    {"kind": "file_exists_glob", "pattern": installed_exe_glob},
                ]
            },
            max_retries=2,
            on_fail="retry_current_chunk",
            notes=["local_teacher_fallback", "python_first_install_chunk"],
        ),
        TeacherTaskChunk(
            chunk_id="chunk-003",
            title=launch_title,
            agent_prompt=_normalize_general_gui_agent_prompt(
                title=launch_title,
                agent_prompt=launch_prompt,
            ),
            success_hint=f"The installed app process for `{keyword}` is running.",
            preconditions=[
                f"The installed app executable for `{keyword}` already exists on disk.",
            ],
            verification={
                "checks": [
                    {"kind": "process_exists", "name": f"{keyword}.exe"},
                ]
            },
            max_retries=2,
            on_fail="retry_current_chunk",
            notes=["local_teacher_fallback", "python_first_launch_chunk"],
        ),
    ]


def build_local_teacher_fallback(
    *,
    task: str,
    prompt: str,
    command_template: str,
    cwd: str | None,
    error: str,
) -> tuple[TeacherResult, TeacherChunkPlanResult]:
    command_result = _fallback_command_result(
        prompt=prompt,
        command_template=command_template,
        cwd=cwd,
        error=error,
    )
    response_text = (
        "Local fallback planner generated this plan because the external teacher was unavailable. "
        "Use Python-first Windows automation: discover the official installer, download it to Downloads, then install and launch the app."
    )
    if _looks_like_install_task(task):
        chunks = _local_install_chunks(task)
    else:
        chunks = [
            TeacherTaskChunk(
                chunk_id="chunk-001",
                title=task,
                agent_prompt=task,
                success_hint=None,
                preconditions=[],
                verification=None,
                max_retries=0,
                on_fail="fail_session",
                notes=["local_teacher_fallback_generic"],
            )
        ]
    teacher_result = TeacherResult(
        prompt=prompt,
        response_text=response_text,
        command_result=command_result,
    )
    teacher_plan = TeacherChunkPlanResult(
        source_task=task,
        source_text=response_text,
        chunks=chunks,
        command_result=command_result,
    )
    return teacher_result, teacher_plan


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
        agent_prompt = _normalize_windows_installer_agent_prompt(
            source_task=source_task,
            title=title,
            agent_prompt=agent_prompt,
        )
        agent_prompt = _normalize_general_gui_agent_prompt(title=title, agent_prompt=agent_prompt)
        success_hint = str(item.get("success_hint") or "").strip() or None
        preconditions = [str(value).strip() for value in item.get("preconditions", []) if str(value).strip()] if isinstance(item.get("preconditions"), list) else []
        raw_verification = item.get("verification")
        verification = raw_verification if isinstance(raw_verification, dict) else None
        verification = _normalize_windows_installer_verification(
            title=title,
            agent_prompt=agent_prompt,
            verification=verification,
        )
        raw_max_retries = item.get("max_retries")
        try:
            max_retries = int(raw_max_retries) if raw_max_retries is not None else (1 if verification else 0)
        except (TypeError, ValueError):
            max_retries = 1 if verification else 0
        max_retries = max(0, min(2, max_retries))
        on_fail = str(item.get("on_fail") or ("retry_current_chunk" if max_retries > 0 else "fail_session")).strip().lower()
        if on_fail not in {"retry_current_chunk", "fail_session"}:
            on_fail = "retry_current_chunk" if max_retries > 0 else "fail_session"
        notes = [str(value).strip() for value in item.get("notes", []) if str(value).strip()] if isinstance(item.get("notes"), list) else []
        normalized.append(
            TeacherTaskChunk(
                chunk_id=chunk_id,
                title=title,
                agent_prompt=agent_prompt,
                success_hint=success_hint,
                preconditions=preconditions,
                verification=verification,
                max_retries=max_retries,
                on_fail=on_fail,
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
            preconditions=[],
            verification=None,
            max_retries=0,
            on_fail="fail_session",
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
                preconditions=[],
                verification=None,
                max_retries=0,
                on_fail="fail_session",
                notes=["fallback_due_to_split_parse_failure"],
            )
        ]
    return TeacherChunkPlanResult(
        source_task=task,
        source_text=teacher_text,
        chunks=chunks,
        command_result=result,
    )
