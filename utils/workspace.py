from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .block_parser import classify_block, parse_blocks
from .diff_utils import unified_diff
from .document_segmenter import detect_boundary_candidates, split_text_at_boundaries, suggested_boundaries
from .id_utils import make_block_id, make_document_id, utc_now
from .log_manager import append_log, make_log
from .state_manager import save_state


def active_blocks(document: dict) -> list[dict]:
    return sorted((b for b in document["blocks"] if not b.get("deleted")), key=lambda b: b["current_order"])


def document_text(document: dict) -> str:
    return "\n\n".join(block["text"] for block in active_blocks(document)).strip()


def find_document(state: dict, document_id: str) -> dict:
    for document in state["documents"]:
        if document["document_id"] == document_id:
            return document
    raise ValueError(f"문서를 찾을 수 없습니다: {document_id}")


def find_block(state: dict, block_id: str) -> tuple[dict, dict]:
    for document in state["documents"]:
        for block in document["blocks"]:
            if block["block_id"] == block_id:
                return document, block
    raise ValueError(f"블록을 찾을 수 없습니다: {block_id}")


def _reindex(document: dict) -> None:
    for index, block in enumerate(active_blocks(document)):
        block["current_order"] = index


def _commit(data_dir: Path, state: dict, record: dict) -> dict:
    state["completed"] = False
    state.pop("completed_at", None)
    state.setdefault("history", []).append(record["correction_id"])
    save_state(data_dir, state)
    append_log(data_dir / "logs_jsonl" / f"{state['file_id']}_corrections.jsonl", record)
    return record


def build_initial_state(upload: dict) -> dict:
    text = upload["text"]
    candidates = detect_boundary_candidates(text)
    chunks = split_text_at_boundaries(text, suggested_boundaries(text, candidates))
    if not chunks:
        raise ValueError("TXT에서 문서를 생성할 수 없습니다.")
    documents, originals = [], {}
    for index, chunk in enumerate(chunks, start=1):
        document_id = make_document_id(index)
        title = next((line.strip() for line in chunk["text"].splitlines() if line.strip()), f"문서 {index}")[:100]
        document = {
            "document_id": document_id,
            "title": title,
            "doc_type": "unknown",
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "blocks": parse_blocks(chunk["text"], document_id),
        }
        documents.append(document)
        originals[document_id] = chunk["text"]
    now = utc_now()
    return {
        "schema_version": 1,
        "file_id": upload["file_id"],
        "filename": upload["filename"],
        "encoding": upload["encoding"],
        "before_path": upload["before_path"],
        "source_text": text,
        "boundary_candidates": candidates,
        "documents": documents,
        "original_documents": originals,
        "history": [],
        "completed": False,
        "created_at": now,
        "updated_at": now,
    }


def edit_document(data_dir: Path, state: dict, document_id: str, title: str, doc_type: str, user_id: str, memo: str = "") -> dict:
    document = find_document(state, document_id)
    before_title, before_type = document["title"], document["doc_type"]
    document["title"] = title.strip() or before_title
    document["doc_type"] = doc_type.strip() or "unknown"
    record = make_log(
        state["file_id"], document_id, user_id, "DOC_RETYPE", error_type="other",
        before_text=f"{before_type} | {before_title}", after_text=f"{document['doc_type']} | {document['title']}", memo=memo,
        before_doc_type=before_type, after_doc_type=document["doc_type"], before_title=before_title, after_title=document["title"],
    )
    return _commit(data_dir, state, record)


def edit_block(data_dir: Path, state: dict, block_id: str, new_text: str, user_id: str, error_type: str, memo: str = "") -> dict:
    document, block = find_block(state, block_id)
    before = block["text"]
    if before == new_text:
        raise ValueError("변경된 내용이 없습니다.")
    block["text"] = new_text
    block["modified"] = True
    block["block_type"] = classify_block(new_text)
    record = make_log(
        state["file_id"], document["document_id"], user_id, "EDIT", error_type=error_type,
        block_id=block_id, before_text=before, after_text=new_text, diff=unified_diff(before, new_text), memo=memo,
    )
    return _commit(data_dir, state, record)


