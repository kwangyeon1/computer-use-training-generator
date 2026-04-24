from __future__ import annotations

import base64
import html
import hashlib
import json
import re
import urllib.parse
import urllib.request

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
            {{"kind": "process_exists", "name": "ExampleApp.exe"}}
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
- If the user's original task explicitly contains URLs or important warnings, keep them in the relevant chunk prompt.
- For software download/install chunks, do not invent or promote exact vendor URLs that were not present in the user's original task. If no user-provided URL exists, instruct the agent to use a short target-keyword search or continue from the visible browser page instead of hard-coding a guessed domain.
- Each chunk must include a read-only verification plan.
- Verification must use only the allowed check kinds: `path_exists`, `file_exists_glob`, `file_size_gt`, `process_exists`, `json_marker_valid_installer`, `json_marker_valid_exe`.
- All verification checks are combined with logical AND.
- If you need to allow multiple possible installer filenames, use one broad glob that matches all acceptable names instead of multiple alternative checks.
- For GUI-first Windows installer download chunks, prefer `json_marker_valid_installer` on the soft continuity file instead of requiring the final filename to contain the app name. For Python-first chunks, a broad installer glob such as `~/Downloads/*vendor*.exe` is acceptable; official `.msi` installers are also acceptable when that is the vendor-provided Windows installer.
- Do not output raw Python for verification.
- `preconditions` should describe what must already be true before the chunk starts.
- `max_retries` should be a small integer, usually 0, 1, or 2.
- `on_fail` must be either `retry_current_chunk` or `fail_session`.
- Do not add commentary outside JSON.
- Plan for the target machine described by the task and teacher answer, not for your own CLI sandbox.
- If the task is about a Windows desktop or Windows software install, use Windows-oriented chunk prompts and Windows-friendly verifier checks.
- For Windows software installation tasks, prefer the official `.exe` or `.msi` installer build, not `.zip`, portable, or archive downloads unless the task explicitly asks for those formats.
- For Windows installer download chunks, state explicitly in `agent_prompt` that the agent must download the installer `.exe` or `.msi` and must avoid `.zip` or archive builds.
- For Windows installer download chunks, prefer deterministic Python-first flows where there is grounded evidence: user-provided URL, current visible page, search result page, or a verified link discovered by the agent. Avoid guessed exact domains in the teacher chunk.
- For Windows installer download chunks, name exact landing/release URLs only when the user supplied them in the original task; otherwise tell the agent to search with target keywords and platform hints.
- For GUI-first install/download tasks, do not emit a standalone browser-navigation chunk whose verifier only checks a generic state such as `~/Downloads` existing. If opening an official page is only preparation for a download, fold that navigation into the download chunk and verify the actual downloaded installer artifact instead.
- Only fall back to browser GUI navigation when a direct official installer URL cannot be determined from the teacher answer or current task context.
- For general GUI operation tasks after installation, continue from the current desktop/app state instead of restarting setup or redownloading software unless the task explicitly asks for it.
- For general GUI operation tasks, prefer prompts and verifiers that reflect the intended app state, open window/process, created file, or changed project/workspace state.
- For general GUI operation tasks, prefer Python GUI automation or process/file inspection over instructions written as if a human will take the next action.
- Do not emit Linux or macOS verification paths unless the task explicitly requires those platforms.
{execution_style_guidance}

Overall task:
{task}

Teacher answer:
{teacher_text}
"""

_REPLAN_LINK_CANDIDATE_PROMPT_TEMPLATE = """Return strict JSON only.

The computer-use agent failed a software download/navigation chunk and needs candidate landing-page URLs for the next retry.

Task:
{task}

Chunk title:
{chunk_title}

Chunk prompt:
{chunk_prompt}

Failure/verifier context:
{failure_context}

Previously failed attempts to exclude from the next retry:
{retry_exclusions}

Requirements:
- Output exactly one JSON object with this shape: {{"candidate_urls":["https://..."],"notes":"short optional reason"}}
- Return 1 to 5 absolute http(s) page URLs, not prose.
- Prefer vendor/product/download/release landing pages that are relevant to the exact target product keywords in the task.
- Do not return a page URL that is already listed in the excluded failed-attempt URLs.
- If excluded failed search queries are listed, avoid repeating those same failed searches as the primary retrieval path.
- Do not return search-engine result URLs, app-store URLs, YouTube, blogs, tutorials, reviews, forums, social media, or unrelated software pages.
- It is acceptable to include non-official but clearly product-specific download pages when the official page failed.
- Do not invent versioned installer artifact filenames. Return page URLs that the agent can open and inspect.
- If no relevant page URL is known, return {{"candidate_urls":[],"notes":"no relevant URL found"}}.
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
    "msi",
    "zip",
    "archive",
    "http",
    "https",
    "www",
    "com",
    "io",
    "docs",
    "edition",
    "for",
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

_GENERIC_STOP_TOKENS = {
    "a",
    "an",
    "and",
    "any",
    "app",
    "archive",
    "artifact",
    "browser",
    "build",
    "button",
    "client",
    "code",
    "confirm",
    "continue",
    "current",
    "desktop",
    "dialog",
    "download",
    "downloads",
    "entry",
    "exe",
    "file",
    "first",
    "follow",
    "for",
    "from",
    "generated",
    "driven",
    "automation",
    "gui",
    "have",
    "html",
    "https",
    "identify",
    "installer",
    "just",
    "latest",
    "link",
    "machine",
    "not",
    "official",
    "only",
    "open",
    "page",
    "perform",
    "verify",
    "verified",
    "verifying",
    "portable",
    "program",
    "prompt",
    "python",
    "release",
    "return",
    "service",
    "version",
    "same",
    "save",
    "saved",
    "screen",
    "setup",
    "step",
    "target",
    "task",
    "that",
    "the",
    "their",
    "then",
    "this",
    "use",
    "using",
    "vendor",
    "visible",
    "downloaded",
    "driving",
    "process",
    "processes",
    "control",
    "controls",
    "desktop",
    "state",
    "ready",
    "wait",
    "waiting",
    "when",
    "windows",
    "with",
    "zhihu",
    "question",
}

_GENERIC_KOREAN_STOP_TOKENS = {
    "설치",
    "설치해줘",
    "설치해",
    "다운로드",
    "다운로드해",
    "다운로드해줘",
    "다운받아",
    "다운받아줘",
    "내려받아",
    "내려받아줘",
    "받아",
    "받아줘",
    "해줘",
    "해주세요",
    "프로그램",
    "프로그램을",
    "버전",
    "pc버전",
    "윈도우",
    "공식",
    "페이지",
    "실행",
    "파일",
    "폴더",
    "앱",
    "대상",
    "최신",
    "화면",
    "브라우저",
    "검색결과",
    "설치파일",
}

_SUSPICIOUS_RESULT_HOST_TOKENS = {
    "download",
    "downloads",
    "setup",
    "installer",
    "latest",
    "free",
    "get",
    "safe",
    "apps-",
    "app-",
    "pc-",
    "win-",
}

_NON_VENDOR_RESULT_HOST_TOKENS = {
    "blog",
    "blogs",
    "youtube",
    "youtu",
    "forum",
    "community",
    "reddit",
    "cafe",
    "tistory",
    "medium",
    "zhihu",
    "naver",
    "daum",
    "google",
    "bing",
    "yahoo",
    "duckduckgo",
    "baidu",
}

_REFERENCE_RESULT_TOKENS = {
    "dictionary",
    "translate",
    "translation",
    "translator",
    "wiktionary",
    "thesaurus",
    "papago",
    "deepl",
}

