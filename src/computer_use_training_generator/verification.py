from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
import time
from typing import Any
import urllib.error
import urllib.request

from .models import ChunkVerificationResult, TeacherTaskChunk


_ALLOWED_CHECK_KINDS = {
    "path_exists",
    "file_exists_glob",
    "file_size_gt",
    "process_exists",
    "json_marker_valid_exe",
    "json_marker_valid_installer",
}

_VERIFICATION_RETRY_DELAY_S = 2.0
_VERIFICATION_RETRY_COUNT = 5


def _strip_screenshot_base64(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_screenshot_base64(item)
            for key, item in value.items()
            if key != "screenshot_base64"
        }
    if isinstance(value, list):
        return [_strip_screenshot_base64(item) for item in value]
    return value


def _normalize_verification_spec(verification: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(verification, dict):
        return None
    raw_checks = verification.get("checks")
    if not isinstance(raw_checks, list):
        return None
    checks: list[dict[str, object]] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in _ALLOWED_CHECK_KINDS:
            continue
        normalized: dict[str, object] = {"kind": kind}
        if kind in {"file_exists_glob", "file_size_gt"}:
            pattern = str(item.get("pattern") or "").strip()
            if not pattern:
                continue
            normalized["pattern"] = pattern
            if kind == "file_size_gt":
                try:
                    normalized["bytes"] = max(0, int(item.get("bytes") or 0))
                except (TypeError, ValueError):
                    continue
        elif kind == "path_exists":
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            normalized["path"] = path
        elif kind == "process_exists":
            name = str(item.get("name") or item.get("process_name") or "").strip()
            if not name:
                continue
            normalized["name"] = name
        elif kind in {"json_marker_valid_exe", "json_marker_valid_installer"}:
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            default_field = "installed_exe" if kind == "json_marker_valid_exe" else "installer_path"
            field = str(item.get("field") or default_field).strip() or default_field
            normalized["path"] = path
            normalized["field"] = field
            if kind == "json_marker_valid_installer":
                try:
                    normalized["bytes"] = max(0, int(item.get("bytes") or 0))
                except (TypeError, ValueError):
                    normalized["bytes"] = 0
            raw_keywords = item.get("keywords")
            if isinstance(raw_keywords, list):
                keywords = []
                for value in raw_keywords:
                    token = str(value or "").strip().lower()
                    if token and token not in keywords:
                        keywords.append(token)
                if keywords:
                    normalized["keywords"] = keywords
        checks.append(normalized)
    if not checks:
        return None
    return {"checks": checks}


def _expanded_glob_patterns(pattern: str) -> list[str]:
    raw = str(pattern or "").strip()
    if not raw:
        return []
    patterns = [raw]
    lowered = raw.lower()

    def _append(candidate: str) -> None:
        if candidate and candidate not in patterns:
            patterns.append(candidate)

    def _collapse_stars(candidate: str) -> str:
        return re.sub(r"\*+", "*", candidate)

    if lowered.endswith("-windows-x86_64.exe"):
        suffix = raw[-len("-windows-x86_64.exe") :]
        base = raw[: -len(suffix)]
        _append(f"{base}-x86_64-setup.exe")
        _append(f"{base}-x86_64-installer.exe")
        _append(f"{base}-setup.exe")
        _append(f"{base}-installer.exe")
    elif lowered.endswith("-windows.exe"):
        suffix = raw[-len("-windows.exe") :]
        base = raw[: -len(suffix)]
        _append(f"{base}-setup.exe")
        _append(f"{base}-installer.exe")
    elif "-windows-*.exe" in lowered:
        base = re.sub(r"-windows-\*\.exe$", "", raw, flags=re.IGNORECASE)
        _append(f"{base}-x86_64-setup.exe")
        _append(f"{base}-x86_64-installer.exe")
        _append(f"{base}-setup.exe")
        _append(f"{base}-installer.exe")

    if lowered.endswith(".exe"):
        installer_alias = re.sub("installer", "setup", raw, flags=re.IGNORECASE)
        setup_alias = re.sub("setup", "installer", raw, flags=re.IGNORECASE)
        if installer_alias != raw:
            _append(_collapse_stars(installer_alias))
        if setup_alias != raw:
            _append(_collapse_stars(setup_alias))
        relaxed = raw
        for token_pattern in (
            r"\*windows\*installer\*",
            r"\*windows\*setup\*",
            r"\*win\*installer\*",
            r"\*win\*setup\*",
            r"\*installer\*",
            r"\*setup\*",
            r"\*windows\*",
            r"\*win\*",
        ):
            candidate = re.sub(token_pattern, "*", raw, flags=re.IGNORECASE)
            if candidate != raw:
                _append(_collapse_stars(candidate))
                relaxed = candidate
        if "installer" in lowered or "setup" in lowered or "windows" in lowered or "win" in lowered:
            _append(_collapse_stars(relaxed))
    for candidate in list(patterns):
        candidate_lower = candidate.lower()
        if candidate_lower.endswith(".exe"):
            _append(candidate[:-4] + ".msi")
        elif candidate_lower.endswith(".msi"):
            _append(candidate[:-4] + ".exe")

    return patterns


def _has_file_based_checks(verification: dict[str, object] | None) -> bool:
    normalized = _normalize_verification_spec(verification)
    if normalized is None:
        return False
    for check in normalized["checks"]:
        kind = str(check.get("kind") or "").strip()
        if kind in {"file_exists_glob", "file_size_gt", "json_marker_valid_installer"}:
            return True
    return False


def build_verification_code(verification: dict[str, object] | None) -> str | None:
    normalized = _normalize_verification_spec(verification)
    if normalized is None:
        return None
    checks_json = json.dumps(normalized["checks"], ensure_ascii=False)
    return f"""import glob
import json
import os
from pathlib import Path
import re
import subprocess

CHECKS = json.loads({checks_json!r})
SYSTEM_APP_NAMES = {{
    "store.exe",
    "applicationframehost.exe",
    "explorer.exe",
    "winget.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
}}


def _glob_paths(pattern):
    expanded = _expanded_glob_patterns(pattern)
    matches = []
    for candidate in expanded:
        for path in sorted(glob.glob(os.path.expanduser(candidate), recursive=True)):
            if path not in matches:
                matches.append(path)
    return expanded, matches


def _expanded_glob_patterns(pattern):
    raw = str(pattern or "").strip()
    if not raw:
        return []
    patterns = [raw]
    lowered = raw.lower()

    def _append(candidate):
        if candidate and candidate not in patterns:
            patterns.append(candidate)

    if lowered.endswith("-windows-x86_64.exe"):
        suffix = raw[-len("-windows-x86_64.exe"):]
        base = raw[:-len(suffix)]
        _append(f"{{base}}-x86_64-setup.exe")
        _append(f"{{base}}-x86_64-installer.exe")
        _append(f"{{base}}-setup.exe")
        _append(f"{{base}}-installer.exe")
    elif lowered.endswith("-windows.exe"):
        suffix = raw[-len("-windows.exe"):]
        base = raw[:-len(suffix)]
        _append(f"{{base}}-setup.exe")
        _append(f"{{base}}-installer.exe")
    elif "-windows-*.exe" in lowered:
        base = re.sub(r"-windows-\\*\\.exe$", "", raw, flags=re.IGNORECASE)
        _append(f"{{base}}-x86_64-setup.exe")
        _append(f"{{base}}-x86_64-installer.exe")
        _append(f"{{base}}-setup.exe")
        _append(f"{{base}}-installer.exe")
    if lowered.endswith(".exe"):
        installer_alias = re.sub("installer", "setup", raw, flags=re.IGNORECASE)
        setup_alias = re.sub("setup", "installer", raw, flags=re.IGNORECASE)
        if installer_alias != raw:
            _append(re.sub(r"\\*+", "*", installer_alias))
        if setup_alias != raw:
            _append(re.sub(r"\\*+", "*", setup_alias))
        relaxed = raw
        for token_pattern in (
            r"\\*windows\\*installer\\*",
            r"\\*windows\\*setup\\*",
            r"\\*win\\*installer\\*",
            r"\\*win\\*setup\\*",
            r"\\*installer\\*",
            r"\\*setup\\*",
            r"\\*windows\\*",
            r"\\*win\\*",
        ):
            candidate = re.sub(token_pattern, "*", raw, flags=re.IGNORECASE)
            if candidate != raw:
                _append(re.sub(r"\\*+", "*", candidate))
                relaxed = candidate
        if "installer" in lowered or "setup" in lowered or "windows" in lowered or "win" in lowered:
            _append(re.sub(r"\\*+", "*", relaxed))
    for candidate in list(patterns):
        candidate_lower = candidate.lower()
        if candidate_lower.endswith(".exe"):
            _append(candidate[:-4] + ".msi")
        elif candidate_lower.endswith(".msi"):
            _append(candidate[:-4] + ".exe")
    return patterns


def _process_listing():
    if os.name == "nt":
        result = subprocess.run(["tasklist"], capture_output=True, text=True, errors="replace", check=False)
        return result.stdout.lower()
    result = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, errors="replace", check=False)
    return result.stdout.lower()


def _normalize_keywords(values):
    keywords = []
    for value in values or []:
        token = str(value or "").strip().lower()
        if token and token not in keywords:
            keywords.append(token)
    return keywords


def _looks_like_invalid_exe_path(path_text):
    lowered = str(path_text or "").lower().replace("\\\\", "/")
    name = os.path.basename(str(path_text or "")).lower()
    if any(token in lowered for token in ("/temp/", "/tmp/", "/appdata/local/temp/", "/winget/")):
        return True
    if name in SYSTEM_APP_NAMES:
        return True
    if any(token in name for token in ("setup", "installer", "install", "unins", "uninstall", "update", "updater", "repair", "clicktorun", "protocolhandler")):
        return True
    return False


def _validate_json_marker_exe(marker_path, field, keywords):
    expanded_marker = os.path.expanduser(str(marker_path or ""))
    marker = Path(expanded_marker)
    entry = {{
        "path": expanded_marker,
        "field": field,
    }}
    if not marker.exists():
        entry["exists"] = False
        return False, entry
    entry["exists"] = True
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:
        entry["error"] = f"invalid_json: {{exc}}"
        return False, entry
    raw_candidate = str((payload or {{}}).get(field) or "").strip().strip('"')
    entry["value"] = raw_candidate
    if not raw_candidate:
        entry["error"] = "missing_field_value"
        return False, entry
    candidate = Path(os.path.expandvars(os.path.expanduser(raw_candidate)))
    entry["resolved_path"] = str(candidate)
    entry["candidate_exists"] = candidate.exists()
    entry["suffix"] = candidate.suffix.lower()
    entry["invalid_path"] = _looks_like_invalid_exe_path(str(candidate))
    normalized_keywords = _normalize_keywords(keywords)
    entry["keywords"] = normalized_keywords
    keyword_hits = [keyword for keyword in normalized_keywords if keyword in str(candidate).lower()]
    entry["keyword_hits"] = keyword_hits
    ok = (
        candidate.exists()
        and candidate.is_file()
        and candidate.suffix.lower() == ".exe"
        and not entry["invalid_path"]
        and (not normalized_keywords or bool(keyword_hits))
    )
    return ok, entry


def _validate_json_marker_installer(marker_path, field, keywords, min_bytes):
    expanded_marker = os.path.expanduser(str(marker_path or ""))
    marker = Path(expanded_marker)
    entry = {{
        "path": expanded_marker,
        "field": field,
    }}
    if not marker.exists():
        entry["exists"] = False
        return False, entry
    entry["exists"] = True
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:
        entry["error"] = f"invalid_json: {{exc}}"
        return False, entry
    if not isinstance(payload, dict):
        payload = {{}}
    raw_candidate = str(payload.get(field) or "").strip().strip('"')
    entry["value"] = raw_candidate
    if not raw_candidate:
        entry["error"] = "missing_field_value"
        return False, entry
    candidate = Path(os.path.expandvars(os.path.expanduser(raw_candidate)))
    entry["resolved_path"] = str(candidate)
    entry["candidate_exists"] = candidate.exists()
    entry["suffix"] = candidate.suffix.lower()
    entry["bytes"] = None
    if candidate.exists() and candidate.is_file():
        try:
            entry["bytes"] = candidate.stat().st_size
        except OSError:
            entry["bytes"] = None
    normalized_keywords = _normalize_keywords(keywords)
    marker_keywords = payload.get("target_keywords") or []
    if not isinstance(marker_keywords, list):
        marker_keywords = []
    source_url = str(payload.get("source_url") or "")
    keyword_haystack = " ".join(
        [
            str(candidate).lower(),
            source_url.lower(),
            " ".join(str(item).lower() for item in marker_keywords),
        ]
    )
    keyword_hits = [keyword for keyword in normalized_keywords if keyword in keyword_haystack]
    entry["keywords"] = normalized_keywords
    entry["marker_target_keywords"] = marker_keywords
    entry["source_url"] = source_url
    entry["keyword_hits"] = keyword_hits
    allowed_suffixes = {{".exe", ".msi"}}
    ok = (
        candidate.exists()
        and candidate.is_file()
        and candidate.suffix.lower() in allowed_suffixes
        and (entry["bytes"] or 0) > int(min_bytes or 0)
        and (not normalized_keywords or bool(keyword_hits))
    )
    return ok, entry


evidence = []
passed = True

for check in CHECKS:
    kind = str(check.get("kind") or "").strip()
    entry = {{"kind": kind}}
    ok = False

    if kind == "path_exists":
        path = os.path.expanduser(str(check.get("path") or ""))
        ok = Path(path).exists()
        entry["path"] = path
        entry["exists"] = ok
    elif kind == "file_exists_glob":
        pattern = str(check.get("pattern") or "")
        searched_patterns, matches = _glob_paths(pattern)
        ok = bool(matches)
        entry["pattern"] = pattern
        entry["searched_patterns"] = searched_patterns
        entry["matches"] = matches
    elif kind == "file_size_gt":
        pattern = str(check.get("pattern") or "")
        threshold = int(check.get("bytes") or 0)
        searched_patterns, matches = _glob_paths(pattern)
        sizes = []
        for path in matches:
            try:
                sizes.append({{"path": path, "bytes": Path(path).stat().st_size}})
            except OSError:
                sizes.append({{"path": path, "bytes": None}})
        ok = any((item.get("bytes") or 0) > threshold for item in sizes)
        entry["pattern"] = pattern
        entry["searched_patterns"] = searched_patterns
        entry["bytes"] = threshold
        entry["matches"] = sizes
    elif kind == "process_exists":
        name = str(check.get("name") or "")
        haystack = _process_listing()
        ok = bool(name) and name.lower() in haystack
        entry["name"] = name
        entry["exists"] = ok
    elif kind == "json_marker_valid_exe":
        path = str(check.get("path") or "")
        field = str(check.get("field") or "installed_exe")
        keywords = check.get("keywords") or []
        ok, marker_entry = _validate_json_marker_exe(path, field, keywords)
        entry.update(marker_entry)
    elif kind == "json_marker_valid_installer":
        path = str(check.get("path") or "")
        field = str(check.get("field") or "installer_path")
        keywords = check.get("keywords") or []
        min_bytes = int(check.get("bytes") or 0)
        ok, marker_entry = _validate_json_marker_installer(path, field, keywords, min_bytes)
        entry.update(marker_entry)
    else:
        entry["error"] = "unsupported_check_kind"

    entry["passed"] = ok
    evidence.append(entry)
    if not ok:
        passed = False

print(json.dumps({{"passed": passed, "evidence": evidence}}, ensure_ascii=False))
"""


def _executor_rpc(endpoint: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/rpc",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_s)) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"verification executor error status={exc.code} body={body!r}") from exc


def _parse_verification_stdout(stdout_tail: str | None) -> tuple[bool, list[dict[str, object]], str | None]:
    if not stdout_tail:
        return False, [], "verification stdout was empty"
    lines = [line.strip() for line in stdout_tail.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            passed = bool(payload.get("passed", False))
            evidence = payload.get("evidence")
            if not isinstance(evidence, list):
                evidence = []
            return passed, [item for item in evidence if isinstance(item, dict)], None
    return False, [], "verification stdout did not contain a JSON result"


def write_verification_artifact(*, session_root: Path, agent_run_label: str, result: ChunkVerificationResult) -> None:
    path = session_root / "agent_runs" / f"{agent_run_label}.verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")


def _run_verification_once(
    *,
    endpoint: str,
    timeout_s: float,
    session_id: str,
    agent_run_label: str,
    chunk: TeacherTaskChunk,
    verification_code: str,
) -> ChunkVerificationResult:
    try:
        payload = _executor_rpc(
            endpoint,
            {
                "action": "execute",
                "python_code": verification_code,
                "run_dir": "",
                "step_id": f"{agent_run_label}-verify",
                "metadata": {
                    "agent_session_id": f"{session_id}-verification",
                    "verification": True,
                    "chunk_id": chunk.chunk_id,
                    "agent_run_label": agent_run_label,
                },
            },
            timeout_s=float(timeout_s),
        )
    except Exception as exc:
        return ChunkVerificationResult(
            verification=chunk.verification,
            verification_code=verification_code,
            passed=False,
            return_code=None,
            evidence=[],
            error=str(exc),
        )
    record = payload.get("record") if isinstance(payload, dict) else None
    return_code = record.get("return_code") if isinstance(record, dict) else None
    stdout_tail = payload.get("stdout_tail") if isinstance(payload, dict) else None
    stderr_tail = payload.get("stderr_tail") if isinstance(payload, dict) else None
    parsed_passed, evidence, parse_error = _parse_verification_stdout(stdout_tail if isinstance(stdout_tail, str) else None)
    if return_code not in (None, 0):
        parsed_passed = False
    error = parse_error
    if return_code not in (None, 0):
        error = error or f"verification executor returned {return_code}"
    return ChunkVerificationResult(
        verification=chunk.verification,
        verification_code=verification_code,
        passed=parsed_passed,
        return_code=return_code if isinstance(return_code, int) else None,
        evidence=evidence,
        stdout_tail=stdout_tail if isinstance(stdout_tail, str) else None,
        stderr_tail=stderr_tail if isinstance(stderr_tail, str) else None,
        error=error,
        executor_payload=_strip_screenshot_base64(payload) if isinstance(payload, dict) else {},
    )


def run_chunk_verification(
    *,
    endpoint: str | None,
    timeout_s: float,
    session_id: str,
    agent_run_label: str,
    chunk: TeacherTaskChunk,
) -> ChunkVerificationResult | None:
    verification_code = build_verification_code(chunk.verification)
    if verification_code is None:
        return None
    if not endpoint:
        return ChunkVerificationResult(
            verification=chunk.verification,
            verification_code=verification_code,
            passed=False,
            return_code=None,
            evidence=[],
            error="agent_endpoint is required for chunk verification",
        )
    result = _run_verification_once(
        endpoint=str(endpoint),
        timeout_s=float(timeout_s),
        session_id=session_id,
        agent_run_label=agent_run_label,
        chunk=chunk,
        verification_code=verification_code,
    )
    if result.passed or not _has_file_based_checks(chunk.verification):
        return result
    if result.return_code not in (None, 0):
        return result
    for _ in range(_VERIFICATION_RETRY_COUNT):
        time.sleep(_VERIFICATION_RETRY_DELAY_S)
        result = _run_verification_once(
            endpoint=str(endpoint),
            timeout_s=float(timeout_s),
            session_id=session_id,
            agent_run_label=agent_run_label,
            chunk=chunk,
            verification_code=verification_code,
        )
        if result.passed or result.return_code not in (None, 0):
            break
    return result
