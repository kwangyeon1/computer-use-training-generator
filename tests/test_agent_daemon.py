from __future__ import annotations

import json
import threading
import time

from computer_use_training_generator import agent as agent_mod


def test_named_agent_daemon_request_waits_for_complete_json_response(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(agent_mod._RAW_AGENT_STATE_DIRS, "fake-agent", tmp_path)
    requests_dir = tmp_path / "requests"
    responses_dir = tmp_path / "responses"

    def writer() -> None:
        deadline = time.monotonic() + 2.0
        request_path = None
        while time.monotonic() < deadline:
            matches = list(requests_dir.glob("*.json"))
            if matches:
                request_path = matches[0]
                break
            time.sleep(0.01)
        assert request_path is not None
        response_path = responses_dir / request_path.name
        response_path.write_text("{", encoding="utf-8")
        time.sleep(0.1)
        response_path.write_text(json.dumps({"ok": True, "summary": {"run_dir": "run"}}), encoding="utf-8")

    thread = threading.Thread(target=writer)
    thread.start()
    response = agent_mod._send_named_agent_daemon_request(
        "fake-agent",
        {"action": "run"},
        timeout_s=2.0,
    )
    thread.join(timeout=1.0)

    assert response == {"ok": True, "summary": {"run_dir": "run"}}
    assert not list(requests_dir.glob("*.json"))
    assert not list(responses_dir.glob("*.json"))
