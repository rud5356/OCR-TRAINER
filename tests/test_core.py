from pathlib import Path

from utils.export_manager import export_all
from utils.state_manager import load_state, save_state
from utils.text_loader import decode_text, ingest_upload
from utils.workspace import build_initial_state, edit_block
from utils.workspace import (
    active_blocks,
    delete_block,
    insert_block,
    keep_block,
    merge_blocks,
    merge_documents,
    move_block,
    reorder_document,
    split_block,
    split_document,
)


def test_decode_cp949():
    text, encoding = decode_text("접수증\n본문".encode("cp949"))
    assert text == "접수증\n본문"
    assert encoding in {"cp949", "euc-kr"}


def test_state_edit_and_export(tmp_path: Path):
    raw = "INVOICE\nNo. 100\n\n금액: 1,000원\n본문".encode("utf-8")
    upload = ingest_upload("sample.txt", raw, tmp_path)
    state = build_initial_state(upload)
    save_state(tmp_path, state)
    block = state["documents"][0]["blocks"][0]
    edit_block(tmp_path, state, block["block_id"], "INVOICE 수정", "tester", "ocr_typo")
    restored = load_state(tmp_path, state["file_id"])
    assert restored["documents"][0]["blocks"][0]["text"] == "INVOICE 수정"
    outputs = export_all(tmp_path, restored)
    assert all(Path(path).exists() for path in outputs.values())
    assert "INVOICE 수정" in Path(outputs["after_file"]).read_text(encoding="utf-8")


def test_all_structure_operations(tmp_path: Path):
    raw = "제목\n첫 문장입니다.\n\n둘째 문장입니다.\n\n셋째 문장입니다.".encode("utf-8")
    state = build_initial_state(ingest_upload("operations.txt", raw, tmp_path))
    save_state(tmp_path, state)
    document_id = state["documents"][0]["document_id"]
    insert_block(tmp_path, state, document_id, 1, "삽입 문장", "tester")
    blocks = active_blocks(state["documents"][0])
    split_block(tmp_path, state, blocks[0]["block_id"], ["제목", "첫 문장입니다."], "tester")
    blocks = active_blocks(state["documents"][0])
    merge_blocks(tmp_path, state, document_id, [blocks[0]["block_id"], blocks[1]["block_id"]], "tester")
    blocks = active_blocks(state["documents"][0])
    delete_block(tmp_path, state, blocks[-1]["block_id"], "tester", "중복")
    keep_block(tmp_path, state, active_blocks(state["documents"][0])[0]["block_id"], "tester")
    split_document(tmp_path, state, document_id, 1, "tester", "경계 확인")
    second_id = state["documents"][1]["document_id"]
    reorder_document(tmp_path, state, second_id, 0, "tester")
    moving = active_blocks(state["documents"][0])[0]["block_id"]
    move_block(tmp_path, state, moving, document_id, 0, "tester")
    merge_documents(tmp_path, state, [d["document_id"] for d in state["documents"]], "tester")
    assert len(state["documents"]) == 1
    assert len(state["history"]) == 9