def move_block(data_dir: Path, state: dict, block_id: str, target_document_id: str, target_position: int, user_id: str, memo: str = "") -> dict:
    source_document, block = find_block(state, block_id)
    target_document = find_document(state, target_document_id)
    if block.get("deleted"):
        raise ValueError("삭제된 블록은 이동할 수 없습니다.")
    before_position = block["current_order"]
    source_id = source_document["document_id"]
    source_document["blocks"].remove(block)
    target_blocks = active_blocks(target_document)
    target_position = max(0, min(int(target_position), len(target_blocks)))
    target_document["blocks"].append(block)
    block["current_order"] = target_position - 0.5
    _reindex(source_document)
    _reindex(target_document)
    record = make_log(
        state["file_id"], target_document_id, user_id, "MOVE", error_type="layout_order_error",
        block_id=block_id, before_document_id=source_id, after_document_id=target_document_id,
        before_position=before_position, after_position=block["current_order"], before_text=block["text"], after_text=block["text"], memo=memo,
    )
    return _commit(data_dir, state, record)


def merge_blocks(data_dir: Path, state: dict, document_id: str, block_ids: list[str], user_id: str, separator: str = "\n", memo: str = "") -> dict:
    document = find_document(state, document_id)
    chosen = [block for block in active_blocks(document) if block["block_id"] in block_ids]
    if len(chosen) < 2:
        raise ValueError("MERGE 대상은 2개 이상이어야 합니다.")
    positions = [block["current_order"] for block in chosen]
    merged_text = separator.join(block["text"] for block in chosen)
    for block in chosen:
        block["deleted"] = True
        block["modified"] = True
    merged = {
        "block_id": make_block_id(document_id), "original_text": "", "text": merged_text,
        "original_order": min(block["original_order"] for block in chosen), "current_order": min(positions) - 0.5,
        "block_type": classify_block(merged_text), "modified": True, "deleted": False,
    }
    document["blocks"].append(merged)
    _reindex(document)
    record = make_log(
        state["file_id"], document_id, user_id, "MERGE", error_type="block_split_error",
        source_block_ids=[block["block_id"] for block in chosen], new_block_ids=[merged["block_id"]],
        before_text=[block["text"] for block in chosen], after_text=merged_text, block_id=merged["block_id"], memo=memo,
    )
    return _commit(data_dir, state, record)


def split_block(data_dir: Path, state: dict, block_id: str, parts: list[str], user_id: str, memo: str = "") -> dict:
    document, source = find_block(state, block_id)
    parts = [part.strip() for part in parts if part.strip()]
    if len(parts) < 2:
        raise ValueError("SPLIT 결과는 비어 있지 않은 2개 이상의 조각이어야 합니다.")
    source["deleted"] = True
    source["modified"] = True
    new_blocks = []
    for offset, text in enumerate(parts):
        new_blocks.append({
            "block_id": make_block_id(document["document_id"]), "original_text": "", "text": text,
            "original_order": source["original_order"], "current_order": source["current_order"] + (offset + 1) / 100,
            "block_type": classify_block(text), "modified": True, "deleted": False,
        })
    document["blocks"].extend(new_blocks)
    _reindex(document)
    record = make_log(
        state["file_id"], document["document_id"], user_id, "SPLIT", error_type="block_merge_error",
        block_id=block_id, source_block_ids=[block_id], new_block_ids=[b["block_id"] for b in new_blocks],
        before_text=source["text"], after_text=[b["text"] for b in new_blocks], memo=memo,
    )
    return _commit(data_dir, state, record)


def delete_block(data_dir: Path, state: dict, block_id: str, user_id: str, reason: str, memo: str = "") -> dict:
    document, block = find_block(state, block_id)
    if block.get("deleted"):
        raise ValueError("이미 삭제된 블록입니다.")
    block["deleted"] = True
    block["modified"] = True
    _reindex(document)
    record = make_log(
        state["file_id"], document["document_id"], user_id, "DELETE", error_type="noise_text",
        block_id=block_id, before_text=block["text"], after_text="", reason=reason, memo=memo,
    )
    return _commit(data_dir, state, record)


def insert_block(data_dir: Path, state: dict, document_id: str, position: int, text: str, user_id: str, memo: str = "") -> dict:
    if not text.strip():
        raise ValueError("삽입할 텍스트가 비어 있습니다.")
    document = find_document(state, document_id)
    blocks = active_blocks(document)
    position = max(0, min(int(position), len(blocks)))
    block = {
        "block_id": make_block_id(document_id), "original_text": "", "text": text.strip(),
        "original_order": -1, "current_order": position - 0.5, "block_type": classify_block(text),
        "modified": True, "deleted": False,
    }
    document["blocks"].append(block)
    _reindex(document)
    record = make_log(
        state["file_id"], document_id, user_id, "INSERT", error_type="missing_text", block_id=block["block_id"],
        before_position=None, after_position=block["current_order"], insertion_position=block["current_order"],
        before_text="", after_text=block["text"], memo=memo,
    )
    return _commit(data_dir, state, record)


