import json
from unittest.mock import patch

from computer_use_training_generator.verification import (
    _expanded_glob_patterns,
    _has_file_based_checks,
    build_verification_code,
)
from computer_use_training_generator.teacher import (
    _decode_bing_result_url,
    _extract_bing_result_candidates,
    _extract_link_candidate_urls,
    _format_replan_link_candidate_exclusions,
    _looks_like_install_execution_chunk,
    _looks_like_plausible_official_page_url,
    _looks_like_store_detour_prompt,
    _merge_gui_first_navigation_chunks,
    _normalize_chunks,
    build_local_teacher_fallback,
    _normalize_general_gui_agent_prompt,
    _normalize_windows_installer_agent_prompt,
    _sanitize_discovered_official_urls,
    _select_official_page_urls,
    _simplify_windows_installer_glob,
    _task_staging_subdir,
    _target_installer_keywords,
)
from computer_use_training_generator.cli import (
    _attach_source_task_prompt_key,
    _collect_retry_link_request_exclusions,
    _compose_chunk_prompt,
    _compose_retry_prompt,
    _extract_verified_installer_paths,
    _extract_retry_exclusion_urls_and_queries,
    _initial_chunk_candidate_urls,
    _teacher_link_candidate_urls_from_result,
    _should_stop_after_install_completion,
    _teacher_execution_style_context,
)
from computer_use_training_generator.models import TeacherTaskChunk


def test_expanded_glob_patterns_adds_windows_setup_aliases() -> None:
    patterns = _expanded_glob_patterns("~/Downloads/dbeaver-ce-*-windows-x86_64.exe")
    assert "~/Downloads/dbeaver-ce-*-windows-x86_64.exe" in patterns
    assert "~/Downloads/dbeaver-ce-*-x86_64-setup.exe" in patterns
    assert "~/Downloads/dbeaver-ce-*-setup.exe" in patterns


def test_extract_link_candidate_urls_keeps_product_specific_pages() -> None:
    text = (
        '{"candidate_urls":['
        '"https://www.google.com/search?q=filezilla",'
        '"https://filezilla-project.org/download.php?type=client",'
        '"https://www.youtube.com/watch?v=bad",'
        '"https://filezilla.kr/download"'
        "]}"
    )

    urls = _extract_link_candidate_urls(text, task="filezilla 설치해줘", limit=5)

    assert "https://filezilla-project.org/download.php?type=client" in urls
    assert "https://filezilla.kr/download" in urls
    assert all("google." not in url and "youtube." not in url for url in urls)


def test_compose_retry_prompt_includes_teacher_candidate_urls() -> None:
    chunk = TeacherTaskChunk(
        chunk_id="chunk-001",
        title="Download installer",
        agent_prompt="Download the Windows installer into Downloads.",
        success_hint="installer exists",
        verification={"checks": [{"kind": "json_marker_valid_installer"}]},
        max_retries=1,
        on_fail="retry_current_chunk",
    )
    prompt = _compose_retry_prompt(
        chunk=chunk,
        verification_result={"passed": False, "evidence": []},
        attempt_index=1,
        execution_style="gui_first",
        candidate_urls=[
            "https://example.com/download",
            "https://downloads.example.com/app",
        ],
    )

    assert "Explicit open-target page URLs for this chunk" in prompt
    assert "- https://example.com/download" in prompt
    assert "Use the explicit open-target URLs listed above before generic search" in prompt


def test_compose_chunk_prompt_includes_explicit_open_targets() -> None:
    chunk = TeacherTaskChunk(
        chunk_id="chunk-001",
        title="Download installer",
        agent_prompt="Download the Windows installer into Downloads.",
        success_hint="installer exists",
        verification={"checks": [{"kind": "json_marker_valid_installer"}]},
        max_retries=1,
        on_fail="retry_current_chunk",
    )

    prompt = _compose_chunk_prompt(
        chunk,
        execution_style="gui_first",
        candidate_urls=["https://mydev.kr/", "https://mydev.kr/download.php"],
    )

    assert "Explicit open-target page URLs for this chunk" in prompt
    assert "- https://mydev.kr/" in prompt
    assert "primary runtime open targets" in prompt


def test_compose_chunk_prompt_includes_top_level_source_task() -> None:
    chunk = TeacherTaskChunk(
        chunk_id="chunk-001",
        title="Download installer",
        agent_prompt="Download the Windows installer into Downloads.",
        success_hint="installer exists",
        verification={"checks": [{"kind": "json_marker_valid_installer"}]},
        max_retries=1,
        on_fail="retry_current_chunk",
    )

    prompt = _compose_chunk_prompt(
        chunk,
        execution_style="gui_first",
        source_task="filezilla 설치해줘",
    )

    assert "Top-level source task for this run: filezilla 설치해줘" in prompt


def test_attach_source_task_prompt_key_updates_marker_checks() -> None:
    chunk = TeacherTaskChunk(
        chunk_id="chunk-001",
        title="Download installer",
        agent_prompt="Download the Windows installer into Downloads.",
        success_hint="installer exists",
        verification={
            "checks": [
                {"kind": "json_marker_valid_installer", "path": "~/Downloads/computer-use-agent-context.json"},
                {"kind": "path_exists", "path": "~/Downloads/app.exe"},
            ]
        },
        max_retries=1,
        on_fail="retry_current_chunk",
    )

    _attach_source_task_prompt_key([chunk], source_task="filezilla 설치해줘")

    checks = chunk.verification["checks"]
    assert checks[0]["prompt_key"]
    assert "prompt_key" not in checks[1]


def test_teacher_link_candidate_urls_from_result_parses_normalized_response() -> None:
    text = '{"candidate_urls":["https://example.com/download","ftp://bad"],"raw_response":"{}"}'

    assert _teacher_link_candidate_urls_from_result(text) == ["https://example.com/download"]


def test_format_replan_link_candidate_exclusions_lists_failed_urls_and_queries() -> None:
    text = _format_replan_link_candidate_exclusions(
        failed_candidate_urls=["https://mydev.kr/", "https://mydev.kr/download.php"],
        failed_search_queries=["메모잇 memoit193 kr", "메모잇 다운로드 windows"],
    )

    assert "Previously failed URLs to exclude" in text
    assert "- https://mydev.kr/" in text
    assert "Previously failed search queries to exclude" in text
    assert "- 메모잇 memoit193 kr" in text


def test_extract_retry_exclusion_urls_and_queries_filters_search_engine_urls() -> None:
    text = (
        "PROMPT_URL = 'https://mydev.kr/'\n"
        "FALLBACK_SEARCH_URL = "
        "'https://www.google.com/search?q=%EB%A9%94%EB%AA%A8%EC%9E%87+memoit193+kr'\n"
    )

    urls, queries = _extract_retry_exclusion_urls_and_queries(text)

    assert urls == ["https://mydev.kr/"]
    assert queries == ["메모잇 memoit193 kr"]


