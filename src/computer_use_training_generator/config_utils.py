from __future__ import annotations

import json
from pathlib import Path


_PATH_KEYS = {
    "teacher_workdir",
    "agent_command",
    "agent_model_id",
    "agent_config_path",
    "agent_workdir",
    "output_dir",
}


def _resolve_path_like(value: str, *, config_dir: Path) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate)
    return str((config_dir / candidate).resolve())


def load_generator_config(path: str | None) -> tuple[dict, Path | None]:
    if not path:
        return {}, None
    config_path = Path(path).resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    config_dir = config_path.parent
    for key in _PATH_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            raw[key] = _resolve_path_like(value, config_dir=config_dir)
    return raw, config_path
