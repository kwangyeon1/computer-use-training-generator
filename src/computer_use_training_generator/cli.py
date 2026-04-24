from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse

from .agent import bootstrap_agent, run_agent_prompt
from .collector import (
    append_run_artifacts,
    collect_run_artifacts,
    make_session_id,
    prepare_session_root,
    write_agent_invocation_payload,
    write_session_manifest,
    write_teacher_bundle,
)
from .config_utils import load_generator_config
from .models import TeacherChunkPlanResult, TeacherTaskChunk
from .teacher import (
    _extract_link_candidate_urls,
    _target_installer_keywords,
    _task_staging_subdir,
    build_local_teacher_fallback,
    run_teacher_link_candidates,
    run_teacher,
    split_teacher_response,
)
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


def _normalize_execution_style(value: str | None) -> str:
    normalized = str(value or "python_first").strip().lower().replace("-", "_")
    if normalized == "gui":
        normalized = "gui_first"
    if normalized not in {"python_first", "gui_first"}:
        normalized = "python_first"
    return normalized


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
    _override(config, "execution_style", getattr(args, "execution_style", None))
    if getattr(args, "teacher_split_enabled", False):
        config["teacher_split_enabled"] = True
    if getattr(args, "agent_reasoning_enabled", False):
        config["agent_reasoning_enabled"] = True
    if getattr(args, "chunk_verification_enabled", False):
        config["chunk_verification_enabled"] = True
    if getattr(args, "output_dir", None) is not None:
        config["output_dir"] = str(Path(args.output_dir).resolve())
    config["execution_style"] = _normalize_execution_style(config.get("execution_style"))
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

def _normalize_http_candidate_urls(urls: list[str] | None, *, limit: int = 5) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_url in urls or []:
        url = _strip_trailing_korean_particle_from_url_candidate(str(raw_url or "").strip().rstrip(".,)"))
        if not url.startswith(("http://", "https://")):
            continue
        lowered = url.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(url)
        if len(cleaned) >= limit:
            break
    return cleaned


def _strip_trailing_korean_particle_from_url_candidate(candidate: str) -> str:
    korean_particle_suffixes = (
        "으로는",
        "에서는",
        "에게는",
        "한테는",
        "으로",
        "에서",
        "에게",
        "한테",
        "까지",
        "부터",
        "보다",
        "처럼",
        "라고",
        "이라",
        "라도",
        "이다",
        "은",
        "는",
        "이",
        "가",
        "을",
        "를",
        "에",
        "와",
        "과",
        "도",
    )
    cleaned_candidate = str(candidate or "").strip()
    for suffix in korean_particle_suffixes:
        if not cleaned_candidate.endswith(suffix):
            continue
        stripped = cleaned_candidate[: -len(suffix)]
        if not stripped:
            continue
        if re.search(r"[가-힣]$", stripped):
            continue
        try:
            parsed = urllib.parse.urlparse(stripped)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return stripped
    return cleaned_candidate


def _extract_http_urls(text: str, *, limit: int = 5) -> list[str]:
    return _normalize_http_candidate_urls(
        re.findall(r"https?://[^\s\"'<>]+", str(text or "")),
        limit=limit,
    )


def _compose_explicit_open_target_block(candidate_urls: list[str] | None) -> str:
    normalized = _normalize_http_candidate_urls(candidate_urls)
    if not normalized:
        return ""
    url_lines = "\n".join(f"- {url}" for url in normalized)
    return (
        "Explicit open-target page URLs for this chunk. Treat these exact URLs as the primary runtime open targets "
        "before any generic search, and open them in order:\n"
        f"{url_lines}"
    )