def test_collect_retry_link_request_exclusions_reads_prior_attempt_artifacts(tmp_path) -> None:
    session_root = tmp_path
    agent_runs = session_root / "agent_runs"
    agent_runs.mkdir(parents=True)
    (agent_runs / "chunk-001.attempt-01.json").write_text(
        json.dumps(
            {
                "executor_payload": {
                    "executed_python_code": (
                        "PROMPT_URL = 'https://mydev.kr/'\n"
                        "FALLBACK_SEARCH_URL = "
                        "'https://www.google.com/search?q=%EB%A9%94%EB%AA%A8%EC%9E%87+memoit193+kr'\n"
                    )
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (agent_runs / "chunk-001.attempt-01.teacher_link_candidates.json").write_text(
        json.dumps(
            {
                "candidate_urls": [
                    "https://mydev.kr/",
                    "https://mydev.kr/download.php",
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    urls, queries = _collect_retry_link_request_exclusions(
        session_root=session_root,
        retry_agent_run_label="chunk-001.attempt-02",
    )

    assert "https://mydev.kr/" in urls
    assert "https://mydev.kr/download.php" in urls
    assert queries == ["메모잇 memoit193 kr"]


def test_initial_chunk_candidate_urls_merges_task_and_teacher_urls() -> None:
    chunk = TeacherTaskChunk(
        chunk_id="chunk-001",
        title="Download installer",
        agent_prompt="Open the relevant download page and fetch the installer.",
        success_hint="installer exists",
        verification={"checks": [{"kind": "json_marker_valid_installer"}]},
        max_retries=1,
        on_fail="retry_current_chunk",
    )

    urls = _initial_chunk_candidate_urls(
        task="https://www.filezilla.kr/theme/filezilla/download/FileZilla_3.67.0_win64-setup.exe에서 filezilla 설치해줘",
        chunk=chunk,
        teacher_text="Use https://mydev.kr/ if the main vendor page is needed.",
        teacher_plan_source_text="",
    )

    assert urls == [
        "https://www.filezilla.kr/theme/filezilla/download/FileZilla_3.67.0_win64-setup.exe",
        "https://mydev.kr/",
    ]


def test_initial_chunk_candidate_urls_strip_trailing_backtick_from_teacher_url() -> None:
    chunk = TeacherTaskChunk(
        chunk_id="chunk-001",
        title="Download installer",
        agent_prompt="Open the relevant download page and fetch the installer.",
        success_hint="installer exists",
        verification={"checks": [{"kind": "json_marker_valid_installer"}]},
        max_retries=1,
        on_fail="retry_current_chunk",
    )

    urls = _initial_chunk_candidate_urls(
        task="메모잇 설치해줘",
        chunk=chunk,
        teacher_text="Use https://mydev.kr/` first.",
        teacher_plan_source_text="",
    )

    assert urls == ["https://mydev.kr/"]


def test_extract_verified_installer_paths_skips_keyword_mismatched_marker() -> None:
    result = {
        "passed": True,
        "evidence": [
            {
                "kind": "json_marker_valid_installer",
                "passed": True,
                "resolved_path": r"C:\Users\qkqxl\Downloads\DeskRest_n.exe",
                "keywords": ["filezilla"],
                "keyword_hits": [],
                "marker_keyword_hits": ["filezilla"],
            },
            {
                "kind": "json_marker_valid_installer",
                "passed": True,
                "resolved_path": r"C:\Users\qkqxl\Downloads\FileZilla_Setup.exe",
                "keywords": ["filezilla"],
                "keyword_hits": ["filezilla"],
            },
        ],
    }

    assert _extract_verified_installer_paths(result) == [r"C:\Users\qkqxl\Downloads\FileZilla_Setup.exe"]


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
    assert "~/Downloads/*dbeaver*.msi" in patterns


def test_expanded_glob_patterns_relaxes_exact_arch_installer_name() -> None:
    patterns = _expanded_glob_patterns("~/Downloads/DB.Browser.for.SQLite-v3.13.1-win64.msi")
    assert "~/Downloads/DB.Browser.for.SQLite-v3.13.1-win64.msi" in patterns
    assert "~/Downloads/DB.Browser.for.SQLite-v3.13.1-*.msi" in patterns


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
    assert _has_file_based_checks(
        {
            "checks": [
                {
                    "kind": "json_marker_valid_installer",
                    "path": "~/Downloads/computer-use-agent-context.json",
                    "field": "installer_path",
                }
            ]
        }
    )


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


def test_build_verification_code_supports_json_marker_valid_exe() -> None:
    code = build_verification_code(
        {
            "checks": [
                {
                    "kind": "json_marker_valid_exe",
                    "path": "~/Downloads/computer-use-agent/example/install-success.json",
                    "field": "installed_exe",
                    "keywords": ["kakaotalk"],
                }
            ]
        }
    )
    assert code is not None
    assert "_validate_json_marker_exe" in code
    assert "marker_target_keywords" in code
    exe_block = code.split("def _validate_json_marker_exe", 1)[1].split("def _fallback_installer_candidates", 1)[0]
    assert "min_bytes" not in exe_block


def test_build_verification_code_supports_json_marker_valid_installer() -> None:
    code = build_verification_code(
        {
            "checks": [
                {
                    "kind": "json_marker_valid_installer",
                    "path": "~/Downloads/computer-use-agent-context.json",
                    "field": "installer_path",
                    "keywords": ["targetapp"],
                    "bytes": 1000000,
                }
            ]
        }
    )
    assert code is not None
    assert "_validate_json_marker_installer" in code
    assert "_write_fallback_installer_path" in code
    assert "fallback_candidates = _fallback_installer_candidates(normalized_keywords, min_bytes, allowed_suffixes)" in code
    assert 'entry["fallback_used"] = True' in code
    assert 'entry["fallback_installer_rewritten"] = _write_fallback_installer_path(' in code
    assert 'entry["error"] = "fallback_installer_writeback_failed"' in code
    assert 'rewritten["prompt_key"]' not in code
    assert 'entry["error"] = "missing_field_value"' in code
    assert "source_url" in code
    installer_block = code.split("def _validate_json_marker_installer", 1)[1].split("evidence = []", 1)[0]
    assert "keyword_hits = [keyword for keyword in normalized_keywords if keyword in candidate_keyword_haystack]" in installer_block
    assert "marker_keyword_hits = [keyword for keyword in normalized_keywords if keyword in marker_keyword_haystack]" in installer_block
    assert "alias_keyword_hits" in installer_block
    assert "or (bool(marker_keyword_hits) and bool(alias_keyword_hits))" in installer_block
    assert 'entry["error"] = "installer_path_keyword_mismatch"' in installer_block
    assert "normalized_allowed_suffixes" in code


def test_build_verification_code_supports_archive_allowed_marker_installer() -> None:
    code = build_verification_code(
        {
            "checks": [
                {
                    "kind": "json_marker_valid_installer",
                    "path": "~/Downloads/computer-use-agent-context.json",
                    "field": "installer_path",
                    "keywords": ["mobaxterm"],
                    "bytes": 1000000,
                    "allowed_suffixes": [".zip", ".alz"],
                }
            ]
        }
    )
    assert code is not None
    assert "normalized_allowed_suffixes" in code
    assert "allowed_suffixes = check.get(\"allowed_suffixes\") or []" in code
    assert '".zip"' in code
    assert '".alz"' in code


def test_build_verification_code_does_not_accept_keyword_mismatched_fallback_installer() -> None:
    code = build_verification_code(
        {
            "checks": [
                {
                    "kind": "json_marker_valid_installer",
                    "path": "~/Downloads/computer-use-agent-context.json",
                    "field": "installer_path",
                    "keywords": ["mobaxterm"],
                    "bytes": 1000000,
                }
            ]
        }
    )
    assert code is not None
    assert "if normalized_keywords and not keyword_hits and not (marker_keyword_hits and alias_keyword_hits):" in code
    assert "keyword_ok = (" in code
    assert "or (bool(marker_keyword_hits) and bool(alias_keyword_hits))" in code


def test_build_verification_code_rejects_marker_keyword_when_installer_path_mismatches() -> None:
    code = build_verification_code(
        {
            "checks": [
                {
                    "kind": "json_marker_valid_installer",
                    "path": "~/Downloads/computer-use-agent-context.json",
                    "field": "installer_path",
                    "keywords": ["filezilla"],
                    "bytes": 1000000,
                }
            ]
        }
    )

    assert code is not None
    assert "marker_keyword_hits" in code
    assert "alias_keyword_hits" in code
    assert "installer_path_keyword_mismatch" in code


def test_build_verification_code_checks_marker_prompt_key() -> None:
    code = build_verification_code(
        {
            "checks": [
                {
                    "kind": "json_marker_valid_installer",
                    "path": "~/Downloads/computer-use-agent-context.json",
                    "field": "installer_path",
                    "keywords": ["filezilla"],
                    "prompt_key": "task-scope-123",
                }
            ]
        }
    )

    assert '"prompt_key": "task-scope-123"' in code
    assert "missing_prompt_key" in code
    assert "prompt_key_mismatch" in code
    assert 'entry["fallback_reason"] = prompt_mismatch_reason' in code


def test_install_execution_chunk_does_not_match_download_stage_prompt() -> None:
    prompt = (
        "Use executable Python on the Windows machine to download the official Windows installer `.exe` "
        "and save it to Downloads. If the download finishes, log the filename and confirm temporary "
        "download extensions disappeared."
    )
    assert _looks_like_install_execution_chunk("Download targetapp installer", prompt) is False


def test_simplify_windows_installer_glob_prefers_vendor_exe_glob() -> None:
    assert (
        _simplify_windows_installer_glob("~/Downloads/*dbeaver*win*installer*.exe")
        == "~/Downloads/*dbeaver*.exe"
    )


def test_simplify_windows_installer_glob_preserves_msi_extension() -> None:
    assert (
        _simplify_windows_installer_glob("~/Downloads/*sqlite*windows*installer*.msi")
        == "~/Downloads/*sqlite*.msi"
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
    assert "다른 관련 페이지도 확인" in prompt
    assert "absolute https .exe URL 후보" in prompt
    assert ".msi URL 후보" in prompt
    assert "작업과 일치하는 vendor, product, download 페이지를 우선 사용" in prompt
    assert "하드코딩된 버전 번호나 추측한 파일명으로 점프하지 말고" in prompt
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


def test_target_installer_keywords_filters_generic_english_words() -> None:
    keywords = _target_installer_keywords(
        "Using Python, download the official KakaoTalk Windows installer .exe from the official page.",
        limit=3,
    )
    assert "kakaotalk" in keywords
    assert "python" not in keywords
    assert "the" not in keywords


def test_target_installer_keywords_filters_for_from_db_browser_task() -> None:
    keywords = _target_installer_keywords("DB Browser for SQLite 프로그램을 설치해줘", limit=3)
    assert keywords == ["db", "browser", "sqlite"]
    assert "for" not in keywords


def test_local_install_marker_keywords_ignore_discovered_url_noise() -> None:
    _, teacher_plan = build_local_teacher_fallback(
        task="DB Browser for SQLite 프로그램을 설치해줘",
        prompt="dummy prompt",
        command_template="",
        cwd=None,
        error="teacher unavailable",
        execution_style="gui_first",
    )
    install_checks = teacher_plan.chunks[1].verification["checks"]
    marker_check = next(check for check in install_checks if check["kind"] == "json_marker_valid_exe")
    assert marker_check["keywords"] == ["db", "browser", "sqlite"]


def test_target_installer_keywords_filter_template_words_from_gui_first_prompt() -> None:
    keywords = _target_installer_keywords(
        "카카오톡 pc버전 프로그램을 설치해줘",
        "Open official download page",
        "Use Python-driven browser automation on the Windows desktop to open the official KakaoTalk service page and verify the downloaded installer.",
        limit=4,
    )
    assert "kakaotalk" in keywords
    assert "driven" not in keywords
    assert "automation" not in keywords
    assert "verify" not in keywords
    assert "downloaded" not in keywords
    assert "service" not in keywords


def test_target_installer_keywords_extract_korean_app_name() -> None:
    keywords = _target_installer_keywords("카카오톡 pc버전 프로그램을 설치해줘", limit=2)
    assert "카카오톡" in keywords
    assert "프로그램을" not in keywords
    assert "설치해줘" not in keywords


def test_target_installer_keywords_filter_download_command_suffixes() -> None:
    keywords = _target_installer_keywords("filezilla 설치파일을 다운로드해줘", limit=3)
    assert "filezilla" in keywords
    assert "다운로드해줘" not in keywords


def test_decode_bing_result_url_decodes_redirect_payload() -> None:
    redirected = (
        "https://www.bing.com/ck/a?!&&p=xxx&u="
        "a1aHR0cHM6Ly93d3cua2FrYW9jb3JwLmNvbS9wYWdlL3NlcnZpY2Uvc2VydmljZS9LYWthb1RhbGs_bGFuZz1rbw"
        "&ntb=1"
    )
    assert _decode_bing_result_url(redirected) == "https://www.kakaocorp.com/page/service/service/KakaoTalk?lang=ko"


def test_extract_bing_result_candidates_and_select_official_urls_prefer_corporate_host() -> None:
    sample_html = """
    <li class="b_algo">
      <h2 class=""><a href="https://www.bing.com/ck/a?!&&amp;p=1&amp;u=a1aHR0cHM6Ly9hcHBzLm1pY3Jvc29mdC5jb20vZGV0YWlsL3hwOWsxNzhsNWcwanEwP2hsPWtvLUtSJmdsPUNH&amp;ntb=1">카카오톡 - Windows에서 다운로드 및 설치 | Microsoft Store</a></h2>
      <p>Microsoft Store에서 카카오톡을 설치합니다.</p>
    </li>
    <li class="b_algo">
      <h2 class=""><a href="https://www.bing.com/ck/a?!&&amp;p=2&amp;u=a1aHR0cHM6Ly93d3cua2FrYW9jb3JwLmNvbS9wYWdlL3NlcnZpY2Uvc2VydmljZS9LYWthb1RhbGs_bGFuZz1rbw&amp;ntb=1">쓰는이에 집중. 쓰기좋게 맞춤. 카카오톡 | 카카오</a></h2>
      <p>카카오 공식 서비스 페이지입니다.</p>
    </li>
    <li class="b_algo">
      <h2 class=""><a href="https://pc-kakaocorp.com/">카카오톡 공식 웹사이트 - 카카오톡 공식 다운로드</a></h2>
      <p>공식 다운로드라고 주장하는 서드파티 페이지입니다.</p>
    </li>
    """
    candidates = _extract_bing_result_candidates(sample_html)
    urls = _select_official_page_urls("카카오톡 pc버전 프로그램을 설치해줘", candidates, limit=2)
    assert urls
    assert urls[0] == "https://www.kakaocorp.com/page/service/service/KakaoTalk?lang=ko"
    assert "pc-kakaocorp.com" not in urls[0]


def test_select_official_page_urls_rejects_unrelated_seo_results_without_task_keyword_hits() -> None:
    candidates = [
        {
            "url": "https://wellhealthorganics.com.in/",
            "title": "Well Health Organics",
            "snippet": "Health tips and organic lifestyle articles.",
        },
        {
            "url": "https://appexpress.ai/download/",
            "title": "메모잇 다운로드 | AppExpress",
            "snippet": "메모잇 공식 다운로드 페이지",
        },
    ]
    urls = _select_official_page_urls("메모잇 설치해줘", candidates, limit=2)
    assert urls == ["https://appexpress.ai/download/"]


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
    assert "핵심 키워드 2~5개" in prompt


def test_normalize_windows_installer_agent_prompt_keeps_download_chunk_as_download_when_text_mentions_finishing() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="카카오톡 pc버전 프로그램을 설치해줘",
        title="Download installer",
        agent_prompt=(
            "Use Python on Windows to open the official vendor page, find the Windows PC installer link, "
            "download the official `.exe` into Downloads, and confirm the download completed successfully before finishing."
        ),
        execution_style="gui_first",
    )
    assert "실행할 installer는" not in prompt
    assert "현재 스크린샷에 브라우저" in prompt
    assert "새 다운로드 helper나 URL 탐색 로직" not in prompt


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
    assert "Use screenshot-grounded page-content actions first" in prompt
    assert "avoid toolbar/address/tab/bookmark/blank-margin clicks" in prompt
    assert "do not ask a human to take manual GUI actions" in prompt


def test_should_stop_after_install_completion_for_plain_install_task() -> None:
    chunk = TeacherTaskChunk(
        chunk_id="chunk-002",
        title="Run installer",
        agent_prompt="Launch the downloaded official installer and complete setup with default options.",
        success_hint="The installer finishes and the app is installed on the machine.",
        preconditions=[],
        verification=None,
        max_retries=0,
        on_fail="fail_session",
        notes=[],
    )
    assert _should_stop_after_install_completion("filezilla 설치해줘", chunk) is True


def test_should_not_stop_after_download_or_explicit_launch_task() -> None:
    download_chunk = TeacherTaskChunk(
        chunk_id="chunk-001",
        title="Download installer",
        agent_prompt="Download installer into Downloads.",
        success_hint="The installer exists in Downloads.",
        preconditions=[],
        verification=None,
        max_retries=0,
        on_fail="fail_session",
        notes=[],
    )
    run_chunk = TeacherTaskChunk(
        chunk_id="chunk-002",
        title="Run installer",
        agent_prompt="Launch the downloaded official installer and complete setup.",
        success_hint="The installer finishes and the app is installed on the machine.",
        preconditions=[],
        verification=None,
        max_retries=0,
        on_fail="fail_session",
        notes=[],
    )
    assert _should_stop_after_install_completion("filezilla 설치해줘", download_chunk) is False
    assert _should_stop_after_install_completion("filezilla 설치하고 실행해줘", run_chunk) is False


def test_teacher_execution_style_context_blocks_store_detours() -> None:
    gui_context = _teacher_execution_style_context("gui_first")
    python_context = _teacher_execution_style_context("python_first")
    assert "do not route through Microsoft Store" in gui_context
    assert "winget" in gui_context
    assert "do not route through Microsoft Store" in python_context
    assert "Windows `.exe`/`.msi` installer when one is available" in python_context


def test_compose_retry_prompt_gui_first_keeps_same_page_and_download_only() -> None:
    prompt = _compose_retry_prompt(
        chunk=TeacherTaskChunk(
            chunk_id="chunk-001",
            title="Download Memoit installer",
            agent_prompt="공식 Memoit 설치파일만 다운로드하세요.",
            success_hint="The installer exists in Downloads.",
            preconditions=["Windows desktop session is available"],
            verification={"checks": [{"kind": "path_exists", "path": "~/Downloads/setup_memoit193.exe"}]},
            max_retries=1,
            on_fail="retry_current_chunk",
            notes=[],
        ),
        verification_result={
            "passed": False,
            "evidence": [{"kind": "path_exists", "path": "C:\\Users\\user\\Downloads\\setup_memoit193.exe", "exists": False}],
        },
        attempt_index=1,
        execution_style="gui_first",
    )
    assert "GUI-first retry rule: if the browser page for this chunk is already visible" in prompt
    assert "Do not guess a new direct installer URL" in prompt
    assert "Do not launch or silently install the installer in this chunk." in prompt
    assert "Do not use browser toolbar/address/tab/bookmark areas as click targets." in prompt
    assert "keep these exact task/product keywords in the query: memoit" in prompt.lower()
    assert "Do not replace those task/product keywords with generic retry wording" in prompt


def test_normalize_chunks_adds_file_size_check_for_windows_download_chunk() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Download installer exe",
                    "agent_prompt": "Open the official page and download the Windows installer `.exe` into Downloads.",
                    "verification": {
                        "checks": [
                            {"kind": "file_exists_glob", "pattern": "~/Downloads/*kakaotalk*.exe"},
                        ]
                    },
                }
            ]
        },
        source_task="카카오톡 pc버전 프로그램을 설치해줘",
        source_text="dummy",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    assert any(check["kind"] == "json_marker_valid_installer" for check in checks)
    assert not any(check["kind"] == "file_size_gt" for check in checks)


def test_normalize_chunks_adds_file_size_check_for_windows_msi_download_chunk() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Download installer msi",
                    "agent_prompt": "Open the official page and download the Windows installer `.msi` into Downloads.",
                    "verification": {
                        "checks": [
                            {"kind": "file_exists_glob", "pattern": "~/Downloads/*sqlite*.msi"},
                        ]
                    },
                }
            ]
        },
        source_task="DB Browser for SQLite 프로그램을 설치해줘",
        source_text="dummy",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    marker_check = next(check for check in checks if check["kind"] == "json_marker_valid_installer")
    assert marker_check["path"] == "~/Downloads/computer-use-agent-context.json"
    assert ".msi" in marker_check["allowed_suffixes"]


def test_normalize_chunks_keeps_download_chunk_verifier_as_download_checks() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "공식 설치 파일 다운로드",
                    "agent_prompt": (
                        "Windows 데스크톱에서 Python을 사용해 공식 설치 페이지를 연 뒤, "
                        "`https://vendor.example/download`에서 `Windows (Installer)` 링크를 찾아 "
                        "`.exe` 설치 파일만 다운로드하세요. 파일은 `Downloads` 폴더에 저장하고, "
                        "다운로드가 끝나면 임시 확장자가 남아 있지 않은지 확인하세요."
                    ),
                    "verification": {
                        "checks": [
                            {"kind": "file_exists_glob", "pattern": "~/Downloads/dbeaver*.exe"},
                            {"kind": "file_size_gt", "pattern": "~/Downloads/dbeaver*.exe", "bytes": 1000000},
                        ]
                    },
                }
            ]
        },
        source_task="dbeaver 프로그램을 설치해줘",
        source_text="dummy",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    assert any(check["kind"] == "json_marker_valid_installer" for check in checks)
    assert not any(check["kind"] == "file_exists_glob" for check in checks)
    assert not any(check["kind"] == "file_size_gt" for check in checks)
    assert not any(check["kind"] == "json_marker_valid_exe" for check in checks)
    assert not any(check.get("path") == "~/Downloads/install-success.json" for check in checks)


