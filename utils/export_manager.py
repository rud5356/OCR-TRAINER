from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd

from .diff_utils import diff_summary
from .log_manager import read_logs
from .state_manager import save_state
from .text_loader import atomic_write_text
from .workspace import active_blocks, document_text


def _jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records)
    atomic_write_text(path, text)


def export_all(data_dir: Path, state: dict) -> dict[str, str]:
    file_id = state["file_id"]
    logs_path = data_dir / "logs_jsonl" / f"{file_id}_corrections.jsonl"
    if not logs_path.exists():
        atomic_write_text(logs_path, "")
    logs = read_logs(logs_path)
    counts = Counter(log.get("operation") for log in logs)

    rendered_documents = []
    document_pairs = []
    document_rows = []
    block_rows = []
    for index, document in enumerate(state["documents"], start=1):
        after = document_text(document)
        header = f"===== DOCUMENT {index:03d} | {document['title']} ====="
        rendered_documents.append(f"{header}\n\n{after}")
        document_path = data_dir / "after" / "by_document" / file_id / f"{document['document_id']}_after.txt"
        atomic_write_text(document_path, after)
        doc_logs = [log for log in logs if log.get("document_id") == document["document_id"]]
        before = state.get("original_documents", {}).get(document["document_id"])
        if before is None:
            before = "\n\n".join(block.get("original_text", "") for block in document["blocks"] if block.get("original_text"))
        document_pairs.append({
            "file_id": file_id, "document_id": document["document_id"], "doc_type": document["doc_type"],
            "title": document["title"], "before_document": before, "after_document": after, "change_count": len(doc_logs),
        })
        document_rows.append({
            "document_id": document["document_id"], "문서 제목": document["title"], "문서 유형": document["doc_type"],
            "시작 위치": document.get("start_line"), "종료 위치": document.get("end_line"),
            "블록 수": len(active_blocks(document)), "수정 건수": len(doc_logs), "최종 저장 파일 경로": str(document_path),
        })
        for block in document["blocks"]:
            block_rows.append({
                "file_id": file_id, "document_id": document["document_id"], "block_id": block["block_id"],
                "original_order": block["original_order"], "current_order": block["current_order"],
                "block_type": block["block_type"], "수정 여부": block.get("modified", False),
                "삭제 여부": block.get("deleted", False), "최종 텍스트 일부": block.get("text", "")[:500],
            })

    after_file = "\n\n".join(rendered_documents).rstrip() + "\n"
    file_path = data_dir / "after" / "by_file" / f"{file_id}_after.txt"
    atomic_write_text(file_path, after_file)
    document_pairs_path = data_dir / "logs_jsonl" / f"{file_id}_document_pairs.jsonl"
    _jsonl(document_pairs_path, document_pairs)
    file_pair_path = data_dir / "logs_jsonl" / f"{file_id}_file_pair.jsonl"
    _jsonl(file_pair_path, [{
        "file_id": file_id, "before_file": state["source_text"], "after_file": after_file,
        "document_count": len(state["documents"]), "change_count": len(logs),
    }])

    summary = [{
        "file_id": file_id, "원본 전체 줄 수": len(state["source_text"].splitlines()),
        "분리된 문서 수": len(state["documents"]),
        "전체 블록 수": sum(len(active_blocks(d)) for d in state["documents"]),
        "수정된 블록 수": sum(sum(bool(b.get("modified")) for b in d["blocks"]) for d in state["documents"]),
        "총 수정 건수": len(logs), "문서 경계 수정 건수": counts["DOC_SPLIT"] + counts["DOC_MERGE"],
        "문단 이동 건수": counts["MOVE"], "문단 병합 건수": counts["MERGE"],
        "문단 분리 건수": counts["SPLIT"], "텍스트 수정 건수": counts["EDIT"],
        "삭제 건수": counts["DELETE"], "삽입 건수": counts["INSERT"], "수정 없음 건수": counts["KEEP"],
    }]
    change_rows = []
    for log in logs:
        change_rows.append({
            "수정번호": log.get("correction_id"), "file_id": file_id, "document_id": log.get("document_id"),
            "수정유형": log.get("operation"), "오류유형": log.get("error_type"),
            "수정 전 문서": log.get("before_document_id"), "수정 후 문서": log.get("after_document_id"),
            "수정 전 위치": log.get("before_position"), "수정 후 위치": log.get("after_position"),
            "수정 전 내용": str(log.get("before_text") or "")[:32000],
            "수정 후 내용": str(log.get("after_text") or "")[:32000],
            "diff 요약": diff_summary(str(log.get("before_text") or ""), str(log.get("after_text") or "")),
            "수정사유": log.get("reason"), "신뢰도": log.get("confidence"),
            "수정시간": log.get("timestamp"), "메모": log.get("memo"),
        })
    excel_path = data_dir / "logs_excel" / f"{file_id}_change_log.xlsx"
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        pd.DataFrame(summary).to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(document_rows).to_excel(writer, sheet_name="Documents", index=False)
        pd.DataFrame(change_rows).to_excel(writer, sheet_name="Change_Log", index=False)
        pd.DataFrame(block_rows).to_excel(writer, sheet_name="Block_Status", index=False)
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                max_length = max(len(str(cell.value or "")) for cell in column)
                sheet.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 10), 50)

    state["completed"] = True
    state["completed_at"] = pd.Timestamp.utcnow().isoformat()
    save_state(data_dir, state)
    return {
        "after_file": str(file_path), "document_pairs": str(document_pairs_path),
        "file_pair": str(file_pair_path), "excel": str(excel_path), "corrections": str(logs_path),
    }