def _compose_chunk_prompt(
    chunk: TeacherTaskChunk,
    *,
    execution_style: str = "python_first",
    candidate_urls: list[str] | None = None,
) -> str:
    normalized_style = _normalize_execution_style(execution_style)
    if normalized_style == "gui_first":
        prefix = (
            "Return executable Python only for this chunk. Continue from the currently visible browser, download UI, "
            "app window, or installer dialog when that state is already on screen. Use screenshot-grounded page-content "
            "actions first, avoid toolbar/address/tab/bookmark/blank-margin clicks, and do not ask a human to take manual GUI actions."
        )
    else:
        prefix = (
            "Return executable Python only for this chunk. Do not ask a human to perform manual GUI actions "
            "outside the generated Python."
        )
    parts = [prefix, _compose_explicit_open_target_block(candidate_urls), chunk.agent_prompt.strip()]
    if chunk.success_hint:
        parts.append(f"Current chunk success target: {chunk.success_hint}")
    if chunk.preconditions:
        parts.append("Preconditions expected before or during this chunk:\n- " + "\n- ".join(chunk.preconditions))
    parts.append("Do only this chunk. Do not skip ahead to later chunks.")
    return "\n\n".join(part for part in parts if part)


def _extract_verified_installer_paths(verification_result: dict | None) -> list[str]:
    if not verification_result:
        return []
    evidence = verification_result.get("evidence") or []
    results: list[str] = []
    seen: set[str] = set()
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        if entry.get("passed") is False:
            continue
        keywords = [str(item).strip().lower() for item in (entry.get("keywords") or []) if str(item).strip()]
        keyword_hits = [str(item).strip().lower() for item in (entry.get("keyword_hits") or []) if str(item).strip()]
        if keywords and not keyword_hits:
            continue
        kind = str(entry.get("kind") or "")
        if kind not in {"path_exists", "file_exists_glob", "file_size_gt", "json_marker_valid_installer"}:
            continue
        candidates: list[str] = []
        path_value = str(entry.get("path") or "").strip()
        pattern_value = str(entry.get("pattern") or "").strip()
        resolved_path_value = str(entry.get("resolved_path") or "").strip()
        if path_value:
            candidates.append(path_value)
        if resolved_path_value:
            candidates.append(resolved_path_value)
        if pattern_value.lower().endswith((".exe", ".msi")) and "*" not in pattern_value:
            candidates.append(pattern_value)
        matches = entry.get("matches") or []
        if isinstance(matches, list):
            for match in matches:
                if isinstance(match, dict):
                    candidate = str(match.get("path") or "").strip()
                else:
                    candidate = str(match or "").strip()
                if candidate:
                    candidates.append(candidate)
        for candidate in candidates:
            normalized = candidate.replace("\\", "/").rstrip()
            if not normalized.lower().endswith((".exe", ".msi")):
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            results.append(candidate)
    return results


def _append_verified_installer_hint(prompt_text: str, verification_result: dict | None) -> str:
    installer_paths = _extract_verified_installer_paths(verification_result)
    if not installer_paths:
        return prompt_text
    details = "\n".join(f"- `{path}`" for path in installer_paths[:3])
    hint = (
        "Previously verified installer artifacts on the target machine. Prefer these exact installer paths before "
        "searching Downloads broadly again:\n"
        f"{details}"
    )
    return prompt_text + "\n\n" + hint