def test_normalize_chunks_removes_optional_checksum_chunk_for_install_task() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Download installer",
                    "agent_prompt": "Download the official Windows installer `.msi` into Downloads.",
                    "verification": {"checks": [{"kind": "file_exists_glob", "pattern": "~/Downloads/*sqlite*.msi"}]},
                },
                {
                    "chunk_id": "chunk-002",
                    "title": "Verify installer checksum",
                    "agent_prompt": "Download SHA256SUMS.txt and verify the MSI checksum.",
                    "verification": {"checks": [{"kind": "file_exists_glob", "pattern": "~/Downloads/SHA256SUMS.txt"}]},
                },
                {
                    "chunk_id": "chunk-003",
                    "title": "Install app",
                    "agent_prompt": "Run the downloaded Windows installer `.msi` with msiexec and finish installation.",
                    "preconditions": ["The MSI has been downloaded and checksum-verified"],
                    "verification": {"checks": [{"kind": "path_exists", "path": "C:\\Program Files\\TargetApp"}]},
                },
            ]
        },
        source_task="DB Browser for SQLite 프로그램을 설치해줘",
        source_text="dummy",
        execution_style="gui_first",
    )
    assert [chunk.chunk_id for chunk in chunks] == ["chunk-001", "chunk-003"]
    assert all("checksum" not in " ".join(chunk.preconditions).lower() for chunk in chunks)
    assert "optional_checksum_chunk_removed" in chunks[-1].notes


