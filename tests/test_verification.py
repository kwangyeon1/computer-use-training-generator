from computer_use_training_generator.verification import (
    _expanded_glob_patterns,
    _has_file_based_checks,
    build_verification_code,
)
from computer_use_training_generator.teacher import (
    build_local_teacher_fallback,
    _normalize_general_gui_agent_prompt,
    _normalize_windows_installer_agent_prompt,
    _simplify_windows_installer_glob,
)
from computer_use_training_generator.cli import _compose_chunk_prompt
from computer_use_training_generator.models import TeacherTaskChunk


def test_expanded_glob_patterns_adds_windows_setup_aliases() -> None:
    patterns = _expanded_glob_patterns("~/Downloads/dbeaver-ce-*-windows-x86_64.exe")
    assert "~/Downloads/dbeaver-ce-*-windows-x86_64.exe" in patterns
    assert "~/Downloads/dbeaver-ce-*-x86_64-setup.exe" in patterns
    assert "~/Downloads/dbeaver-ce-*-setup.exe" in patterns


def test_expanded_glob_patterns_adds_aliases_for_windows_wildcard_suffix() -> None:
    patterns = _expanded_glob_patterns("~/Downloads/dbeaver-ce-*-windows-*.exe")
    assert "~/Downloads/dbeaver-ce-*-windows-*.exe" in patterns
    assert "~/Downloads/dbeaver-ce-*-x86_64-setup.exe" in patterns
    assert "~/Downloads/dbeaver-ce-*-setup.exe" in patterns


def test_expanded_glob_patterns_relaxes_brittle_windows_installer_glob() -> None:
    patterns = _expanded_glob_patterns("~/Downloads/*dbeaver*win*installer*.exe")
    assert "~/Downloads/*dbeaver*win*installer*.exe" in patterns
    assert "~/Downloads/*dbeaver*win*setup*.exe" in patterns
    assert "~/Downloads/*dbeaver*.exe" in patterns


def test_has_file_based_checks_detects_download_verifiers() -> None:
    assert _has_file_based_checks(
        {
            "checks": [
                {
                    "kind": "file_exists_glob",
                    "pattern": "~/Downloads/dbeaver-ce-*.exe",
                }
            ]
        }
    )
    assert not _has_file_based_checks({"checks": [{"kind": "process_exists", "name": "DBeaver.exe"}]})


def test_build_verification_code_searches_expanded_patterns() -> None:
    code = build_verification_code(
        {
            "checks": [
                {
                    "kind": "file_exists_glob",
                    "pattern": "~/Downloads/dbeaver-ce-*-windows-x86_64.exe",
                }
            ]
        }
    )
    assert code is not None
    assert "_expanded_glob_patterns" in code
    assert 'entry["searched_patterns"] = searched_patterns' in code


def test_simplify_windows_installer_glob_prefers_vendor_exe_glob() -> None:
    assert (
        _simplify_windows_installer_glob("~/Downloads/*dbeaver*win*installer*.exe")
        == "~/Downloads/*dbeaver*.exe"
    )


def test_normalize_windows_installer_agent_prompt_adds_reuse_hint() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="dbeaver를 설치해줘",
        title="Download installer",
        agent_prompt="브라우저에서 공식 다운로드 페이지를 열고 Windows용 `.exe`를 다운로드하세요.",
    )
    assert "실행 가능한 Python 코드만으로 수행" in prompt
    assert "curl, wget, powershell, http.server 같은 외부 도구" in prompt
    assert "os.environ" in prompt
    assert "다른 공식 페이지나 공식 release 페이지도 확인" in prompt
    assert "absolute https .exe URL 후보" in prompt
    assert "https://dbeaver.com/download/" in prompt
    assert "https://github.com/dbeaver/dbeaver/releases/latest" in prompt
    assert "이미 Downloads 폴더에 사용할 수 있는 대상 앱의 Windows installer" in prompt


def test_normalize_windows_installer_agent_prompt_uses_target_app_keyword() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="dbeaver를 설치해줘",
        title="Run Installer",
        agent_prompt="Locate the downloaded Windows installer `.exe` in Downloads and run it.",
    )
    assert "`dbeaver`" in prompt
    assert "`run`" not in prompt
    assert "`locate`" not in prompt


def test_normalize_windows_installer_agent_prompt_adds_install_chunk_guidance() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="dbeaver를 설치해줘",
        title="Install and launch DBeaver",
        agent_prompt="Locate the downloaded Windows installer `.exe` in Downloads and run it, finish install, and launch the app.",
    )
    assert "새 다운로드 helper나 URL 탐색 로직을 만들지 말고" in prompt
    assert "top-level 직선 코드" in prompt
    assert "/VERYSILENT" in prompt
    assert "%LOCALAPPDATA%" in prompt
    assert "Path.home() 이나 os.environ" in prompt
    assert "같은 silent command를 반복하지 말고" in prompt
    assert "curl, wget, powershell" not in prompt
    assert "https://dbeaver.com/download/" not in prompt