def _compose_retry_prompt(
    *,
    chunk: TeacherTaskChunk,
    verification_result: dict,
    attempt_index: int,
    execution_style: str = "python_first",
    prior_verification_result: dict | None = None,
    candidate_urls: list[str] | None = None,
) -> str:
    evidence = verification_result.get("evidence")
    error = verification_result.get("error")
    evidence_text = json.dumps(evidence, ensure_ascii=False, indent=2) if evidence else "[]"
    retry_header = f"Previous attempt {attempt_index} did not satisfy the chunk verifier. Retry only this chunk."
    details = [retry_header]
    normalized_style = _normalize_execution_style(execution_style)
    if error:
        details.append(f"Verifier error: {error}")
    details.append(f"Verifier evidence:\n{evidence_text}")
    if normalized_style == "gui_first":
        retry_target_keywords = _target_installer_keywords(
            chunk.title,
            chunk.agent_prompt,
            chunk.success_hint or "",
            *chunk.preconditions,
            limit=6,
        )
        details.append(
            "GUI-first retry rule: if the browser page for this chunk is already visible, stay on that same page/tab first. "
            "Do not guess a new direct installer URL before exhausting the visible page-content path."
        )
        details.append(
            "For a download chunk, end only when the installer file exists on disk with a plausible size. "
            "Do not launch or silently install the installer in this chunk."
        )
        details.append(
            "If one visible coordinate candidate does not progress, try a different visible page-content candidate in the same script. "
            "Do not use browser toolbar/address/tab/bookmark areas as click targets."
        )
        if retry_target_keywords:
            details.append(
                "If you must abandon the current page and run a new browser search, keep these exact task/product keywords in the query: "
                + ", ".join(retry_target_keywords)
                + "."
            )
            details.append(
                "Do not replace those task/product keywords with generic retry wording, verifier artifact names, or unrelated product names."
            )
    if candidate_urls:
        details.append(
            "Use the explicit open-target URLs listed above before generic search, but still verify that the visible "
            "page and download result match this chunk target."
        )
    prompt_text = _compose_chunk_prompt(
        chunk,
        execution_style=execution_style,
        candidate_urls=candidate_urls,
    )
    prompt_text = _append_verified_installer_hint(prompt_text, prior_verification_result)
    return prompt_text + "\n\n" + "\n\n".join(details)


def _chunk_looks_like_download_retry(chunk: TeacherTaskChunk) -> bool:
    combined = " ".join(
        str(part or "")
        for part in (
            chunk.title,
            chunk.agent_prompt,
            chunk.success_hint,
            json.dumps(chunk.verification or {}, ensure_ascii=False),
        )
    ).lower()
    return any(token in combined for token in ("download", "다운로드", "installer", "설치파일", ".exe", ".msi"))