def test_normalize_chunks_replaces_msi_install_path_alternatives_with_marker_verification() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-002",
                    "title": "Run Installer",
                    "agent_prompt": (
                        "Launch the downloaded target app MSI from Downloads using Python automation on the Windows machine. "
                        "Complete the setup flow by following the standard installer dialogs."
                    ),
                    "verification": {
                        "checks": [
                            {"kind": "path_exists", "path": "~/AppData/Local/Programs/Target App"},
                            {"kind": "path_exists", "path": "C:/Program Files/Target App"},
                            {"kind": "path_exists", "path": "C:/Program Files (x86)/Target App"},
                        ]
                    },
                }
            ]
        },
        source_task="Target App 프로그램을 설치해줘",
        source_text="dummy",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    assert any(check["kind"] == "path_exists" and check["path"] == "~/Downloads/install-success.json" for check in checks)
    assert any(check["kind"] == "json_marker_valid_exe" for check in checks)
    assert not any(str(check.get("path", "")).startswith("C:/Program Files") for check in checks)


def test_normalize_chunks_replaces_start_menu_process_install_verifier_with_marker() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-003",
                    "title": "Run installer and verify app launch",
                    "agent_prompt": (
                        "Use Python to launch the installer executable found in the previous step and proceed through "
                        "the installer with default choices. After installation finishes, verify the app is installed "
                        "by checking for its program files entry or Start Menu shortcut, then launch it once."
                    ),
                    "verification": {
                        "checks": [
                            {"kind": "path_exists", "path": "~/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/TargetApp.lnk"},
                            {"kind": "process_exists", "name": "TargetApp.exe"},
                        ]
                    },
                }
            ]
        },
        source_task="TargetApp 프로그램을 설치해줘",
        source_text="dummy",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    assert any(check["kind"] == "path_exists" and check["path"] == "~/Downloads/install-success.json" for check in checks)
    assert any(check["kind"] == "json_marker_valid_exe" for check in checks)
    assert not any(check.get("path") == "~/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/TargetApp.lnk" for check in checks)