_KOREAN_PARTICLE_SUFFIXES = (
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
    "으로도",
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


def _strip_korean_particle(token: str) -> str:
    raw = str(token or "").strip().lower()
    if not raw or re.search(r"[가-힣]", raw) is None:
        return raw
    for suffix in _KOREAN_PARTICLE_SUFFIXES:
        if raw.endswith(suffix) and len(raw) > len(suffix) + 1:
            candidate = raw[: -len(suffix)]
            if candidate:
                return candidate
    return raw


def _normalize_execution_style(value: str | None) -> str:
    normalized = str(value or "python_first").strip().lower().replace("-", "_")
    if normalized == "gui":
        normalized = "gui_first"
    if normalized not in {"python_first", "gui_first"}:
        normalized = "python_first"
    return normalized


def _decode_bing_result_url(raw_href: str) -> str:
    href = html.unescape(str(raw_href or "").strip())
    if not href:
        return href
    parsed = urllib.parse.urlparse(href)
    host = str(parsed.netloc or "").lower()
    if "bing.com" not in host or not parsed.path.startswith("/ck/"):
        return href
    encoded = urllib.parse.parse_qs(parsed.query).get("u", [None])[0]
    if not encoded:
        return href
    payload = str(encoded).strip()
    if payload.startswith("a1"):
        payload = payload[2:]
    try:
        padding = "=" * (-len(payload) % 4)
        return base64.urlsafe_b64decode(payload + padding).decode("utf-8", errors="replace")
    except Exception:
        return href


def _clean_html_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()


def _extract_bing_result_candidates(html_text: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for block_match in re.finditer(r'<li class="b_algo".*?</li>', str(html_text or ""), flags=re.S):
        block = block_match.group(0)
        title_match = re.search(r"<h2[^>]*><a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a></h2>", block, flags=re.S)
        if not title_match:
            continue
        raw_href = str(title_match.group(1) or "").strip()
        raw_title = str(title_match.group(2) or "").strip()
        snippet_match = re.search(r"<p>(.*?)</p>", block, flags=re.S)
        raw_snippet = str(snippet_match.group(1) or "").strip() if snippet_match else ""
        url = _decode_bing_result_url(raw_href)
        title = _clean_html_text(raw_title)
        snippet = _clean_html_text(raw_snippet)
        if not url or not title:
            continue
        candidates.append(
            {
                "url": url,
                "title": title,
                "snippet": snippet,
            }
        )
    return candidates


def _registrable_host(host: str) -> str:
    labels = [label for label in str(host or "").split(".") if label]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return str(host or "").lower()


def _score_official_page_candidate(task: str, candidate: dict[str, str]) -> int:
    url = str(candidate.get("url") or "").strip()
    title = str(candidate.get("title") or "").strip()
    snippet = str(candidate.get("snippet") or "").strip()
    if not url or not title:
        return -10_000
    parsed = urllib.parse.urlparse(url)
    host = str(parsed.netloc or "").lower()
    path = str(parsed.path or "").lower()
    decoded_path = urllib.parse.unquote(path)
    registrable = _registrable_host(host)
    labels = [label for label in host.split(".") if label]
    lead_label = labels[0] if labels else ""
    combined = "\n".join((title, snippet, url, decoded_path)).lower()
    keywords = _target_installer_keywords(task, limit=3)

    score = 0
    if any(token in combined for token in ("official", "공식")):
        score += 28
    if any(token in combined for token in ("download", "다운로드", "install", "설치", "windows", "pc")):
        score += 12
    if any(token in host for token in _NON_VENDOR_RESULT_HOST_TOKENS):
        score -= 120
    if any(token in combined for token in _REFERENCE_RESULT_TOKENS):
        score -= 140
    if "apps.microsoft.com" in host or "microsoft store" in combined:
        score -= 18
    if "notice" in path or "notices" in path:
        score -= 10
    if "/download" in path or "/service/" in path or "/release" in path:
        score += 8
    if any(
        marker in combined
        for marker in (
            "사용법",
            "how to",
            "tutorial",
            "guide",
            "review",
            "후기",
            "tips",
            "tip ",
            "blog",
        )
    ):
        score -= 72
    if (
        not any(marker in decoded_path for marker in ("/download", "/downloads", "/service", "/release", "/releases", "/product"))
        and len([segment for segment in re.split(r"[^a-z0-9가-힣]+", decoded_path) if segment]) >= 3
    ):
        score -= 36
    if lead_label and any(lead_label.startswith(prefix) for prefix in ("pc-", "win-", "apps-", "app-")):
        score -= 36
    for token in _SUSPICIOUS_RESULT_HOST_TOKENS:
        if token in host:
            score -= 18
    score -= host.count("-") * 8
    if len(labels) <= 3:
        score += 10
    if registrable and registrable == host:
        score += 6
    if lead_label and len(lead_label) <= 12:
        score += 6
    keyword_hits = 0
    for keyword in keywords:
        lowered_keyword = keyword.lower()
        matched = False
        if lowered_keyword in combined:
            score += 8
            matched = True
        if lowered_keyword in host:
            score += 6
            matched = True
        if matched:
            keyword_hits += 1
    if keywords:
        if keyword_hits == 0:
            score -= 240
        elif keyword_hits == 1:
            score += 18
        else:
            score += 36
    return score


def _looks_like_plausible_official_page_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    host = str(parsed.netloc or "").lower()
    if not host:
        return False
    if any(token in host for token in _NON_VENDOR_RESULT_HOST_TOKENS):
        return False
    if host == "apps.microsoft.com":
        return False
    decoded_path = urllib.parse.unquote(str(parsed.path or "")).lower()
    combined = "\n".join((host, decoded_path))
    if any(token in combined for token in _REFERENCE_RESULT_TOKENS):
        return False
    if any(
        marker in combined
        for marker in (
            "사용법",
            "tutorial",
            "guide",
            "review",
            "후기",
            "blog",
        )
    ):
        return False
    path_tokens = [segment for segment in re.split(r"[^a-z0-9가-힣]+", decoded_path) if segment]
    has_official_path_marker = any(
        marker in decoded_path
        for marker in ("/download", "/downloads", "/service", "/release", "/releases", "/product")
    )
    if not has_official_path_marker and len(path_tokens) >= 3:
        return False
    return True


def _sanitize_discovered_official_urls(task: str, urls: list[str], *, limit: int = 2) -> list[str]:
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw_url in urls:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if not _looks_like_plausible_official_page_url(url):
            continue
        score = _score_official_page_candidate(
            task,
            {
                "url": url,
                "title": urllib.parse.unquote(str(urllib.parse.urlparse(url).path or "")),
                "snippet": "",
            },
        )
        ranked.append((score, url))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [url for score, url in ranked[: max(1, int(limit))] if score > -20]


def _select_official_page_urls(task: str, candidates: list[dict[str, str]], *, limit: int = 2) -> list[str]:
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = str(candidate.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        score = _score_official_page_candidate(task, candidate)
        ranked.append((score, url))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [url for score, url in ranked[: max(1, int(limit))] if score > -20]


def _discover_official_page_urls(task: str, *, limit: int = 2) -> list[str]:
    keywords = _target_installer_keywords(task, limit=3)
    if not keywords:
        return []
    query_terms: list[str] = []
    for token in [*keywords, *_task_search_platform_hints(task, limit=2)]:
        cleaned = str(token or "").strip()
        if cleaned and cleaned not in query_terms:
            query_terms.append(cleaned)
    if not any(re.search(r"[가-힣]", keyword) for keyword in query_terms) and "windows" not in query_terms:
        query_terms.append("windows")
    query = " ".join(query_terms[:4])
    search_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
    user_agents = (
        "Mozilla/5.0",
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    for user_agent in user_agents:
        request = urllib.request.Request(
            search_url,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                html_text = response.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        selected = _select_official_page_urls(task, _extract_bing_result_candidates(html_text), limit=limit)
        selected = _sanitize_discovered_official_urls(task, selected, limit=limit)
        if selected:
            return selected
    return []


def _url_keyword_text(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    values: list[str] = []
    host_labels = [label for label in str(parsed.netloc or "").lower().split(".") if label]
    if len(host_labels) > 2:
        for label in host_labels[:-2]:
            cleaned = re.sub(r"[^a-z0-9가-힣]+", "", label).strip()
            if cleaned:
                values.append(cleaned)
    for segment in str(parsed.path or "").split("/"):
        decoded = urllib.parse.unquote(segment).strip()
        if decoded:
            values.append(decoded)
    for entries in urllib.parse.parse_qs(str(parsed.query or "")).values():
        for entry in entries:
            decoded = urllib.parse.unquote(str(entry or "")).strip()
            if decoded:
                values.append(decoded)
    return " ".join(values).strip()


def _execution_style_guidance(execution_style: str) -> str:
    normalized = _normalize_execution_style(execution_style)
    if normalized == "gui_first":
        return (
            "- Execution style for this planning run: `gui_first`.\n"
            "- Keep all steps executable in Python, but when the screenshot or current state already shows a browser page, search results, "
            "download control, installer wizard, or target app window, prefer continuing from that visible UI.\n"
            "- If a visible download-like or installer-like control is on screen, prefer screenshot-grounded GUI actions on the current page before HTML scraping, fresh HTTP fetching, or guessed installer URLs.\n"
            "- On retries, keep the same visible page/tab first, try different page-content candidates, and avoid browser toolbar/address/tab regions as click targets.\n"
            "- Do not force direct download or silent install shortcuts when grounded visible UI progression is the safer next step.\n"
            "- For desktop software installation tasks, do not route through Microsoft Store, app stores, or package managers unless the task explicitly asks for that source. Prefer direct official vendor pages or direct official `.exe` or `.msi` installers."
        )
    return (
        "- Execution style for this planning run: `python_first`.\n"
        "- Prefer deterministic Python-first flows such as direct official URL discovery, direct file download, file/process checks, and "
        "silent or subprocess-based install/launch before browser-click-heavy flows.\n"
        "- For desktop software installation tasks, do not route through Microsoft Store, app stores, or package managers unless the task explicitly asks for that source. Prefer direct official vendor pages or direct official `.exe` or `.msi` installers."
    )


def _looks_like_install_task(task: str) -> bool:
    lowered = str(task or "").lower()
    return any(token in lowered for token in ("설치", "install", "installer", "setup"))


def _task_explicitly_requests_store(task: str) -> bool:
    lowered = str(task or "").lower()
    return any(
        token in lowered
        for token in (
            "microsoft store",
            "ms store",
            "windows store",
            "app store",
            "스토어",
            "앱 스토어",
        )
    )


def _looks_like_store_detour_prompt(agent_prompt: str) -> bool:
    lowered = str(agent_prompt or "").lower()
    negative_markers = (
        "do not use microsoft store",
        "do not use the microsoft store",
        "do not route through microsoft store",
        "do not route through the microsoft store",
        "do not substitute a third-party source or microsoft store",
        "avoid microsoft store",
        "instead of microsoft store",
        "microsoft store 대신",
        "스토어로 가지 마세요",
        "스토어를 사용하지 마세요",
    )
    if any(marker in lowered for marker in negative_markers):
        return False
    if any(token in lowered for token in ("ms-windows-store://", "apps.microsoft.com")):
        return True
    return any(
        token in lowered
        for token in (
            "open the microsoft store",
            "use microsoft store",
            "install from microsoft store",
            "microsoft store listing",
            "windows store listing",
            "open store listing",
            "open the store listing",
            "앱 스토어",
            "스토어에서 설치",
            "스토어 목록",
        )
    )


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


def _product_phrase_keyword_candidates(part: str) -> list[str]:
    text = str(part or "")
    task_prefix = re.split(
        r"(?:설치|다운로드|실행|프로그램|install|download|launch|run)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    raw_tokens = re.findall(r"[A-Za-z0-9가-힣]+", task_prefix)
    if len(raw_tokens) < 2:
        return []
    has_product_case = any(
        (token.isupper() and len(token) <= 5)
        or (any(ch.isupper() for ch in token) and any(ch.islower() for ch in token))
        for token in raw_tokens
    )
    has_connector = any(token.lower() in {"for", "of", "with", "and"} for token in raw_tokens)
    if not (has_product_case or has_connector):
        return []
    result: list[str] = []
    for raw in raw_tokens:
        lowered = _strip_korean_particle(raw.lower())
        if not lowered or lowered in {"for", "of", "with", "and", "the"}:
            continue
        if lowered in _GENERIC_INSTALLER_TOKENS or lowered in _GENERIC_ACTION_TOKENS:
            continue
        if lowered in _GENERIC_STOP_TOKENS and lowered != "browser":
            continue
        if lowered in _GENERIC_KOREAN_STOP_TOKENS or lowered.isdigit():
            continue
        if re.search(r"[가-힣]", lowered) is None and len(lowered) <= 2 and not raw.isupper():
            continue
        if lowered not in result:
            result.append(lowered)
    return result


def _target_installer_keywords(*parts: str, limit: int = 2) -> list[str]:
    tokens: list[str] = []
    for part in parts:
        for token in _product_phrase_keyword_candidates(str(part or "")):
            if token not in tokens:
                tokens.append(token)
            if len(tokens) >= limit:
                return tokens
        for token in re.findall(r"[a-z0-9가-힣]+", str(part or "").lower()):
            token = _strip_korean_particle(token)
            if (
                not token
                or token in _GENERIC_INSTALLER_TOKENS
                or token in _GENERIC_ACTION_TOKENS
                or token in _GENERIC_STOP_TOKENS
                or token in _GENERIC_KOREAN_STOP_TOKENS
                or token.isdigit()
                or (re.search(r"[가-힣]", token) is None and len(token) <= 2)
                or (re.search(r"[가-힣]", token) is not None and len(token) <= 1)
            ):
                continue
            if token not in tokens:
                tokens.append(token)
            if len(tokens) >= limit:
                return tokens
    return tokens


def _task_search_platform_hints(*parts: str, limit: int = 2) -> list[str]:
    raw_text = " ".join(str(part or "") for part in parts).lower()
    compact_text = re.sub(r"\s+", "", raw_text)
    hints: list[str] = []

    def _append_hint(value: str) -> None:
        cleaned = str(value or "").strip().lower()
        if cleaned and cleaned not in hints:
            hints.append(cleaned)

    if any(token in compact_text for token in ("pc버전", "pc용", "피시버전", "피씨버전")) or re.search(r"\bpc\b", raw_text):
        _append_hint("pc")
    if "windows" in raw_text or "윈도우" in raw_text:
        _append_hint("windows")
    return hints[:limit]


def _matching_installer_hint(*, source_task: str, title: str, agent_prompt: str, action: str) -> str:
    keywords = _target_installer_keywords(source_task, limit=3) or _target_installer_keywords(
        source_task,
        title,
        agent_prompt,
        limit=3,
    )
    if keywords:
        joined = ", ".join(f"`{keyword}`" for keyword in keywords)
        return (
            f"대상 앱과 일치하는 installer만 사용하세요. 파일명은 가능하면 {joined} 같은 대상 앱 키워드를 포함해야 하며, "
            f"무관한 다른 installer `.exe`나 `.msi`는 {action}하지 마세요."
        )
    return f"대상 앱과 일치하는 installer만 사용하고, 무관한 다른 installer `.exe`나 `.msi`는 {action}하지 마세요."


def _official_source_hint(*parts: str) -> str:
    source_task = str(parts[0] if parts else "")
    keywords = _target_installer_keywords(source_task, limit=3) or _target_installer_keywords(*parts, limit=3)
    if not keywords:
        return ""
    joined = ", ".join(f"`{keyword}`" for keyword in keywords)
    return (
        f"작업과 일치하는 vendor, product, download 페이지를 우선 사용하세요. "
        f"가능하면 {joined} 같은 대상 앱 키워드가 포함된 Windows installer `.exe` 또는 `.msi`를 우선 찾으세요. "
        "한 페이지에서 raw installer 링크를 찾지 못해도 하드코딩된 버전 번호나 추측한 파일명으로 점프하지 말고, "
        "다른 관련 페이지 HTML에서 최신 `.exe` 또는 `.msi` asset을 다시 추출하세요."
    )


def _extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.findall(r"https?://[^\s<>()\[\]{}\"'`]+", str(text or "")):
        candidate = str(match or "").strip().rstrip(".,);]>}")
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _remove_non_user_urls_from_prompt(source_task: str, text: str) -> str:
    user_urls = set(_extract_urls_from_text(source_task))
    if user_urls or _task_explicitly_requests_store(source_task):
        return str(text or "")
    cleaned = str(text or "")
    for url in _extract_urls_from_text(cleaned):
        cleaned = cleaned.replace(f"`{url}`", "a relevant product/download page")
        cleaned = cleaned.replace(url, "a relevant product/download page")
    return cleaned


def _exact_official_page_hint(task: str, *parts: str, limit: int = 2) -> str:
    task_urls = _extract_urls_from_text(task)
    if not task_urls:
        return ""
    discovered: list[str] = []
    for url in task_urls:
        if url not in discovered:
            discovered.append(url)
    selected = _sanitize_discovered_official_urls(task, discovered, limit=limit)
    if not selected:
        return ""
    lines = "\n".join(f"- {url}" for url in selected)
    return (
        "Use these exact page URLs first before any search engine result or inferred domain:\n"
        f"{lines}\n"
        "Prefer the first URL if it already looks like the primary vendor or product landing page."
    )


def _source_allows_official_archive_package(*parts: str) -> bool:
    combined = "\n".join(str(part or "") for part in parts)
    lowered = combined.lower()
    if any(url.lower().endswith((".zip", ".alz")) for url in _extract_urls_from_text(combined)):
        return True
    explicit_markers = (
        "if the downloaded package is a `.zip`",
        "if the downloaded package is a .zip",
        "if the package is a `.zip`",
        "if the package is a .zip",
        "page only provides an archive package",
        "only provides an archive package",
        "not as a direct standalone `.exe` link",
        "not as a direct standalone .exe link",
        "downloadable package, not as a direct standalone `.exe` link",
        "downloadable package, not as a direct standalone .exe link",
        "contained installer executable",
        "contained installer on the target machine",
        "locate the installer executable inside the extracted folder",
    )
    if any(marker in lowered for marker in explicit_markers):
        return True
    return (
        any(token in lowered for token in ("zip", "alz", "archive package", "installer package"))
        and "extract" in lowered
        and any(token in lowered for token in ("installer", "setup", "contained"))
    )


def _likely_install_path_hint(keyword: str) -> str:
    target = re.sub(r"[^A-Za-z0-9]+", "", str(keyword or "")).strip() or "TargetApp"
    return (
        f"%LOCALAPPDATA%\\\\{target}, %LOCALAPPDATA%\\\\Programs\\\\{target}, "
        f"%ProgramFiles%\\\\{target}, %ProgramFiles(x86)%\\\\{target}"
    )


def _downloads_installer_glob(task: str, *parts: str) -> str | None:
    keywords = _target_installer_keywords(task, *parts, limit=6)
    if not keywords:
        return None
    filename_like_ascii = []
    fallback_ascii = []
    avoid_ascii_tokens = {"corp", "company", "service", "detail", "page", "lang", "release", "download"}
    for keyword in keywords:
        if re.search(r"[a-z]", keyword) is None:
            continue
        lowered = keyword.lower()
        if lowered in avoid_ascii_tokens or lowered.endswith("corp"):
            fallback_ascii.append(keyword)
            continue
        filename_like_ascii.append(keyword)
    preferred = (filename_like_ascii or fallback_ascii or keywords)[0]
    token = re.sub(r"[^0-9A-Za-z가-힣]+", "", str(preferred or "")).strip()
    if not token:
        return None
    return f"~/Downloads/*{token}*.exe"


def _task_staging_subdir(task: str, *, salt: str | None = None) -> str:
    keywords = _target_installer_keywords(task, limit=2)
    ascii_parts = [re.sub(r"[^a-z0-9]+", "", keyword.lower()) for keyword in keywords]
    ascii_parts = [part for part in ascii_parts if part]
    prefix = "-".join(ascii_parts[:2]) or "targetapp"
    digest_source = f"{task or ''}::{salt or ''}"
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def _simplify_windows_installer_glob(pattern: str) -> str:
    raw = str(pattern or "").strip()
    lowered = raw.lower()
    if not raw or not lowered.endswith((".exe", ".msi")):
        return raw
    suffix = ".msi" if lowered.endswith(".msi") else ".exe"
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
    vendor_glob = f"*{'*'.join(vendor_tokens)}*{suffix}"
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
    if "windows" not in combined or not any(ext in combined for ext in (".exe", ".msi", ".zip", ".alz")):
        return verification
    if not any(keyword in combined for keyword in ("installer", "install", "설치", "download", "다운로드")):
        return verification
    checks = verification.get("checks")
    if not isinstance(checks, list):
        return verification
    normalized_checks: list[dict] = []
    changed = False
    has_file_exists_glob = False
    has_file_size_gt = False
    candidate_pattern = ""
    for item in checks:
        if not isinstance(item, dict):
            normalized_checks.append(item)
            continue
        updated = dict(item)
        kind = str(updated.get("kind") or "").strip()
        if kind == "path_exists":
            path = str(updated.get("path") or "").strip()
            if path and any(token in path for token in ("*", "?", "[")):
                updated = {"kind": "file_exists_glob", "pattern": path}
                kind = "file_exists_glob"
                changed = True
        if kind == "file_exists_glob":
            pattern = str(updated.get("pattern") or "").strip()
            simplified = _simplify_windows_installer_glob(pattern)
            if simplified and simplified != pattern:
                updated["pattern"] = simplified
                changed = True
            has_file_exists_glob = True
            candidate_pattern = str(updated.get("pattern") or "").strip() or candidate_pattern
        elif kind == "file_size_gt":
            has_file_size_gt = True
        normalized_checks.append(updated)
    if has_file_exists_glob and not has_file_size_gt and candidate_pattern:
        normalized_checks.append(
            {
                "kind": "file_size_gt",
                "pattern": candidate_pattern,
                "bytes": 1_000_000,
            }
        )
        changed = True
    if not changed:
        return verification
    return {**verification, "checks": normalized_checks}


def _chunk_has_verification_kind(chunk: TeacherTaskChunk, *kinds: str) -> bool:
    checks = (chunk.verification or {}).get("checks")
    if not isinstance(checks, list):
        return False
    expected = {str(kind).strip() for kind in kinds if str(kind).strip()}
    for item in checks:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip() in expected:
            return True
    return False


def _verification_has_kind(verification: dict | None, *kinds: str) -> bool:
    checks = (verification or {}).get("checks")
    if not isinstance(checks, list):
        return False
    expected = {str(kind).strip() for kind in kinds if str(kind).strip()}
    for item in checks:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip() in expected:
            return True
    return False


def _verification_has_path_exists(verification: dict | None, path: str) -> bool:
    checks = (verification or {}).get("checks")
    if not isinstance(checks, list):
        return False
    expected_path = str(path or "").strip()
    if not expected_path:
        return False
    for item in checks:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip() != "path_exists":
            continue
        if str(item.get("path") or "").strip() == expected_path:
            return True
    return False


def _verification_has_json_marker_check(
    verification: dict | None,
    *,
    kind: str,
    marker_path: str,
    field: str,
) -> bool:
    checks = (verification or {}).get("checks")
    if not isinstance(checks, list):
        return False
    expected_kind = str(kind or "").strip()
    expected_path = str(marker_path or "").strip()
    expected_field = str(field or "").strip()
    if not expected_kind or not expected_path or not expected_field:
        return False
    for item in checks:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip() != expected_kind:
            continue
        path = str(item.get("path") or "").strip()
        if not path.lower().endswith(".json"):
            continue
        actual_field = str(
            item.get("field")
            or ("installed_exe" if expected_kind == "json_marker_valid_exe" else "installer_path")
        ).strip()
        if path == expected_path and actual_field == expected_field:
            return True
    return False


def _looks_like_download_chunk(title: str, agent_prompt: str) -> bool:
    combined = f"{title}\n{agent_prompt}".lower()
    install_like_markers = (
        "launch the downloaded",
        "launch the installer",
        "launch the ",
        "run installer",
        "run the installer",
        "run the downloaded",
        "execute the installer",
        "installer wizard",
        "drive the windows installer",
        "drive the installer",
        "uac prompt",
        "uac or permission prompt",
        "permission prompt",
        "설치 파일 실행",
        "설치 마법사",
        "설치 관리자",
        "설치 진행",
        "설치 완료",
        "설치 후",
        "다운로드된",
    )
    if any(token in combined for token in install_like_markers):
        return False
    if any(
        token in combined
        for token in (
            "subprocess",
        )
    ):
        return False
    download_markers = (
        "download",
        "다운로드",
        "save into",
        "save it into",
        "save to",
        "fetch first",
        "resolve the actual",
        "resolve the installer",
        "downloaded installer artifact",
        "downloads folder",
        "download completed",
        "windows installer `.exe` only",
        "windows installer `.msi` only",
        "installer `.exe` only",
        "installer `.msi` only",
    )
    has_artifact_hint = any(ext in combined for ext in (".exe", ".msi", ".zip", ".alz")) or any(
        token in combined for token in ("archive package", "installer package", "contained installer", "extract it")
    )
    return has_artifact_hint and any(
        token in combined for token in download_markers
    )


def _looks_like_navigation_only_chunk(chunk: TeacherTaskChunk) -> bool:
    combined = f"{chunk.title}\n{chunk.agent_prompt}".lower()
    if _chunk_has_verification_kind(chunk, "file_exists_glob", "file_size_gt", "process_exists"):
        return False
    if any(ext in combined for ext in (".exe", ".msi")) and any(token in combined for token in ("download", "다운로드", "downloads folder")):
        return False
    navigation_markers = (
        "open a browser",
        "navigate to",
        "official page",
        "landing page",
        "download page",
        "official site",
        "locate the download",
        "locate the installer",
        "identify the installer",
        "브라우저",
        "페이지",
        "공식 페이지",
    )
    return any(marker in combined for marker in navigation_markers)


def _looks_like_install_execution_chunk(title: str, agent_prompt: str) -> bool:
    combined = f"{title}\n{agent_prompt}".lower()
    if any(token in combined for token in ("login screen", "로그인 화면", "launch marker", "foreground")):
        return False
    download_stage_markers = (
        "download the official windows installer",
        "download the windows installer",
        "download the installer",
        "download the official",
        "save it to",
        "save it into",
        "downloads folder",
        "download completed",
        "다운로드하세요",
        "다운로드가 끝나면",
        "다운로드 버튼",
        "다운로드 진행 ui",
    )
    strong_install_markers = (
        "run the installer",
        "launch the installer",
        "execute the installer",
        "installer wizard",
        "uac prompt",
        "license dialog",
        "destination dialog",
        "completion dialog",
        "do not download anything in this chunk",
        "설치 파일 실행",
        "설치 마법사",
        "설치 관리자",
        "설치 진행",
        "설치 완료",
        "권한 창",
    )
    install_run_markers = (
        "launch the downloaded",
        "launch the installer",
        "run installer",
        "run the installer",
        "execute the installer",
        "installer wizard",
        "uac prompt",
        "license dialog",
        "destination dialog",
        "completion dialog",
        "do not download anything in this chunk",
        "uac or permission prompt",
        "permission prompt",
        "approve it",
        "complete the installation",
        "complete the setup",
        "finish install",
        "finish setup",
        "proceed through the installer",
        "after installation finishes",
        "installation finishes",
        "drive the windows installer",
        "drive the installer",
        "설치 파일 실행",
        "설치 마법사",
        "설치 관리자",
        "설치 진행",
        "설치 완료",
        "권한 창",
        "실행",
    )
    if any(marker in combined for marker in download_stage_markers) and not any(marker in combined for marker in strong_install_markers):
        return False
    installer_artifact_markers = (
        ".exe",
        ".msi",
        ".zip",
        ".alz",
        " msi",
        "msi ",
        " installer",
        "installer executable",
        "setup executable",
        "archive package",
        "installer package",
    )
    return any(ext in combined for ext in installer_artifact_markers) and any(marker in combined for marker in install_run_markers)


def _looks_like_launch_execution_chunk(title: str, agent_prompt: str) -> bool:
    combined = f"{title}\n{agent_prompt}".lower()
    if any(ext in combined for ext in (".exe", ".msi", " msi", "msi ", " installer")) and any(token in combined for token in ("installer", "setup", "run installer", "launch the downloaded")):
        return False
    return any(
        token in combined
        for token in (
            "launch the app",
            "launch the installed app",
            "login screen",
            "로그인 화면",
            "start menu",
            "foreground",
            "already running",
            "running",
            "launch marker",
        )
    )


def _merge_gui_first_navigation_chunks(chunks: list[TeacherTaskChunk]) -> list[TeacherTaskChunk]:
    if len(chunks) < 2:
        return chunks
    merged: list[TeacherTaskChunk] = []
    index = 0
    while index < len(chunks):
        current = chunks[index]
        next_chunk = chunks[index + 1] if index + 1 < len(chunks) else None
        if (
            next_chunk is not None
            and _looks_like_navigation_only_chunk(current)
            and _looks_like_download_chunk(next_chunk.title, next_chunk.agent_prompt)
        ):
            merged_prompt = (
                "If the current browser or desktop state has not yet reached the official download UI, "
                "complete that navigation first and then continue directly into the download in the same chunk.\n\n"
                f"Navigation stage to preserve:\n{current.agent_prompt.strip()}\n\n"
                f"Download stage:\n{next_chunk.agent_prompt.strip()}"
            )
            merged_preconditions = list(dict.fromkeys([*current.preconditions, *next_chunk.preconditions]))
            merged_notes = list(dict.fromkeys([*next_chunk.notes, "merged_prior_navigation_chunk"]))
            merged.append(
                TeacherTaskChunk(
                    chunk_id=next_chunk.chunk_id,
                    title=next_chunk.title,
                    agent_prompt=merged_prompt,
                    success_hint=next_chunk.success_hint,
                    preconditions=merged_preconditions,
                    verification=next_chunk.verification,
                    max_retries=next_chunk.max_retries,
                    on_fail=next_chunk.on_fail,
                    notes=merged_notes,
                )
            )
            index += 2
            continue
        merged.append(current)
        index += 1
    return merged


def _task_explicitly_requests_checksum(task: str) -> bool:
    lowered = str(task or "").lower()
    return any(token in lowered for token in ("checksum", "sha256", "sha-256", "hash", "체크섬", "해시", "무결성"))


def _looks_like_checksum_only_chunk(chunk: TeacherTaskChunk) -> bool:
    combined = f"{chunk.title}\n{chunk.agent_prompt}\n{chunk.success_hint or ''}".lower()
    checksum_markers = ("checksum", "sha256", "sha-256", "hash", "sums.txt", "checksums", "체크섬", "해시", "무결성")
    if not any(marker in combined for marker in checksum_markers):
        return False
    install_progress_markers = (
        "msiexec",
        "run the installer",
        "launch the installer",
        "execute the installer",
        "installer wizard",
        "complete the installation",
        "install and verify",
        "설치 파일 실행",
        "설치 진행",
        "설치 완료",
    )
    return not any(marker in combined for marker in install_progress_markers)


def _remove_optional_checksum_chunks(chunks: list[TeacherTaskChunk], *, source_task: str) -> list[TeacherTaskChunk]:
    if not chunks or not _looks_like_install_task(source_task) or _task_explicitly_requests_checksum(source_task):
        return chunks
    filtered: list[TeacherTaskChunk] = []
    removed_any = False
    for chunk in chunks:
        if _looks_like_checksum_only_chunk(chunk):
            removed_any = True
            continue
        filtered.append(chunk)
    if not removed_any:
        return chunks
    checksum_markers = ("checksum", "sha256", "sha-256", "hash", "체크섬", "해시", "무결성")
    sanitized: list[TeacherTaskChunk] = []
    for chunk in filtered:
        preconditions = [
            item
            for item in chunk.preconditions
            if not any(marker in str(item).lower() for marker in checksum_markers)
        ]
        notes = list(dict.fromkeys([*chunk.notes, "optional_checksum_chunk_removed"]))
        sanitized.append(
            TeacherTaskChunk(
                chunk_id=chunk.chunk_id,
                title=chunk.title,
                agent_prompt=chunk.agent_prompt,
                success_hint=chunk.success_hint,
                preconditions=preconditions,
                verification=chunk.verification,
                max_retries=chunk.max_retries,
                on_fail=chunk.on_fail,
                notes=notes,
            )
        )
    return sanitized or chunks


def _normalize_windows_installer_agent_prompt(
    *,
    source_task: str,
    title: str,
    agent_prompt: str,
    source_text: str | None = None,
    execution_style: str = "python_first",
) -> str:
    raw = str(agent_prompt or "").strip()
    if not raw:
        return raw
    raw = _remove_non_user_urls_from_prompt(source_task, raw)
    normalized_style = _normalize_execution_style(execution_style)
    normalized = raw
    archive_allowed = _source_allows_official_archive_package(source_task, title, raw, source_text or "")
    common_python_hint = (
        "이 chunk는 실행 가능한 Python 코드만으로 수행하세요. "
        "다운로드는 curl, wget, powershell, http.server 같은 외부 도구 대신 Python HTTP와 파일 I/O를 사용하세요. "
        "검색이 필요하면 자연어 문장 전체를 검색창에 넣지 말고, 대상 앱 핵심 키워드 2~5개와 필요한 플랫폼 힌트(`pc`, `windows`) 정도만 짧게 사용하세요. "
        "Windows 사용자 폴더 경로는 %USERPROFILE% 문자열을 그대로 쓰지 말고 os.environ, os.path.expandvars, Path.home() 등으로 실제 경로를 해석하세요. "
        "chunk prompt 안에 이미 URL이 있으면 그 exact URL부터 먼저 fetch하고, 비슷해 보이는 다른 host나 guessed latest path로 바꾸지 마세요. "
        "landing page에 raw installer 링크가 바로 없으면 같은 스크립트 안에서 다른 관련 페이지도 확인하세요. "
        "HTML에서 실제로 확인하지 않은 `/files/latest`, `/download/latest` 같은 guessed artifact 디렉터리를 HTML page처럼 바로 열지 마세요. "
        "HTML의 relative href/src 링크는 urllib.parse.urljoin 으로 base page에 대해 절대 URL로 변환해서 검사하세요. "
        "installer 링크가 버전 숫자를 포함한다고 가정하지 말고, relative 또는 absolute `.exe` 또는 `.msi` 링크를 넓게 수집한 뒤 HTTP 요청으로 검증하세요. "
        "HTML에서는 href만 보지 말고 absolute https .exe URL 후보와 .msi URL 후보도 찾고, 선택한 URL은 실제 HTTP 요청으로 검증하세요. "
        "다운로드나 설치가 실패하면 예외를 발생시키거나 non-zero로 종료하세요."
    )
    install_python_hint = (
        "이 install chunk에서는 새 다운로드 helper나 URL 탐색 로직을 만들지 말고, 이미 내려받은 installer `.exe` 또는 `.msi`를 바로 찾는 코드부터 시작하세요. "
        "함수 여러 개나 main()을 만들지 말고 top-level 직선 코드로 작성하세요. "
        "처음 25줄 안에서 Downloads 안의 installer 경로를 찾고, silent install 시도를 시작하세요. "
        "경로는 Path.home() 이나 os.environ 으로 실제 Windows 경로를 해석하세요. "
        "silent install은 `/VERYSILENT`, `/SILENT`, `/SP-`, `/NORESTART` 같은 일반적인 Windows installer switch 조합을 우선 시도하세요. "
        "설치 후에는 `%LOCALAPPDATA%`, `%ProgramFiles%`, `%ProgramFiles(x86)%` 아래의 일반적인 설치 경로에서 대상 앱 `.exe`를 찾고, 찾으면 즉시 실행한 뒤 프로세스가 뜰 때까지 확인하세요. "
        "silent install이 분명히 실패하거나 timeout이 나면 같은 silent command를 반복하지 말고 Python GUI 자동화로 현재 설치 창을 진행하세요."
    )
    if archive_allowed:
        gui_download_hint = (
            "이 chunk는 실행 가능한 Python 코드만으로 수행하세요. "
            "현재 스크린샷에 브라우저, 검색 결과, 다운로드 페이지, 다운로드 버튼, 다운로드 진행 UI가 보이면 그 보이는 UI를 Python GUI 자동화로 이어서 사용하세요. "
            "현재 화면에 근거가 없을 때만 새 페이지를 여세요. "
            "검색이 필요하면 자연어 문장 전체를 검색창에 넣지 말고, 대상 앱 핵심 키워드 2~5개와 필요한 플랫폼 힌트(`pc`, `windows`) 정도만 짧게 사용하세요. "
            "근거 있는 visible browser/download UI가 이미 있으면 그 chunk 안에서 새 urllib/requests HTML scraping이나 fresh direct fetch로 갈아타지 말고, 먼저 그 UI를 끝까지 진행하세요. "
            "보이는 download/install control 이 있으면 현재 스크린샷을 보고 페이지 본문 영역의 좌표나 키보드 이동을 고르세요. direct installer URL을 추측해서 바로 받으려 하지 마세요. "
            "같은 페이지를 다시 시도할 때는 같은 탭/페이지를 유지하고, 이전에 실패한 지점과 다른 page-content 후보를 몇 개 순차적으로 시도하세요. "
            "브라우저 주소창, 탭 줄, 북마크 바, 빈 여백은 download 후보로 취급하지 마세요. "
            "작업과 관련된 vendor, product, download 페이지를 우선하세요. `.exe` 또는 `.msi`가 있으면 그것을 우선하고, 페이지가 설치용 `.zip` 또는 `.alz` 패키지만 제공하면 그 archive를 사용해도 됩니다."
        )
    else:
        gui_download_hint = (
            "이 chunk는 실행 가능한 Python 코드만으로 수행하세요. "
            "현재 스크린샷에 브라우저, 검색 결과, 다운로드 페이지, 다운로드 버튼, 다운로드 진행 UI가 보이면 그 보이는 UI를 Python GUI 자동화로 이어서 사용하세요. "
            "현재 화면에 근거가 없을 때만 새 페이지를 여세요. "
            "검색이 필요하면 자연어 문장 전체를 검색창에 넣지 말고, 대상 앱 핵심 키워드 2~5개와 필요한 플랫폼 힌트(`pc`, `windows`) 정도만 짧게 사용하세요. "
            "근거 있는 visible browser/download UI가 이미 있으면 그 chunk 안에서 새 urllib/requests HTML scraping이나 fresh direct fetch로 갈아타지 말고, 먼저 그 UI를 끝까지 진행하세요. "
            "보이는 download/install control 이 있으면 현재 스크린샷을 보고 페이지 본문 영역의 좌표나 키보드 이동을 고르세요. direct installer URL을 추측해서 바로 받으려 하지 마세요. "
            "같은 페이지를 다시 시도할 때는 같은 탭/페이지를 유지하고, 이전에 실패한 지점과 다른 page-content 후보를 몇 개 순차적으로 시도하세요. "
            "브라우저 주소창, 탭 줄, 북마크 바, 빈 여백은 download 후보로 취급하지 마세요. "
            "작업과 관련된 vendor, product, download 페이지를 우선하세요. `.exe` 또는 `.msi`가 있으면 그것을 우선하고, 페이지가 설치용 `.zip` 또는 `.alz` 패키지만 제공하면 그 archive를 사용해도 됩니다."
        )
    gui_install_hint = (
        "이 install chunk는 실행 가능한 Python 코드만으로 수행하세요. "
        "현재 스크린샷이나 데스크톱 상태에 installer wizard, UAC prompt, license dialog, destination dialog, completion dialog가 보이면 "
        "그 visible installer UI를 Python GUI 자동화로 먼저 진행하세요. 현재 화면에 설치 UI가 없을 때만 이미 다운로드된 installer `.exe` 또는 `.msi`를 다시 실행하세요. "
        "작업 도중 새 다운로드 helper나 URL 탐색 로직을 추가하지 마세요."
    )
    source_hint = _official_source_hint(source_task, title, raw, source_text or "")
    exact_page_hint = _exact_official_page_hint(source_task, raw, source_text or "")
    raw_install_chunk = _looks_like_install_execution_chunk(title, raw)
    raw_download_chunk = _looks_like_download_chunk(title, raw)
    if raw_install_chunk:
        install_hint = (
            "실행할 installer는 현재 작업 대상 앱과 일치하는 `.exe` 또는 `.msi`만 고르세요.\n"
            + _matching_installer_hint(source_task=source_task, title=title, agent_prompt=raw, action="실행")
        )
        preferred_install_hint = gui_install_hint if normalized_style == "gui_first" else install_python_hint
        if preferred_install_hint not in normalized:
            normalized = f"{preferred_install_hint}\n\n{normalized}"
        if archive_allowed:
            archive_install_hint = (
                "관련 source가 설치용 `.zip` 또는 `.alz` 패키지만 제공한 경우에는 먼저 그 archive를 Downloads 안에서 추출하고, "
                "추출된 폴더 안의 실제 installer `.exe` 또는 `.msi`를 찾아 그 설치 UI를 진행하세요."
            )
            if archive_install_hint not in normalized:
                normalized = f"{archive_install_hint}\n\n{normalized}"
        if install_hint not in normalized:
            normalized = f"{install_hint}\n\n{normalized}"
        if exact_page_hint and exact_page_hint not in normalized:
            normalized = f"{normalized}\n\n{exact_page_hint}"
        return _remove_non_user_urls_from_prompt(source_task, normalized)
    if raw_download_chunk:
        if archive_allowed:
            normalized = normalized.replace(
                "Do not use `.zip`, portable, or archive downloads.",
                "Prefer `.exe` or `.msi` when the current page provides them. If the page for this task only exposes a `.zip` or `.alz` installer package, download that archive instead of switching to another host.",
            )
            normalized = normalized.replace(
                "여전히 공식 vendor source를 우선하고 `.zip`이나 archive가 아니라 Windows installer `.exe` 또는 `.msi`를 선택하세요.",
                "관련 vendor, product, download 페이지를 우선하세요. `.exe` 또는 `.msi`가 있으면 그것을 우선하고, 페이지가 설치용 `.zip` 또는 `.alz` 패키지만 제공하면 그 archive를 사용해도 됩니다.",
            )
        reuse_hint = (
            "이미 Downloads 폴더에 사용할 수 있는 대상 앱의 Windows installer `.exe` 또는 `.msi`가 있으면 새로 받지 말고 그 파일을 그대로 사용해도 됩니다.\n"
            + _matching_installer_hint(source_task=source_task, title=title, agent_prompt=raw, action="사용")
        )
        context_hint = (
            "If you confirm or obtain a valid installer/archive for this task, write the soft continuity file "
            "`~/Downloads/computer-use-agent-context.json` with `installer_path`, `source_url`, and `target_keywords` so verification and later chunks can continue from the exact artifact."
        )
        preferred_download_hint = gui_download_hint if normalized_style == "gui_first" else common_python_hint
        if preferred_download_hint not in normalized:
            normalized = f"{preferred_download_hint}\n\n{normalized}"
        if source_hint and source_hint not in normalized:
            normalized = f"{normalized}\n\n{source_hint}"
        if exact_page_hint and exact_page_hint not in normalized:
            normalized = f"{normalized}\n\n{exact_page_hint}"
        if reuse_hint not in normalized:
            normalized = f"{normalized}\n\n{reuse_hint}"
        if normalized_style == "gui_first" and context_hint not in normalized:
            normalized = f"{normalized}\n\n{context_hint}"
    return _remove_non_user_urls_from_prompt(source_task, normalized)


def _normalize_general_gui_agent_prompt(
    *,
    title: str,
    agent_prompt: str,
    execution_style: str = "python_first",
) -> str:
    raw = str(agent_prompt or "").strip()
    if not raw:
        return raw
    normalized_style = _normalize_execution_style(execution_style)
    combined = f"{title}\n{raw}".lower()
    if any(keyword in combined for keyword in ("download", "다운로드", "installer", ".exe", ".msi", "setup", "설치")):
        return raw
    if not any(keyword in combined for keyword in ("open", "launch", "create", "click", "project", "window", "menu", "dialog", "파일", "열", "실행", "생성", "프로젝트", "창", "메뉴")):
        return raw
    if normalized_style == "gui_first":
        continue_hint = (
            "현재 앱이나 창이 이미 열려 있으면 그 상태를 이어서 사용하고, 작업과 무관한 재설치나 재다운로드는 하지 마세요.\n"
            "브라우저, 앱 창, 메뉴, 다이얼로그처럼 현재 캡처에 보이는 UI를 Python GUI 자동화로 이어서 사용하는 쪽을 우선하세요."
        )
    else:
        continue_hint = (
            "현재 앱이나 창이 이미 열려 있으면 그 상태를 이어서 사용하고, 작업과 무관한 재설치나 재다운로드는 하지 마세요.\n"
            "사람이 수동으로 조작한다고 가정하지 말고, 캡처 화면을 보고 판단한 뒤 실행 가능한 Python 코드만으로 GUI 상태 확인과 조작을 수행하세요."
        )
    if continue_hint in raw:
        return raw
    return f"{raw}\n\n{continue_hint}"


def _local_install_chunks(
    task: str,
    *,
    execution_style: str = "python_first",
    staging_subdir: str | None = None,
) -> list[TeacherTaskChunk]:
    normalized_style = _normalize_execution_style(execution_style)
    keyword = (_target_installer_keywords(task, limit=1) or ["targetapp"])[0]
    discovered_official_urls = []
    if _extract_urls_from_text(task):
        discovered_official_urls = _discover_official_page_urls(task, limit=2)
    discovered_official_urls = [
        url for url in discovered_official_urls if _looks_like_plausible_official_page_url(url)
    ]
    if not _task_explicitly_requests_store(task):
        non_store_urls = [url for url in discovered_official_urls if "apps.microsoft.com" not in str(url).lower()]
        if non_store_urls:
            discovered_official_urls = non_store_urls
    discovered_keyword_parts = [text for text in (_url_keyword_text(url) for url in discovered_official_urls) if text]
    discovered_source_hint = ""
    if discovered_official_urls:
        discovered_lines = "\n".join(f"- {url}" for url in discovered_official_urls)
        discovered_source_hint = (
            "Use these exact official page URLs first before any search engine result or inferred domain:\n"
            f"{discovered_lines}\n"
            "Prefer the first URL if it already looks like the primary vendor or product landing page."
        )
    target_keywords: list[str] = []
    task_keywords = _target_installer_keywords(task, limit=6)
    discovered_keywords = _target_installer_keywords(*discovered_keyword_parts, limit=6)
    for token in task_keywords:
        if token not in target_keywords:
            target_keywords.append(token)
    task_has_ascii_keyword = any(re.search(r"[a-z]", token) for token in target_keywords)
    if not task_has_ascii_keyword:
        for token in discovered_keywords:
            if re.search(r"[a-z]", token) is None:
                continue
            if token not in target_keywords:
                target_keywords.append(token)
            if len(target_keywords) >= 6:
                break
    staging_subdir = str(staging_subdir or _task_staging_subdir(task)).strip()
    keyword_download_glob = _downloads_installer_glob(task, *discovered_keyword_parts)
    if keyword_download_glob:
        downloads_root = "%USERPROFILE%\\\\Downloads\\\\"
        downloads_glob = keyword_download_glob
        install_marker_path = "~/Downloads/install-success.json"
        launch_marker_path = "~/Downloads/launch-success.json"
        context_path = "~/Downloads/computer-use-agent-context.json"
    else:
        downloads_root = f"%USERPROFILE%\\\\Downloads\\\\computer-use-agent\\\\{staging_subdir}\\\\"
        downloads_glob = f"~/Downloads/computer-use-agent/{staging_subdir}/*.exe"
        install_marker_path = f"~/Downloads/computer-use-agent/{staging_subdir}/install-success.json"
        launch_marker_path = f"~/Downloads/computer-use-agent/{staging_subdir}/launch-success.json"
        context_path = f"~/Downloads/computer-use-agent/{staging_subdir}/computer-use-agent-context.json"
    likely_install_paths = _likely_install_path_hint(keyword)
    archive_install_hint = (
        f"If the official downloaded installer package for this task is `.zip` or `.alz`, extract it inside `{downloads_root}` first, "
        "then find the contained installer `.exe` or `.msi` and continue from that extracted installer."
    )
    download_title = f"Download {keyword} installer"
    install_title = f"Install {keyword}"
    launch_title = f"Launch {keyword}"
    if normalized_style == "gui_first":
        download_verification_checks = [
            {
                "kind": "json_marker_valid_installer",
                "path": context_path,
                "field": "installer_path",
                "keywords": target_keywords,
                "bytes": 1000000,
                "allowed_suffixes": [".exe", ".msi", ".zip", ".alz"],
            }
        ]
    else:
        download_verification_checks = [
            {"kind": "file_exists_glob", "pattern": downloads_glob},
            {"kind": "file_size_gt", "pattern": downloads_glob, "bytes": 1000000},
        ]
    if normalized_style == "gui_first":
        download_prompt = (
            f"Use executable Python on the Windows machine to obtain the Windows installer `.exe` or `.msi` for the target app from this task: {task}. "
            f"First inspect the current screenshot and desktop state. If a browser, search results page, relevant vendor/product/download page, or download control is already visible, continue from that visible UI with Python automation and download the installer into `{downloads_root}`. "
            f"If no grounded visible UI exists yet, open a relevant vendor, product, or download page in Python and continue there. "
            f"If the soft continuity file `{context_path}` already points to a valid installer for this task and that file still exists, you may reuse it instead of downloading again, but validate it before trusting it. "
            f"Do not require the final downloaded filename to contain the app name; browser downloads may use temporary or vendor-specific filenames. "
            f"If you confirm or obtain a valid installer, update `{context_path}` with the exact installer path, source URL, and target keywords so later chunks can continue from it. "
            "Prefer `.exe` or `.msi` installers when the current page exposes them directly. "
            "If the relevant page only exposes a Windows installer package as `.zip` or `.alz`, downloading that archive is allowed."
        )
        install_prompt = (
            f"{_matching_installer_hint(source_task=task, title=install_title, agent_prompt=task, action='실행')} "
            f"Use executable Python only. Do not download anything in this chunk. "
            f"First inspect the current screenshot and desktop state for an installer wizard, UAC prompt, license dialog, destination dialog, or completion dialog, and drive that visible UI forward if present. "
            f"Launching only the final app executable is not enough for this chunk. "
            f"Prefer reading the soft continuity file `{context_path}` first; if it names a valid installer path for this task, reuse that exact path instead of searching Downloads broadly, but ignore it if validation fails. "
            f"If no installer UI is visible yet, find the existing installer `.exe` or `.msi` in `{downloads_root}`, launch it once, and then continue from the resulting installer UI. "
            f"{archive_install_hint} "
            f"Only after installer progression should you check likely install directories such as `{likely_install_paths}` for the installed app executable. Avoid recursively scanning the whole of `%LOCALAPPDATA%` or `%ProgramFiles%`. "
            f"Do not import `pywin32`, `pywinauto`, `win32gui`, `win32con`, `win32api`, or `pythoncom`. Prefer the standard library, `psutil`, `pyautogui`, and `pygetwindow` only if clearly needed. "
            f"Fail explicitly if no valid installer is found or if the install still has not produced the app executable. "
            f"End only when the installed app `.exe` exists on disk, `{install_marker_path}` contains the discovered executable path, and `{context_path}` is updated with the same installed executable."
        )
    else:
        download_prompt = (
            f"Use executable Python on the Windows machine to download the Windows installer `.exe` or `.msi` for the target app from this task: {task}. "
            f"Prefer a relevant vendor, product, or download page, resolve the current Windows x86_64 setup `.exe` or `.msi` URL in Python, and save it to `{downloads_root}`. "
            f"If the soft continuity file `{context_path}` already points to a valid installer for this task and that file still exists, you may reuse it instead of downloading again, but validate it before trusting it. "
            f"If one page does not expose a raw `.exe` link or `.msi` link, inspect another relevant page in the same script before failing. "
            f"After you confirm or obtain a valid installer, update `{context_path}` with the exact installer path. "
            "Prefer `.exe` or `.msi` installers when the current page exposes them directly. "
            "If the relevant page only exposes a Windows installer package as `.zip` or `.alz`, downloading that archive is allowed."
        )
        install_prompt = (
            f"{_matching_installer_hint(source_task=task, title=install_title, agent_prompt=task, action='실행')} "
            f"Use executable Python only. Do not download anything in this chunk. "
            f"First inspect the current screenshot and desktop state for an installer wizard, UAC prompt, license dialog, destination dialog, or completion dialog, and drive that UI forward if it is visible. "
            f"Launching only the final app executable is not enough for this chunk. "
            f"Prefer reading the soft continuity file `{context_path}` first; if it names a valid installer path for this task, reuse that exact path instead of searching Downloads broadly, but ignore it if validation fails. "
            f"If the app is not already installed, the script must either launch the installer `.exe` or `.msi` itself or operate a visible installer window with Python GUI automation. "
            f"If no installer UI is visible, find the existing installer `.exe` or `.msi` in `{downloads_root}`, launch it once, wait long enough for setup to appear or continue, and then check only likely install directories such as `{likely_install_paths}` for the installed app executable. Avoid recursively scanning the whole of `%LOCALAPPDATA%` or `%ProgramFiles%`. "
            f"{archive_install_hint} "
            f"Do not import `pywin32`, `pywinauto`, `win32gui`, `win32con`, `win32api`, or `pythoncom`. Prefer the standard library, `psutil`, `pyautogui`, and `pygetwindow` only if clearly needed. "
            f"Fail explicitly if no valid installer is found or if the install still has not produced the app executable. "
            f"End only when the installed app `.exe` exists on disk, `{install_marker_path}` contains the discovered executable path, and `{context_path}` is updated with the same installed executable."
        )
    if discovered_source_hint:
        download_prompt = f"{download_prompt}\n\n{discovered_source_hint}"
    launch_prompt = (
        f"Use executable Python on the Windows machine to locate the already-installed app executable for the target app from this task: {task}, "
        f"prefer reading `{install_marker_path}` and the soft continuity file `{context_path}` first if they exist, launch the app once, bring the app window to the foreground if needed, and end only after the app process is running. "
        f"Do not redownload or reinstall the app in this chunk. Prefer existing install paths under `%LOCALAPPDATA%`, `%ProgramFiles%`, and `%ProgramFiles(x86)%`, and if the app is already running just verify that process and focus the window. "
        f"Write `{launch_marker_path}` only after the launch succeeded, and update `{context_path}` with the launched executable path."
    )
    return [
        TeacherTaskChunk(
            chunk_id="chunk-001",
            title=download_title,
            agent_prompt=_normalize_windows_installer_agent_prompt(
                source_task=task,
                title=download_title,
                agent_prompt=download_prompt,
                execution_style=normalized_style,
            ),
            success_hint=f"A target-app installer `.exe` or `.msi` exists in `{downloads_root}` and is non-empty.",
            preconditions=[
                "Windows desktop session is available and Python can run.",
                "The machine has network access to the official vendor or release pages.",
            ],
            verification={
                "checks": download_verification_checks
            },
            max_retries=1,
            on_fail="retry_current_chunk",
            notes=["local_teacher_fallback", f"{normalized_style}_download_chunk"],
        ),
        TeacherTaskChunk(
            chunk_id="chunk-002",
            title=install_title,
            agent_prompt=_normalize_windows_installer_agent_prompt(
                source_task=task,
                title=install_title,
                agent_prompt=install_prompt,
                execution_style=normalized_style,
            ),
            success_hint=f"The install marker `{install_marker_path}` exists and points to the installed app executable.",
            preconditions=[
                f"A non-empty target-app installer `.exe` or `.msi` already exists in `{downloads_root}`.",
            ],
            verification={
                "checks": [
                    {"kind": "path_exists", "path": install_marker_path},
                    {
                        "kind": "json_marker_valid_exe",
                        "path": install_marker_path,
                        "field": "installed_exe",
                        "keywords": target_keywords,
                    },
                ]
            },
            max_retries=2,
            on_fail="retry_current_chunk",
            notes=["local_teacher_fallback", f"{normalized_style}_install_chunk"],
        ),
        TeacherTaskChunk(
            chunk_id="chunk-003",
            title=launch_title,
            agent_prompt=_normalize_general_gui_agent_prompt(
                title=launch_title,
                agent_prompt=launch_prompt,
                execution_style=normalized_style,
            ),
            success_hint=f"The launch marker `{launch_marker_path}` exists after the installed app was started.",
            preconditions=[
                f"The install marker `{install_marker_path}` already exists on disk.",
            ],
            verification={
                "checks": [
                    {"kind": "path_exists", "path": launch_marker_path},
                    {
                        "kind": "json_marker_valid_exe",
                        "path": launch_marker_path,
                        "field": "launched_exe",
                        "keywords": target_keywords,
                    },
                ]
            },
            max_retries=2,
            on_fail="retry_current_chunk",
            notes=["local_teacher_fallback", f"{normalized_style}_launch_chunk"],
        ),
    ]


def build_local_teacher_fallback(
    *,
    task: str,
    prompt: str,
    command_template: str,
    cwd: str | None,
    error: str,
    execution_style: str = "python_first",
    staging_subdir: str | None = None,
) -> tuple[TeacherResult, TeacherChunkPlanResult]:
    normalized_style = _normalize_execution_style(execution_style)
    command_result = _fallback_command_result(
        prompt=prompt,
        command_template=command_template,
        cwd=cwd,
        error=error,
    )
    response_text = (
        "Local fallback planner generated this plan because the external teacher was unavailable. "
        + (
            "Use GUI-first Windows automation: continue from visible browser or installer UI when grounded by the screenshot, and keep all steps executable in Python."
            if normalized_style == "gui_first"
            else "Use Python-first Windows automation: discover the official installer, download it to Downloads, then install and launch the app."
        )
    )
    if _looks_like_install_task(task):
        chunks = _local_install_chunks(
            task,
            execution_style=normalized_style,
            staging_subdir=staging_subdir,
        )
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
                notes=["local_teacher_fallback_generic", normalized_style],
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


def _extract_link_candidate_urls(text: str, *, task: str, limit: int = 5) -> list[str]:
    try:
        payload = _extract_json_object(text)
    except Exception:
        payload = {}
    raw_urls = payload.get("candidate_urls")
    if not isinstance(raw_urls, list):
        raw_urls = re.findall(r"https?://[^\s\"'<>]+", str(text or ""))
    urls: list[str] = []
    for raw_url in raw_urls:
        url = str(raw_url or "").strip().rstrip(".,)")
        if not url:
            continue
        urls.append(url)
    sanitized = _sanitize_discovered_official_urls(task, urls, limit=limit)
    if sanitized:
        return sanitized
    # If the teacher supplied product-specific pages that fail the stricter
    # official-page filter, keep safe non-reference pages rather than losing the retry hint entirely.
    kept: list[str] = []
    seen: set[str] = set()
    task_keywords = [keyword.lower() for keyword in _target_installer_keywords(task, limit=6)]
    for raw_url in urls:
        parsed = urllib.parse.urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        host = str(parsed.netloc or "").lower()
        if any(token in host for token in _NON_VENDOR_RESULT_HOST_TOKENS):
            continue
        if host == "apps.microsoft.com":
            continue
        combined = urllib.parse.unquote(f"{host}{parsed.path}").lower()
        if task_keywords and not any(keyword in combined for keyword in task_keywords if len(keyword) >= 3):
            continue
        if raw_url in seen:
            continue
        seen.add(raw_url)
        kept.append(raw_url)
        if len(kept) >= limit:
            break
    return kept


def _format_replan_link_candidate_exclusions(
    *,
    failed_candidate_urls: list[str] | None = None,
    failed_search_queries: list[str] | None = None,
    limit: int = 8,
) -> str:
    sections: list[str] = []
    normalized_urls: list[str] = []
    normalized_queries: list[str] = []
    seen_urls: set[str] = set()
    seen_queries: set[str] = set()
    for raw_url in failed_candidate_urls or []:
        url = str(raw_url or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        normalized_urls.append(url)
        if len(normalized_urls) >= limit:
            break
    for raw_query in failed_search_queries or []:
        query = re.sub(r"\s+", " ", str(raw_query or "").strip())
        if not query or query in seen_queries:
            continue
        seen_queries.add(query)
        normalized_queries.append(query)
        if len(normalized_queries) >= limit:
            break
    if normalized_urls:
        sections.append(
            "Previously failed URLs to exclude:\n"
            + "\n".join(f"- {url}" for url in normalized_urls)
        )
    if normalized_queries:
        sections.append(
            "Previously failed search queries to exclude:\n"
            + "\n".join(f"- {query}" for query in normalized_queries)
        )
    if not sections:
        return "- None."
    return "\n\n".join(sections)


def run_teacher_link_candidates(
    *,
    task: str,
    chunk_title: str,
    chunk_prompt: str,
    failure_context: str,
    command_template: str,
    cwd: str | None,
    timeout_s: float,
    limit: int = 5,
    failed_candidate_urls: list[str] | None = None,
    failed_search_queries: list[str] | None = None,
) -> TeacherResult:
    prompt = _REPLAN_LINK_CANDIDATE_PROMPT_TEMPLATE.format(
        task=task.strip(),
        chunk_title=chunk_title.strip(),
        chunk_prompt=chunk_prompt.strip(),
        failure_context=failure_context.strip(),
        retry_exclusions=_format_replan_link_candidate_exclusions(
            failed_candidate_urls=failed_candidate_urls,
            failed_search_queries=failed_search_queries,
        ),
    )
    result, response_text = _run_teacher_command(
        prompt=prompt,
        command_template=command_template,
        cwd=cwd,
        timeout_s=timeout_s,
    )
    urls = _extract_link_candidate_urls(response_text, task=task, limit=limit)
    normalized_response = json.dumps(
        {
            "candidate_urls": urls,
            "raw_response": response_text,
        },
        ensure_ascii=False,
        indent=2,
    )
    return TeacherResult(prompt=prompt, response_text=normalized_response, command_result=result)


def _normalize_chunks(
    payload: dict,
    *,
    source_task: str,
    source_text: str,
    execution_style: str = "python_first",
) -> list[TeacherTaskChunk]:
    normalized_style = _normalize_execution_style(execution_style)
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
            source_text=source_text,
            execution_style=normalized_style,
        )
        agent_prompt = _normalize_general_gui_agent_prompt(
            title=title,
            agent_prompt=agent_prompt,
            execution_style=normalized_style,
        )
        success_hint = str(item.get("success_hint") or "").strip() or None
        preconditions = [str(value).strip() for value in item.get("preconditions", []) if str(value).strip()] if isinstance(item.get("preconditions"), list) else []
        raw_verification = item.get("verification")
        verification = raw_verification if isinstance(raw_verification, dict) else None
        verification = _normalize_windows_installer_verification(
            title=title,
            agent_prompt=agent_prompt,
            verification=verification,
        )
        target_keywords = _target_installer_keywords(source_task, title, agent_prompt, limit=6)
        source_target_keywords = _target_installer_keywords(source_task, limit=6) or target_keywords
        if _looks_like_install_task(source_task):
            is_download_stage = _looks_like_download_chunk(title, agent_prompt)
            combined_stage = f"{title}\n{agent_prompt}".lower()
            is_install_stage = _looks_like_install_execution_chunk(title, agent_prompt) or (
                not is_download_stage
                and any(
                    token in combined_stage
                    for token in (
                        "install and launch",
                        "run installer",
                        "run the installer",
                        "launch the installer",
                        "proceed through the installer",
                        "설치 실행",
                        "설치 진행",
                    )
                )
            )
            if normalized_style == "gui_first" and is_download_stage:
                verification = {
                    "checks": [
                        {
                            "kind": "json_marker_valid_installer",
                            "path": "~/Downloads/computer-use-agent-context.json",
                            "field": "installer_path",
                            "keywords": source_target_keywords,
                            "bytes": 1_000_000,
                            "allowed_suffixes": [".exe", ".msi", ".zip", ".alz"],
                        }
                    ]
                }
            elif is_install_stage and not (
                _verification_has_path_exists(verification, "~/Downloads/install-success.json")
                and _verification_has_json_marker_check(
                    verification,
                    kind="json_marker_valid_exe",
                    marker_path="~/Downloads/install-success.json",
                    field="installed_exe",
                )
            ):
                verification = {
                    "checks": [
                        {"kind": "path_exists", "path": "~/Downloads/install-success.json"},
                        {
                            "kind": "json_marker_valid_exe",
                            "path": "~/Downloads/install-success.json",
                            "field": "installed_exe",
                            "keywords": source_target_keywords,
                        },
                    ]
                }
            elif _looks_like_launch_execution_chunk(title, agent_prompt) and not (
                _verification_has_path_exists(verification, "~/Downloads/launch-success.json")
                and _verification_has_json_marker_check(
                    verification,
                    kind="json_marker_valid_exe",
                    marker_path="~/Downloads/launch-success.json",
                    field="launched_exe",
                )
            ):
                verification = {
                    "checks": [
                        {"kind": "path_exists", "path": "~/Downloads/launch-success.json"},
                        {
                            "kind": "json_marker_valid_exe",
                            "path": "~/Downloads/launch-success.json",
                            "field": "launched_exe",
                            "keywords": source_target_keywords,
                        },
                    ]
                }
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
        normalized = _remove_optional_checksum_chunks(normalized, source_task=source_task)
        if (
            _looks_like_install_task(source_task)
            and not _task_explicitly_requests_store(source_task)
            and any(_looks_like_store_detour_prompt(chunk.agent_prompt) for chunk in normalized)
        ):
            return _local_install_chunks(source_task, execution_style=normalized_style)
        if normalized_style == "gui_first":
            normalized = _merge_gui_first_navigation_chunks(normalized)
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
    execution_style: str = "python_first",
) -> TeacherChunkPlanResult:
    normalized_style = _normalize_execution_style(execution_style)
    split_prompt = _SPLIT_PROMPT_TEMPLATE.format(
        task=task.strip(),
        teacher_text=teacher_text.strip(),
        execution_style_guidance=_execution_style_guidance(normalized_style),
    )
    result, response_text = _run_teacher_command(
        prompt=split_prompt,
        command_template=command_template,
        cwd=cwd,
        timeout_s=timeout_s,
    )
    try:
        payload = _extract_json_object(response_text)
        chunks = _normalize_chunks(
            payload,
            source_task=task,
            source_text=teacher_text,
            execution_style=normalized_style,
        )
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
