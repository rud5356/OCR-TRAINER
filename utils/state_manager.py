from __future__ import annotations

import json
import os
from pathlib import Path

from .id_utils import utc_now


def state_path(data_dir: Path, file_id: str) -> Path:
    return data_dir / "working_state" / f"{file_id}_state.json"


def save_state(data_dir: Path, state: dict) -> Path:
    path = state_path(data_dir, state["file_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


def load_state(data_dir: Path, file_id: str) -> dict:
    path = state_path(data_dir, file_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"작업 상태 파일을 읽을 수 없습니다: {path.name}") from exc
    if state.get("file_id") != file_id or not isinstance(state.get("documents"), list):
        raise ValueError("작업 상태 파일 구조가 올바르지 않습니다.")
    return state


def list_states(data_dir: Path) -> list[dict]:
    states = []
    folder = data_dir / "working_state"
    folder.mkdir(parents=True, exist_ok=True)
    for path in sorted(folder.glob("*_state.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            states.append({
                "file_id": state.get("file_id"),
                "filename": state.get("filename", ""),
                "updated_at": state.get("updated_at", ""),
                "completed": bool(state.get("completed")),
            })
        except (OSError, json.JSONDecodeError):
            continue
    return states

