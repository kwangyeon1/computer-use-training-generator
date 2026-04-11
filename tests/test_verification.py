from computer_use_training_generator.verification import (
    _expanded_glob_patterns,
    _has_file_based_checks,
    build_verification_code,
)
from computer_use_training_generator.teacher import (
    _normalize_general_gui_agent_prompt,
    _normalize_windows_installer_agent_prompt,
    _simplify_windows_installer_glob,
)


def test_expanded_glob_patterns_adds_windows_setup_aliases() -> None:
    patterns = _expanded_glob_patterns("~/Downloads/dbeaver-ce-*-windows-x86_64.exe")
    assert "~/Downloads/dbeaver-ce-*-windows-x86_64.exe" in patterns
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
        title="Download installer",
        agent_prompt="브라우저에서 공식 다운로드 페이지를 열고 Windows용 `.exe`를 다운로드하세요.",
    )
    assert "이미 Downloads 폴더에 사용할 수 있는 Windows installer" in prompt


def test_normalize_general_gui_agent_prompt_adds_continue_hint() -> None:
    prompt = _normalize_general_gui_agent_prompt(
        title="Create Eclipse project",
        agent_prompt="Eclipse에서 새 Java 프로젝트를 생성하고 기본 프로젝트 창이 보이게 하세요.",
    )
    assert "현재 앱이나 창이 이미 열려 있으면" in prompt
