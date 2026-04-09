from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

from .models import ChunkVerificationResult, TeacherTaskChunk


_ALLOWED_CHECK_KINDS = {
    "path_exists",
    "file_exists_glob",
    "file_size_gt",
    "process_exists",
}


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
        checks.append(normalized)
    if not checks:
        return None
    return {"checks": checks}


def build_verification_code(verification: dict[str, object] | None) -> str | None:
    normalized = _normalize_verification_spec(verification)
    if normalized is None:
        return None
    checks_json = json.dumps(normalized["checks"], ensure_ascii=False)
    return f"""import glob
import json
import os
from pathlib import Path
import subprocess

CHECKS = json.loads({checks_json!r})


def _glob_paths(pattern):
    return sorted(glob.glob(os.path.expanduser(pattern), recursive=True))


def _process_listing():
    if os.name == "nt":
        result = subprocess.run(["tasklist"], capture_output=True, text=True, errors="replace", check=False)
        return result.stdout.lower()
    result = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, errors="replace", check=False)
    return result.stdout.lower()


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
        matches = _glob_paths(pattern)
        ok = bool(matches)
        entry["pattern"] = pattern
        entry["matches"] = matches
    elif kind == "file_size_gt":
        pattern = str(check.get("pattern") or "")
        threshold = int(check.get("bytes") or 0)
        matches = _glob_paths(pattern)
        sizes = []
        for path in matches:
            try:
                sizes.append({{"path": path, "bytes": Path(path).stat().st_size}})
            except OSError:
                sizes.append({{"path": path, "bytes": None}})
        ok = any((item.get("bytes") or 0) > threshold for item in sizes)
        entry["pattern"] = pattern
        entry["bytes"] = threshold
        entry["matches"] = sizes
    elif kind == "process_exists":
        name = str(check.get("name") or "")
        haystack = _process_listing()
        ok = bool(name) and name.lower() in haystack
        entry["name"] = name
        entry["exists"] = ok
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