def keep_block(data_dir: Path, state: dict, block_id: str, user_id: str, memo: str = "") -> dict:
    document, block = find_block(state, block_id)
    record = make_log(
        state["file_id"], document["document_id"], user_id, "KEEP", error_type="no_error",
        block_id=block_id, before_text=block["text"], after_text=block["text"], memo=memo,
    )
    return _commit(data_dir, state, record)


def split_document(data_dir: Path, state: dict, document_id: str, split_position: int, user_id: str, reason: str = "") -> dict:
    document = find_document(state, document_id)
    blocks = active_blocks(document)
    split_position = int(split_position)
    if split_position <= 0 or split_position >= len(blocks):
        raise ValueError("문서 분리 위치는 첫 블록과 마지막 블록 사이여야 합니다.")
    doc_index = state["documents"].index(document)
    new_id = f"D{max(int(d['document_id'][1:]) for d in state['documents'] if d['document_id'][1:].isdigit()) + 1:03d}"
    moving = blocks[split_position:]
    for block in moving:
        document["blocks"].remove(block)
    new_document = deepcopy(document)
    new_document.update({"document_id": new_id, "title": f"{document['title']} (분리)", "blocks": moving})
    _reindex(document)
    _reindex(new_document)
    state["documents"].insert(doc_index + 1, new_document)
    original_parts = [block.get("original_text", "") for block in blocks]
    state.setdefault("original_documents", {})[document_id] = "\n\n".join(part for part in original_parts[:split_position] if part)
    state["original_documents"][new_id] = "\n\n".join(part for part in original_parts[split_position:] if part)
    record = make_log(
        state["file_id"], document_id, user_id, "DOC_SPLIT", error_type="document_boundary_error",
        before_document_id=document_id, after_document_id=[document_id, new_id], new_document_ids=[document_id, new_id],
        before_position=split_position, after_position=split_position, split_position=split_position, reason=reason,
    )
    return _commit(data_dir, state, record)


def merge_documents(data_dir: Path, state: dict, document_ids: list[str], user_id: str, reason: str = "") -> dict:
    selected = [document for document in state["documents"] if document["document_id"] in document_ids]
    if len(selected) < 2:
        raise ValueError("DOC_MERGE 대상은 2개 이상이어야 합니다.")
    target = selected[0]
    source_ids = [document["document_id"] for document in selected]
    original_map = state.setdefault("original_documents", {})
    merged_original = "\n\n".join(original_map.get(source_id, "") for source_id in source_ids if original_map.get(source_id, ""))
    for source in selected[1:]:
        target["blocks"].extend(active_blocks(source))
        state["documents"].remove(source)
    _reindex(target)
    original_map[target["document_id"]] = merged_original
    for source_id in source_ids[1:]:
        original_map.pop(source_id, None)
    record = make_log(
        state["file_id"], target["document_id"], user_id, "DOC_MERGE", error_type="document_boundary_error",
        before_document_id=source_ids, source_document_ids=source_ids, after_document_id=target["document_id"], reason=reason,
        before_text=[document_text(d) for d in selected], after_text=document_text(target),
    )
    return _commit(data_dir, state, record)


def reorder_document(data_dir: Path, state: dict, document_id: str, new_position: int, user_id: str) -> dict:
    document = find_document(state, document_id)
    before_position = state["documents"].index(document)
    new_position = max(0, min(int(new_position), len(state["documents"]) - 1))
    if before_position == new_position:
        raise ValueError("문서 순서가 변경되지 않았습니다.")
    state["documents"].pop(before_position)
    state["documents"].insert(new_position, document)
    record = make_log(
        state["file_id"], document_id, user_id, "DOC_REORDER", error_type="layout_order_error",
        before_document_id=document_id, after_document_id=document_id,
        before_position=before_position, after_position=new_position,
        before_text=document["title"], after_text=document["title"],
    )
    return _commit(data_dir, state, record)