def test_normalize_chunks_keeps_checksum_chunk_when_task_requests_checksum() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Verify installer checksum",
                    "agent_prompt": "Download SHA256SUMS.txt and verify the MSI checksum.",
                    "verification": {"checks": [{"kind": "file_exists_glob", "pattern": "~/Downloads/SHA256SUMS.txt"}]},
                },
            ]
        },
        source_task="DB Browser for SQLite 프로그램을 설치하고 checksum도 검증해줘",
        source_text="dummy",
        execution_style="gui_first",
    )
    assert [chunk.chunk_id for chunk in chunks] == ["chunk-001"]


def test_merge_gui_first_navigation_chunks_folds_page_open_into_download() -> None:
    chunks = _merge_gui_first_navigation_chunks(
        [
            TeacherTaskChunk(
                chunk_id="chunk-001",
                title="Open official page",
                agent_prompt="Use Python to open a browser and navigate to the official download page, then identify the installer link.",
                preconditions=["Browser is available."],
                verification={"checks": [{"kind": "path_exists", "path": "~/Downloads"}]},
                max_retries=1,
                on_fail="retry_current_chunk",
                notes=[],
            ),
            TeacherTaskChunk(
                chunk_id="chunk-002",
                title="Download installer exe",
                agent_prompt="Download the official Windows installer `.exe` into Downloads.",
                preconditions=["Official page is reachable."],
                verification={
                    "checks": [
                        {"kind": "file_exists_glob", "pattern": "~/Downloads/*targetapp*.exe"},
                        {"kind": "file_size_gt", "pattern": "~/Downloads/*targetapp*.exe", "bytes": 1000000},
                    ]
                },
                max_retries=1,
                on_fail="retry_current_chunk",
                notes=[],
            ),
        ]
    )
    assert len(chunks) == 1
    assert "Navigation stage to preserve" in chunks[0].agent_prompt
    assert "Download stage" in chunks[0].agent_prompt
    assert "merged_prior_navigation_chunk" in chunks[0].notes


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
    assert "If one page does not expose a raw `.exe` link" in teacher_plan.chunks[0].agent_prompt
    assert any(check["kind"] == "file_exists_glob" for check in teacher_plan.chunks[0].verification["checks"])
    assert any(check["kind"] == "path_exists" for check in teacher_plan.chunks[1].verification["checks"])
    assert any(check["kind"] == "json_marker_valid_exe" for check in teacher_plan.chunks[1].verification["checks"])
    assert "First inspect the current screenshot and desktop state" in teacher_plan.chunks[1].agent_prompt
    assert "Do not download anything in this chunk." in teacher_plan.chunks[1].agent_prompt
    assert "Launching only the final app executable is not enough for this chunk." in teacher_plan.chunks[1].agent_prompt
    assert "Avoid recursively scanning the whole of `%LOCALAPPDATA%` or `%ProgramFiles%`." in teacher_plan.chunks[1].agent_prompt
    assert "%LOCALAPPDATA%\\\\Programs\\\\dbeaver" in teacher_plan.chunks[1].agent_prompt
    assert "Do not import `pywin32`, `pywinauto`, `win32gui`, `win32con`, `win32api`, or `pythoncom`." in teacher_plan.chunks[1].agent_prompt
    assert any(check["kind"] == "path_exists" for check in teacher_plan.chunks[2].verification["checks"])
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