def test_normalize_windows_installer_agent_prompt_does_not_add_run_hint_to_download_chunk() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="dbeaver를 설치해줘",
        title="Download DBeaver Installer",
        agent_prompt="브라우저에서 DBeaver Community 공식 페이지를 열고 Windows installer `.exe`를 다운로드하고, 다운로드가 완료될 때까지 기다리세요.",
    )
    assert "실행할 installer는" not in prompt
    assert "새로 받지 말고" in prompt


def test_normalize_general_gui_agent_prompt_adds_continue_hint() -> None:
    prompt = _normalize_general_gui_agent_prompt(
        title="Create Eclipse project",
        agent_prompt="Eclipse에서 새 Java 프로젝트를 생성하고 기본 프로젝트 창이 보이게 하세요.",
    )
    assert "현재 앱이나 창이 이미 열려 있으면" in prompt
    assert "실행 가능한 Python 코드만으로 GUI 상태 확인과 조작" in prompt


def test_compose_chunk_prompt_requires_python_only() -> None:
    prompt = _compose_chunk_prompt(
        TeacherTaskChunk(
            chunk_id="chunk-001",
            title="Download DBeaver",
            agent_prompt="DBeaver installer `.exe`를 다운로드하세요.",
            success_hint="Downloads에 installer가 있음",
            preconditions=["인터넷 연결 가능"],
            verification=None,
            max_retries=1,
            on_fail="retry_current_chunk",
            notes=[],
        )
    )
    assert prompt.startswith("Return executable Python only for this chunk.")


def test_compose_chunk_prompt_gui_first_mentions_visible_ui() -> None:
    prompt = _compose_chunk_prompt(
        TeacherTaskChunk(
            chunk_id="chunk-001",
            title="Download DBeaver",
            agent_prompt="DBeaver installer `.exe`를 다운로드하세요.",
            success_hint=None,
            preconditions=[],
            verification=None,
            max_retries=0,
            on_fail="fail_session",
            notes=[],
        ),
        execution_style="gui_first",
    )
    assert "currently visible browser" in prompt


def test_build_local_teacher_fallback_for_install_task_produces_python_first_chunks() -> None:
    teacher_result, teacher_plan = build_local_teacher_fallback(
        task="dbeaver를 설치해줘",
        prompt="dummy prompt",
        command_template="codex exec '{prompt}'",
        cwd="..",
        error="teacher quota exhausted",
        execution_style="python_first",
    )
    assert "external teacher was unavailable" in teacher_result.response_text
    assert len(teacher_plan.chunks) == 3
    assert "Python" in teacher_plan.chunks[0].agent_prompt
    assert "If one official page does not expose a raw `.exe` link" in teacher_plan.chunks[0].agent_prompt
    assert any(check["kind"] == "file_exists_glob" for check in teacher_plan.chunks[0].verification["checks"])
    assert any(check["kind"] == "file_exists_glob" for check in teacher_plan.chunks[1].verification["checks"])
    assert "First inspect the current screenshot and desktop state" in teacher_plan.chunks[1].agent_prompt
    assert "Do not download anything in this chunk." in teacher_plan.chunks[1].agent_prompt
    assert "Launching only the final app executable is not enough for this chunk." in teacher_plan.chunks[1].agent_prompt
    assert "Avoid recursively scanning the whole of `%LOCALAPPDATA%` or `%ProgramFiles%`." in teacher_plan.chunks[1].agent_prompt
    assert "%LOCALAPPDATA%\\\\Programs\\\\DBeaver" in teacher_plan.chunks[1].agent_prompt
    assert "Do not import `pywin32`, `pywinauto`, `win32gui`, `win32con`, `win32api`, or `pythoncom`." in teacher_plan.chunks[1].agent_prompt
    assert any(check["kind"] == "process_exists" for check in teacher_plan.chunks[2].verification["checks"])
    assert teacher_plan.chunks[1].max_retries == 2
    assert teacher_plan.chunks[2].max_retries == 2


def test_build_local_teacher_fallback_for_install_task_can_produce_gui_first_chunks() -> None:
    teacher_result, teacher_plan = build_local_teacher_fallback(
        task="dbeaver를 설치해줘",
        prompt="dummy prompt",
        command_template="codex exec '{prompt}'",
        cwd="..",
        error="teacher quota exhausted",
        execution_style="gui_first",
    )
    assert "GUI-first Windows automation" in teacher_result.response_text
    assert "현재 스크린샷에 브라우저" in teacher_plan.chunks[0].agent_prompt
    assert "drive that visible UI forward if present" in teacher_plan.chunks[1].agent_prompt
    assert "gui_first_download_chunk" in teacher_plan.chunks[0].notes
