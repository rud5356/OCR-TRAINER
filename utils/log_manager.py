from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .id_utils import make_correction_id, utc_now

LOG_FIELDS = (
    "file_id", "document_id", "correction_id", "timestamp", "user_id", "operation",
    "error_type", "before_document_id", "after_document_id", "block_id", "source_block_ids",
    "new_block_ids", "before_position", "after_position", "before_text", "after_text",
    "before_context", "after_context", "diff", "reason", "confidence", "memo",
    "source_document_ids", "new_document_ids", "before_doc_type", "after_doc_type",
    "before_title", "after_title", "split_position", "insertion_position",
)

ERROR_TYPES = (
    "document_boundary_error", "layout_order_error", "block_split_error", "block_merge_error",
    "table_structure_error", "ocr_typo", "linebreak_error", "duplicated_text", "missing_text",
    "noise_text", "no_error", "other",
)


def make_log(file_id: str, document_id: str, user_id: str, operation: str, **values: Any) -> dict:
    base = {field: None for field in LOG_FIELDS}
    base.update(
        {
            "file_id": file_id,
            "document_id": document_id,
            "correction_id": make_correction_id(),
            "timestamp": utc_now(),
            "user_id": user_id or "anonymous",
            "operation": operation,
            "error_type": values.pop("error_type", "other") or "other",
        }
    )
    base.update({key: value for key, value in values.items() if key in base})
    return base


def append_log(path: Path, record: dict) -> None:
    """Append one complete JSON object and flush it immediately."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()


def read_logs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL {number}행이 손상되었습니다: {exc}") from exc
    return records