def test_build_local_teacher_fallback_for_korean_task_uses_real_app_token() -> None:
    _, teacher_plan = build_local_teacher_fallback(
        task="카카오톡 pc버전 프로그램을 설치해줘",
        prompt="dummy prompt",
        command_template="codex exec '{prompt}'",
        cwd="..",
        error="teacher quota exhausted",
        execution_style="gui_first",
    )
    download_chunk = teacher_plan.chunks[0]
    assert "Targetapp" not in download_chunk.agent_prompt
    assert "카카오톡" in download_chunk.agent_prompt
    assert "computer-use-agent-context.json" in download_chunk.agent_prompt
    checks = download_chunk.verification["checks"]
    download_marker_check = next(check for check in checks if check["kind"] == "json_marker_valid_installer")
    assert download_marker_check["path"] == "~/Downloads/computer-use-agent-context.json"
    assert "카카오톡" in download_marker_check["keywords"]
    install_checks = teacher_plan.chunks[1].verification["checks"]
    marker_check = next(check for check in install_checks if check["kind"] == "json_marker_valid_exe")
    assert "카카오톡" in marker_check["keywords"]


def test_build_local_teacher_fallback_adds_ascii_alias_keywords_from_prompt_context() -> None:
    _, teacher_plan = build_local_teacher_fallback(
        task="setup_memoit193.exe를 내려받아 메모잇 프로그램을 설치해줘",
        prompt="dummy prompt",
        command_template="codex exec '{prompt}'",
        cwd="..",
        error="teacher quota exhausted",
        execution_style="gui_first",
    )
    download_chunk = teacher_plan.chunks[0]
    checks = download_chunk.verification["checks"]
    download_marker_check = next(check for check in checks if check["kind"] == "json_marker_valid_installer")

    assert "메모잇" in download_marker_check["keywords"]
    assert any(keyword.startswith("memoit") for keyword in download_marker_check["keywords"])


def test_task_staging_subdir_changes_with_session_salt() -> None:
    task = "카카오톡 pc버전 프로그램을 설치해줘"
    first = _task_staging_subdir(task, salt="session-a")
    second = _task_staging_subdir(task, salt="session-b")
    assert first != second
    assert first
    assert second


def test_build_local_teacher_fallback_uses_supplied_staging_subdir() -> None:
    _, teacher_plan = build_local_teacher_fallback(
        task="카카오톡 pc버전 프로그램을 설치해줘",
        prompt="dummy prompt",
        command_template="codex exec '{prompt}'",
        cwd="..",
        error="teacher quota exhausted",
        execution_style="gui_first",
        staging_subdir="custom-stage-1234",
    )
    download_chunk = teacher_plan.chunks[0]
    assert "computer-use-agent-context.json" in download_chunk.agent_prompt
    assert "custom-stage-1234" not in download_chunk.agent_prompt
    marker_check = next(check for check in download_chunk.verification["checks"] if check["kind"] == "json_marker_valid_installer")
    assert marker_check["path"] == "~/Downloads/computer-use-agent-context.json"
    assert "카카오톡" in marker_check["keywords"]


def test_build_local_teacher_fallback_does_not_inject_discovered_urls_without_user_url() -> None:
    with patch(
        "computer_use_training_generator.teacher._discover_official_page_urls",
        return_value=[
            "https://www.kakaocorp.com/page/service/service/KakaoTalk?lang=ko",
            "https://apps.microsoft.com/detail/xp9k178l5g0jq0?hl=ko-KR&gl=CG",
        ],
    ) as discover_mock:
        _, teacher_plan = build_local_teacher_fallback(
            task="카카오톡 pc버전 프로그램을 설치해줘",
            prompt="dummy prompt",
            command_template="codex exec '{prompt}'",
            cwd="..",
            error="teacher quota exhausted",
            execution_style="gui_first",
    )
    download_chunk = teacher_plan.chunks[0]
    discover_mock.assert_not_called()
    assert "Use these exact official page URLs first before any search engine result or inferred domain:" not in download_chunk.agent_prompt
    assert "https://www.kakaocorp.com/page/service/service/KakaoTalk?lang=ko" not in download_chunk.agent_prompt
    install_marker_check = next(
        check for check in teacher_plan.chunks[1].verification["checks"] if check["kind"] == "json_marker_valid_exe"
    )
    assert "카카오톡" in install_marker_check["keywords"]


def test_build_local_teacher_fallback_keeps_user_provided_url_hint() -> None:
    _, teacher_plan = build_local_teacher_fallback(
        task="카카오톡 pc버전 프로그램을 설치해줘 https://www.kakaocorp.com/page/service/service/KakaoTalk?lang=ko",
        prompt="dummy prompt",
        command_template="codex exec '{prompt}'",
        cwd="..",
        error="teacher quota exhausted",
        execution_style="gui_first",
    )
    download_chunk = teacher_plan.chunks[0]
    assert "Use these exact page URLs first before any search engine result or inferred domain:" in download_chunk.agent_prompt
    assert "https://www.kakaocorp.com/page/service/service/KakaoTalk?lang=ko" in download_chunk.agent_prompt