def _teacher_link_candidate_urls_from_result(result_text: str) -> list[str]:
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return []
    urls = payload.get("candidate_urls")
    if not isinstance(urls, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_url in urls:
        url = str(raw_url or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned


def _initial_chunk_candidate_urls(
    *,
    task: str,
    chunk: TeacherTaskChunk,
    teacher_text: str | None = None,
    teacher_plan_source_text: str | None = None,
    limit: int = 5,
) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(urls: list[str]) -> None:
        for url in _normalize_http_candidate_urls(urls, limit=limit):
            lowered = url.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            candidates.append(url)
            if len(candidates) >= limit:
                return

    for text in (task, chunk.agent_prompt):
        _add(_extract_http_urls(text, limit=limit))
        if len(candidates) >= limit:
            return candidates

    for text in (teacher_text or "", teacher_plan_source_text or ""):
        _add(_extract_link_candidate_urls(text, task=task, limit=limit))
        if len(candidates) >= limit:
            return candidates
        _add(_extract_http_urls(text, limit=limit))
        if len(candidates) >= limit:
            return candidates
    return candidates


_RETRY_EXCLUSION_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_RETRY_EXCLUSION_SEARCH_HOST_TOKENS = (
    "google.",
    "bing.",
    "duckduckgo.",
    "yahoo.",
    "naver.",
    "daum.",
)
_RETRY_EXCLUSION_STRING_KEYS = frozenset(
    {
        "executed_python_code",
        "python_code",
        "raw_text",
        "stdout_tail",
        "stderr_tail",
    }
)


def _extract_retry_exclusion_urls_and_queries(text: str, *, limit: int = 8) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    queries: list[str] = []
    seen_urls: set[str] = set()
    seen_queries: set[str] = set()
    for raw_url in _RETRY_EXCLUSION_URL_RE.findall(str(text or "")):
        url = str(raw_url or "").strip().rstrip(".,)")
        if not url:
            continue
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        host = str(parsed.netloc or "").lower()
        if any(token in host for token in _RETRY_EXCLUSION_SEARCH_HOST_TOKENS):
            query_values = urllib.parse.parse_qs(str(parsed.query or ""), keep_blank_values=False).get("q") or []
            for raw_query in query_values:
                query = re.sub(r"\s+", " ", urllib.parse.unquote_plus(str(raw_query or "")).strip())
                if not query or query in seen_queries:
                    continue
                seen_queries.add(query)
                queries.append(query)
                if len(queries) >= limit:
                    break
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        urls.append(url)
        if len(urls) >= limit:
            continue
    return urls[:limit], queries[:limit]


def _iter_retry_exclusion_strings(payload) -> list[str]:
    snippets: list[str] = []
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if isinstance(value, str) and key in _RETRY_EXCLUSION_STRING_KEYS:
                    snippets.append(value)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return snippets


def _collect_retry_link_request_exclusions(
    *,
    session_root: Path,
    retry_agent_run_label: str,
    limit: int = 8,
) -> tuple[list[str], list[str]]:
    agent_runs_dir = session_root / "agent_runs"
    chunk_prefix = retry_agent_run_label.split(".attempt-", 1)[0]
    excluded_urls: list[str] = []
    excluded_queries: list[str] = []
    seen_urls: set[str] = set()
    seen_queries: set[str] = set()

    def _append(urls: list[str], queries: list[str]) -> None:
        for url in urls:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            excluded_urls.append(url)
            if len(excluded_urls) >= limit:
                break
        for query in queries:
            if query in seen_queries:
                continue
            seen_queries.add(query)
            excluded_queries.append(query)
            if len(excluded_queries) >= limit:
                break

    for artifact_path in sorted(agent_runs_dir.glob(f"{chunk_prefix}.attempt-*.json")):
        name = artifact_path.name
        if name.endswith((".prompt.json", ".verification.json", ".teacher_link_candidates.json")):
            continue
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except Exception:
            try:
                snippets = [artifact_path.read_text(encoding="utf-8", errors="ignore")]
            except OSError:
                continue
        else:
            snippets = _iter_retry_exclusion_strings(payload)
        collected_urls: list[str] = []
        collected_queries: list[str] = []
        for snippet in snippets:
            urls, queries = _extract_retry_exclusion_urls_and_queries(snippet, limit=limit)
            collected_urls.extend(urls)
            collected_queries.extend(queries)
        _append(collected_urls, collected_queries)

    for artifact_path in sorted(agent_runs_dir.glob(f"{chunk_prefix}.attempt-*.teacher_link_candidates.json")):
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_urls = payload.get("candidate_urls")
        if not isinstance(raw_urls, list):
            continue
        teacher_urls: list[str] = []
        for raw_url in raw_urls:
            url = str(raw_url or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            teacher_urls.append(url)
        _append(teacher_urls, [])

    return excluded_urls[:limit], excluded_queries[:limit]


def _request_teacher_retry_link_candidates(
    *,
    task: str,
    chunk: TeacherTaskChunk,
    verification_result: dict | None,
    teacher_command_template: str,
    teacher_workdir: str | None,
    teacher_timeout_s: float,
    session_root: Path,
    agent_run_label: str,
    execution_style: str,
) -> list[str]:
    if _normalize_execution_style(execution_style) != "gui_first":
        return []
    if not _chunk_looks_like_download_retry(chunk):
        return []
    if not str(teacher_command_template or "").strip():
        return []
    excluded_candidate_urls, excluded_search_queries = _collect_retry_link_request_exclusions(
        session_root=session_root,
        retry_agent_run_label=agent_run_label,
    )
    failure_context = json.dumps(verification_result or {}, ensure_ascii=False, indent=2)
    try:
        result = run_teacher_link_candidates(
            task=task,
            chunk_title=chunk.title,
            chunk_prompt=chunk.agent_prompt,
            failure_context=failure_context,
            command_template=teacher_command_template,
            cwd=teacher_workdir,
            timeout_s=min(max(float(teacher_timeout_s), 30.0), 180.0),
            limit=5,
            failed_candidate_urls=excluded_candidate_urls,
            failed_search_queries=excluded_search_queries,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "candidate_urls": [],
            "excluded_candidate_urls": excluded_candidate_urls,
            "excluded_search_queries": excluded_search_queries,
        }
        path = session_root / "agent_runs" / f"{agent_run_label}.teacher_link_candidates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return []
    candidate_urls = _teacher_link_candidate_urls_from_result(result.response_text)
    payload = {
        "ok": True,
        "teacher_prompt": result.prompt,
        "teacher_response": result.response_text,
        "candidate_urls": candidate_urls,
        "excluded_candidate_urls": excluded_candidate_urls,
        "excluded_search_queries": excluded_search_queries,
        "command_result": asdict(result.command_result),
    }
    path = session_root / "agent_runs" / f"{agent_run_label}.teacher_link_candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return candidate_urls


def _teacher_execution_style_context(execution_style: str) -> str:
    normalized_style = _normalize_execution_style(execution_style)
    if normalized_style == "gui_first":
        return (
            "Plan for GUI-first automation. The agent still returns executable Python only, but when the current "
            "screenshot already shows a browser page, search results, a download control, an installer wizard, a UAC "
            "prompt, or an app window, prefer advancing that visible UI state before replacing it with a fresh direct "
            "download or silent-install shortcut. For desktop software installation tasks, do not route through "
            "Microsoft Store, app stores, winget, package managers, or app-store web listings unless the user "
            "explicitly requests that source; prefer a relevant vendor, product, or download page and a Windows "
            "`.exe`/`.msi` installer when one is available."
        )
    return (
        "Plan for Python-first automation. Prefer deterministic Python flows such as direct URL download, "
        "file verification in Downloads, subprocess-based installer launch, and Python GUI automation only for the "
        "remaining dialogs. For desktop software installation tasks, do not route through Microsoft Store, app "
        "stores, winget, package managers, or app-store web listings unless the user explicitly requests that source; "
        "prefer a relevant vendor, product, or download page and a Windows `.exe`/`.msi` installer when one is available."
    )


def _chunk_completed_from_agent_payload(payload: dict) -> bool:
    summary = payload.get("summary") or {}
    final_response = summary.get("final_response") or {}
    return bool(final_response.get("done")) or str(summary.get("stopped_reason") or "") == "task_completed"


def _task_requests_post_install_launch(task: str) -> bool:
    lowered = str(task or "").lower()
    return any(
        token in lowered
        for token in (
            "launch",
            "run",
            "open",
            "start",
            "execute",
            "실행",
            "열어",
            "켜줘",
            "시작",
        )
    )


def _should_stop_after_install_completion(task: str, chunk: TeacherTaskChunk) -> bool:
    if _task_requests_post_install_launch(task):
        return False
    task_text = str(task or "").lower()
    if not any(token in task_text for token in ("install", "installer", "setup", "설치")):
        return False
    chunk_text = " ".join(
        str(value or "").lower()
        for value in (
            chunk.title,
            chunk.success_hint,
            chunk.agent_prompt,
        )
    )
    if any(token in chunk_text for token in ("download installer", "download-only", "다운로드")) and not any(
        token in chunk_text for token in ("run installer", "launch the downloaded", "설치 파일을 실행", "installer finishes")
    ):
        return False
    install_markers = (
        "run installer",
        "launch the downloaded",
        "complete setup",
        "installer finishes",
        "is installed",
        "installed on the machine",
        "설치 파일을 실행",
        "설치 완료",
        "설치가 완료",
    )
    return any(marker in chunk_text for marker in install_markers)


def _compose_teacher_prompt(*, task: str, config: dict) -> str:
    context = str(config.get("teacher_task_context") or "").strip()
    style_context = _teacher_execution_style_context(str(config.get("execution_style") or "python_first"))
    if not context:
        return f"{style_context}\n\nActual task for the target machine:\n{task.strip()}"
    return f"{context}\n\n{style_context}\n\nActual task for the target machine:\n{task.strip()}"


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
    session_id = make_session_id(args.task)
    session_id, session_root = prepare_session_root(
        output_dir=str(config["output_dir"]),
        task=args.task,
        session_id=session_id,
    )
    local_fallback_staging_subdir = _task_staging_subdir(args.task, salt=session_id)
    teacher_prompt = args.teacher_prompt or _compose_teacher_prompt(task=args.task, config=config)
    teacher_command_template = str(config.get("teacher_command_template", ""))
    teacher_workdir = config.get("teacher_workdir")
    teacher_timeout_s = float(config.get("teacher_timeout_s", 300))
    teacher_split_enabled = bool(config.get("teacher_split_enabled", True))
    execution_style = _normalize_execution_style(str(config.get("execution_style") or "python_first"))
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
                execution_style=execution_style,
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
            execution_style=execution_style,
            staging_subdir=local_fallback_staging_subdir,
        )
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
            execution_style=execution_style,
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
    install_completion_reached = False
    chunk_verification_enabled = bool(config.get("chunk_verification_enabled", True))
    previous_chunk_verification_result: dict | None = None
    for chunk_index, chunk in enumerate(teacher_plan.chunks, start=1):
        attempt_index = 0
        chunk_completed = False
        final_verification_result: dict | None = None
        final_run_label: str | None = None
        final_run_dir: str | None = None
        attempts_used = 0
        initial_candidate_urls = _initial_chunk_candidate_urls(
            task=args.task,
            chunk=chunk,
            teacher_text=teacher_result.response_text,
            teacher_plan_source_text=teacher_plan.source_text,
        )
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
            prompt_text = _compose_chunk_prompt(
                chunk,
                execution_style=execution_style,
                candidate_urls=initial_candidate_urls,
            )
            prompt_text = _append_verified_installer_hint(prompt_text, previous_chunk_verification_result)
            if attempt_index > 0 and final_verification_result is not None:
                retry_agent_run_label = f"chunk-{chunk_index:03d}.attempt-{attempt_index + 1:02d}"
                retry_candidate_urls = _request_teacher_retry_link_candidates(
                    task=args.task,
                    chunk=chunk,
                    verification_result=final_verification_result,
                    teacher_command_template=teacher_command_template,
                    teacher_workdir=teacher_workdir,
                    teacher_timeout_s=float(config.get("teacher_split_timeout_s", teacher_timeout_s)),
                    session_root=session_root,
                    agent_run_label=retry_agent_run_label,
                    execution_style=execution_style,
                )
                prompt_text = _compose_retry_prompt(
                    chunk=chunk,
                    verification_result=final_verification_result,
                    attempt_index=attempt_index,
                    execution_style=execution_style,
                    prior_verification_result=previous_chunk_verification_result,
                    candidate_urls=retry_candidate_urls or initial_candidate_urls,
                )
            prompt_result = run_agent_prompt(
                agent_command=str(config["agent_command"]),
                prompt=prompt_text,
                endpoint=config.get("agent_endpoint"),
                config_path=config.get("agent_config_path"),
                reasoning_enabled=bool(config.get("agent_reasoning_enabled", False)),
                execution_style=execution_style,
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
        if _should_stop_after_install_completion(args.task, chunk):
            install_completion_reached = True
            stopped_reason = "install_completed"
            break
        previous_chunk_verification_result = final_verification_result

    completed_chunks = len(chunk_results)
    remaining_chunks = [] if install_completion_reached else teacher_plan.chunks[completed_chunks:]
    for remaining_index, chunk in enumerate(remaining_chunks, start=completed_chunks + 1):
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
    run_parser.add_argument("--execution-style", choices=("python_first", "gui_first"), default=None, help="How teacher/chunk prompts should bias agent behavior.")
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