def test_normalize_windows_installer_agent_prompt_removes_teacher_invented_url() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="filezilla 설치해줘",
        title="Download FileZilla installer",
        agent_prompt=(
            "Open a browser and go to the official FileZilla Client download page at "
            "`https://filezilla-project.org/download.php?type=client`, then download the installer."
        ),
        source_text="Teacher answer mentioned https://filezilla-project.org/download.php?type=client",
        execution_style="gui_first",
    )

    assert "filezilla-project.org" not in prompt
    assert "a relevant product/download page" in prompt


def test_normalize_windows_installer_agent_prompt_strips_url_added_by_followup_hints() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="filezilla 설치해줘",
        title="Download FileZilla installer",
        agent_prompt=(
            "Open a browser and go to the official FileZilla Client download page at "
            "`https://filezilla.run/`, then download the installer."
        ),
        source_text="Teacher answer mentioned https://filezilla.run/ and retry from there.",
        execution_style="gui_first",
    )

    assert "filezilla.run" not in prompt
    assert "a relevant product/download page" in prompt


def test_plausible_official_page_url_filters_blog_like_article_pages() -> None:
    assert _looks_like_plausible_official_page_url("https://moneyroan.com/desktop-notepad-memo-it/") is False
    assert (
        _looks_like_plausible_official_page_url(
            "https://inoboard.com/%EB%A9%94%EB%AA%A8%EC%9E%87-%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-%EB%B0%8F-%EC%82%AC%EC%9A%A9%EB%B2%95/"
        )
        is False
    )
    assert _looks_like_plausible_official_page_url("https://dictionary.cambridge.org/vi/translate/") is False
    assert _looks_like_plausible_official_page_url("https://www.bookize.com/vi/translate/") is False
    assert _looks_like_plausible_official_page_url("https://www.kakaocorp.com/page/service/service/KakaoTalk?lang=ko") is True


def test_sanitize_discovered_official_urls_drops_blog_like_candidates() -> None:
    urls = _sanitize_discovered_official_urls(
        "메모잇 프로그램을 설치해줘",
        [
            "https://moneyroan.com/desktop-notepad-memo-it/",
            "https://inoboard.com/%EB%A9%94%EB%AA%A8%EC%9E%87-%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-%EB%B0%8F-%EC%82%AC%EC%9A%A9%EB%B2%95/",
        ],
    )
    assert urls == []


def test_sanitize_discovered_official_urls_drops_reference_like_candidates() -> None:
    urls = _sanitize_discovered_official_urls(
        "메모잇 프로그램을 설치해줘",
        [
            "https://dictionary.cambridge.org/vi/translate/",
            "https://www.bookize.com/vi/translate/",
        ],
    )
    assert urls == []


def test_build_local_teacher_fallback_does_not_use_blog_host_for_download_glob_or_exact_urls() -> None:
    with patch(
        "computer_use_training_generator.teacher._discover_official_page_urls",
        return_value=[
            "https://moneyroan.com/desktop-notepad-memo-it/",
            "https://inoboard.com/%EB%A9%94%EB%AA%A8%EC%9E%87-%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-%EB%B0%8F-%EC%82%AC%EC%9A%A9%EB%B2%95/",
        ],
    ):
        _, teacher_plan = build_local_teacher_fallback(
            task="메모잇 프로그램을 설치해줘",
            prompt="dummy prompt",
            command_template="codex exec '{prompt}'",
            cwd="..",
            error="teacher quota exhausted",
            execution_style="gui_first",
        )
    download_chunk = teacher_plan.chunks[0]
    assert "moneyroan.com" not in download_chunk.agent_prompt
    assert "inoboard.com" not in download_chunk.agent_prompt
    checks = download_chunk.verification["checks"]
    assert not any("moneyroan" in check.get("pattern", "") for check in checks if check["kind"] == "file_exists_glob")


def test_build_local_teacher_fallback_does_not_use_reference_host_for_download_glob_or_exact_urls() -> None:
    with patch(
        "computer_use_training_generator.teacher._discover_official_page_urls",
        return_value=[
            "https://dictionary.cambridge.org/vi/translate/",
            "https://www.bookize.com/vi/translate/",
        ],
    ):
        _, teacher_plan = build_local_teacher_fallback(
            task="메모잇 프로그램을 설치해줘",
            prompt="dummy prompt",
            command_template="codex exec '{prompt}'",
            cwd="..",
            error="teacher quota exhausted",
            execution_style="gui_first",
        )
    download_chunk = teacher_plan.chunks[0]
    assert "dictionary.cambridge.org" not in download_chunk.agent_prompt
    assert "bookize.com" not in download_chunk.agent_prompt
    checks = download_chunk.verification["checks"]
    assert not any("dictionary" in check.get("pattern", "") for check in checks if check["kind"] == "file_exists_glob")


def test_build_local_teacher_fallback_uses_context_marker_for_gui_first_download() -> None:
    with patch(
        "computer_use_training_generator.teacher._discover_official_page_urls",
        return_value=[
            "http://mydev.kr/",
            "https://memoit.filei.co.kr/",
        ],
    ):
        _, teacher_plan = build_local_teacher_fallback(
            task="메모잇 프로그램을 설치해줘",
            prompt="dummy prompt",
            command_template="codex exec '{prompt}'",
            cwd="..",
            error="teacher quota exhausted",
            execution_style="gui_first",
        )
    download_chunk = teacher_plan.chunks[0]
    checks = download_chunk.verification["checks"]
    marker_checks = [check for check in checks if check["kind"] == "json_marker_valid_installer"]
    assert marker_checks
    assert marker_checks[0]["path"] == "~/Downloads/computer-use-agent-context.json"
    assert "메모잇" in marker_checks[0]["keywords"]


def test_build_local_teacher_fallback_does_not_keep_discovered_generic_vendor_url_without_user_url() -> None:
    with patch(
        "computer_use_training_generator.teacher._discover_official_page_urls",
        return_value=[
            "https://appexpress.ai/download/",
        ],
    ) as discover_mock:
        _, teacher_plan = build_local_teacher_fallback(
            task="메모잇 설치해줘",
            prompt="dummy prompt",
            command_template="codex exec '{prompt}'",
            cwd="..",
            error="teacher quota exhausted",
            execution_style="gui_first",
        )
    download_chunk = teacher_plan.chunks[0]
    discover_mock.assert_not_called()
    assert "https://appexpress.ai/download/" not in download_chunk.agent_prompt


def test_normalize_chunks_replaces_store_detour_plan_for_windows_installer_task() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Open store listing",
                    "agent_prompt": (
                        "Open the Microsoft Store listing at "
                        "https://apps.microsoft.com/detail/example and prepare installation."
                    ),
                    "verification": None,
                }
            ]
        },
        source_task="어떤 프로그램을 설치해줘",
        source_text="teacher text",
        execution_style="gui_first",
    )
    assert len(chunks) == 3
    assert any("다운로드" in chunk.agent_prompt or "download" in chunk.agent_prompt.lower() for chunk in chunks)
    assert not any("apps.microsoft.com" in chunk.agent_prompt.lower() for chunk in chunks)


def test_negative_store_warning_does_not_trigger_store_detour_replacement() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Download installer",
                    "agent_prompt": (
                        "Open the official download page at https://mobaxterm.mobatek.net/download-home-edition.html. "
                        "If the page only provides an archive package, download that exact official package and do not substitute "
                        "a third-party source or Microsoft Store."
                    ),
                    "verification": {
                        "checks": [
                            {"kind": "file_exists_glob", "pattern": "~/Downloads/MobaXterm*"},
                        ]
                    },
                }
            ]
        },
        source_task="mobaxterm을 설치해줘",
        source_text=(
            "Use the official MobaXterm Home Edition download page at "
            "https://mobaxterm.mobatek.net/download-home-edition.html and do not substitute a third-party source or Microsoft Store."
        ),
        execution_style="gui_first",
    )
    assert len(chunks) == 1
    assert "mobaxterm.mobatek.net/download-home-edition.html" not in chunks[0].agent_prompt
    assert "Microsoft Store" in chunks[0].agent_prompt
    assert not any("local_teacher_fallback" in note for note in chunks[0].notes)


def test_looks_like_store_detour_prompt_ignores_negative_store_warning() -> None:
    assert _looks_like_store_detour_prompt(
        "Use the official vendor page and do not substitute a third-party source or Microsoft Store."
    ) is False
    assert _looks_like_store_detour_prompt(
        "Open the Microsoft Store listing at https://apps.microsoft.com/detail/example."
    ) is True


def test_normalize_chunks_keeps_store_plan_when_task_explicitly_requests_store() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Open store listing",
                    "agent_prompt": "Open the Microsoft Store listing at https://apps.microsoft.com/detail/example.",
                    "verification": None,
                }
            ]
        },
        source_task="microsoft store에서 어떤 프로그램을 설치해줘",
        source_text="teacher text",
        execution_style="gui_first",
    )
    assert len(chunks) == 1
    assert "apps.microsoft.com" in chunks[0].agent_prompt.lower()


def test_normalize_windows_installer_agent_prompt_keeps_official_archive_exception() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="mobaxterm을 설치해줘",
        title="Download MobaXterm installer",
        agent_prompt=(
            "Open the official MobaXterm Home Edition download page and download the Windows installer package into Downloads. "
            "Wait until the download is fully complete before finishing this chunk."
        ),
        source_text=(
            "Use the official MobaXterm Home Edition download page at "
            "https://mobaxterm.mobatek.net/download-home-edition.html. "
            "If the downloaded package is a `.zip`, extract it in Downloads and run the contained installer."
        ),
        execution_style="gui_first",
    )
    assert "mobaxterm.mobatek.net/download-home-edition.html" not in prompt
    assert "exact official archive" in prompt or "`.zip` 또는 `.alz`" in prompt
    assert "Do not use `.zip`, portable, or archive downloads." not in prompt


def test_normalize_windows_installer_agent_prompt_install_chunk_always_branches_archive_suffix() -> None:
    prompt = _normalize_windows_installer_agent_prompt(
        source_task="mobaxterm 프로그램을 설치해줘",
        title="Install MobaXterm",
        agent_prompt=(
            "Use Python to locate the downloaded MobaXterm installer in Downloads, execute it, "
            "and drive the installer wizard to completion."
        ),
        source_text="Use the official download page and install with default options.",
        execution_style="gui_first",
    )

    assert "선택한 installer artifact가 `.zip` 또는 `.alz`이면 먼저 Downloads 안에서 그 archive를 추출하고" in prompt
    assert "추출된 폴더 안의 실제 installer `.exe` 또는 `.msi`를 다시 선택해서 그 파일을 실행하세요." in prompt


def test_normalize_chunks_rewrites_wildcard_path_exists_download_verifier_to_glob() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Download MobaXterm installer",
                    "agent_prompt": (
                        "Open the official page and download the Windows installer package into Downloads. "
                        "If the page only provides an archive package, download that exact official package."
                    ),
                    "verification": {
                        "checks": [
                            {"kind": "path_exists", "path": "~/Downloads/MobaXterm*"},
                        ]
                    },
                }
            ]
        },
        source_task="mobaxterm을 설치해줘",
        source_text="Use the official archive package if the page only provides a .zip installer package.",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    marker_check = next(check for check in checks if check["kind"] == "json_marker_valid_installer")
    assert marker_check["path"] == "~/Downloads/computer-use-agent-context.json"
    assert ".zip" in marker_check["allowed_suffixes"]


def test_normalize_chunks_prioritizes_install_marker_verifier_for_installer_run_prompt() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "title": "Run KakaoTalk installer",
                    "agent_prompt": (
                        "Use Python to launch the KakaoTalk installer `.exe` that was downloaded in `~/Downloads`, "
                        "then drive the Windows installer wizard to completion with default options. "
                        "If a Windows UAC or permission prompt appears, approve it so the installation can proceed."
                    ),
                    "verification": {
                        "checks": [
                            {"kind": "path_exists", "path": "C:/Program Files/KakaoTalk"},
                            {"kind": "process_exists", "name": "KakaoTalk.exe"},
                        ]
                    },
                }
            ]
        },
        source_task="카카오톡 pc버전 프로그램을 설치해줘",
        source_text="teacher text",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    assert checks[0]["path"] == "~/Downloads/install-success.json"
    assert checks[1]["kind"] == "json_marker_valid_exe"
    assert checks[1]["field"] == "installed_exe"


def test_normalize_chunks_replaces_direct_exe_json_marker_with_launch_marker_verification() -> None:
    chunks = _normalize_chunks(
        {
            "chunks": [
                {
                    "chunk_id": "chunk-003",
                    "title": "Confirm TargetApp is installed",
                    "agent_prompt": (
                        "Launch the installed app once, verify it reaches the foreground, "
                        "and leave a launch marker after it starts."
                    ),
                    "verification": {
                        "checks": [
                            {
                                "kind": "json_marker_valid_exe",
                                "path": "C:/Program Files/TargetApp/TargetApp.exe",
                            }
                        ]
                    },
                }
            ]
        },
        source_task="TargetApp 프로그램을 설치해줘",
        source_text="teacher text",
        execution_style="gui_first",
    )
    checks = chunks[0].verification["checks"]
    assert checks[0] == {"kind": "path_exists", "path": "~/Downloads/launch-success.json"}
    assert checks[1]["kind"] == "json_marker_valid_exe"
    assert checks[1]["path"] == "~/Downloads/launch-success.json"
    assert checks[1]["field"] == "launched_exe"
