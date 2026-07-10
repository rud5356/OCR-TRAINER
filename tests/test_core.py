from pathlib import Path

from utils.export_manager import export_all
from utils.plain_diff_logger import make_plain_revision_logs, save_plain_after_and_logs
from utils.state_manager import load_state, save_state
from utils.text_loader import decode_text, ingest_upload
from utils.workspace import build_initial_state
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
    assert state["plain_mode"] is True
    assert len(state["documents"]) == 1
    assert len(active_blocks(state["documents"][0])) == 1
    assert active_blocks(state["documents"][0])[0]["text"] == upload["text"]
    save_state(tmp_path, state)

    after = "INVOICE 수정\nNo. 100\n\n금액: 1,000원\n본문"
    save_plain_after_and_logs(tmp_path, state, after, "tester")
    restored = load_state(tmp_path, state["file_id"])
    assert restored["documents"][0]["blocks"][0]["text"] == after

    outputs = export_all(tmp_path, restored)
    assert all(Path(path).exists() for path in outputs.values())
    after_text = Path(outputs["after_file"]).read_text(encoding="utf-8")
    assert "INVOICE 수정" in after_text
    assert "===== DOCUMENT" not in after_text


def test_plain_revision_logs_for_simple_text_changes(tmp_path: Path):
    before = "홍길동은 접수하였다.\n금액 1000원\nABCDE"
    after = "홍길동은 접수하였디.\n금액\n1000원\nABCED\n새 문장"
    upload = ingest_upload("plain.txt", before.encode("utf-8"), tmp_path)
    state = build_initial_state(upload)
    logs = save_plain_after_and_logs(tmp_path, state, after, "tester")
    operations = {log["operation"] for log in logs}

    assert "문장 내용 수정" in operations
    assert "줄 분리" in operations
    assert "순서 이동" in operations
    assert "추가 의심" in operations
    assert "INSERT" not in operations
    assert load_state(tmp_path, state["file_id"])["documents"][0]["blocks"][0]["text"] == after


def test_plain_preview_logs_empty_when_before_equals_after():
    logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text="같은 텍스트\n그대로",
        after_text="같은 텍스트\n그대로",
    )
    assert logs == []


def test_line_merge_two_lines_with_item_number():
    before = "①상 호(명 칭)\n아쿠아스쿨"
    after = "①상 호(명 칭) 아쿠아스쿨"
    logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=before,
        after_text=after,
    )
    assert len(logs) == 1
    assert logs[0]["operation"] == "줄 병합"
    assert logs[0]["before_position"] == "1~2"
    assert logs[0]["after_position"] == 1
    assert logs[0]["before_text"] == "①상 호(명 칭) / 아쿠아스쿨"
    assert logs[0]["after_text"] == "①상 호(명 칭) 아쿠아스쿨"


def test_line_merge_three_address_lines():
    before = "④주 소\n(사업장소재지)\n(10080) 경기도 김포시 김포한강1로 77-33 (장기동) 1층"
    after = "④주 소 (사업장소재지) (10080) 경기도 김포시 김포한강1로 77-33 (장기동) 1층"
    logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=before,
        after_text=after,
    )
    assert len(logs) == 1
    assert logs[0]["operation"] == "줄 병합"
    assert logs[0]["before_position"] == "1~3"


def test_structural_number_mismatch_is_not_matched_as_edit():
    before = "④주 소\n(사업장소재지)"
    after = "①상 호(명칭) 아쿠아스쿨\n②성명(대표자) 반석현"
    logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=before,
        after_text=after,
    )
    assert not any(
        log["operation"] == "문장 내용 수정"
        and "④주 소" in str(log.get("before_text"))
        and "①상 호" in str(log.get("after_text"))
        for log in logs
    )
    assert {"삭제 의심", "추가 의심"} <= {log["operation"] for log in logs}


def test_spacing_missing_and_phone_ocr_fixes():
    before = "신 청 인\n실 자\n01C - 8994 - 6296"
    after = "신청인\n실수요자\n010 - 8994 - 6296"
    logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=before,
        after_text=after,
    )
    operations = [log["operation"] for log in logs]
    assert "공백 정리" in operations
    assert "OCR 누락 보정" in operations
    assert "OCR 오인식 수정" in operations


def test_moved_same_or_spacing_lines_are_not_delete_add():
    before = "\n".join([f"BEFORE {index:02d}" for index in range(1, 20)] + ["신 청 인"])
    after = "\n".join([f"AFTER {index:02d}" for index in range(1, 10)] + ["신청인"])
    logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=before,
        after_text=after,
    )
    target = next(log for log in logs if log["after_text"] == "신청인")
    assert target["operation"] == "순서 이동+공백 정리"
    assert target["before_position"] == 20
    assert target["after_position"] == 10
    assert target["before_text"] == "신 청 인"
    assert not any(log["operation"] == "삭제 의심" and log["before_text"] == "신 청 인" for log in logs)
    assert not any(log["operation"] == "추가 의심" and log["after_text"] == "신청인" for log in logs)


def test_moved_exact_and_moved_edited_lines():
    before = "A\n같은 문장\nB\n홍길동은 접수하였다."
    after = "홍길동은 접수하였디.\nB\n같은 문장\nA"
    logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=before,
        after_text=after,
    )
    exact = next(log for log in logs if log["before_text"] == "같은 문장")
    edited = next(log for log in logs if log["before_text"] == "홍길동은 접수하였다.")
    assert exact["operation"] == "순서 이동"
    assert exact["before_position"] == 2
    assert exact["after_position"] == 3
    assert edited["operation"] == "순서 이동+문장 수정"
    assert edited["before_position"] == 4
    assert edited["after_position"] == 1


def test_moved_line_merge_and_split():
    merge_before = "머리말\n①상 호(명 칭)\n아쿠아스쿨"
    merge_after = "①상 호(명 칭) 아쿠아스쿨\n머리말"
    merge_logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=merge_before,
        after_text=merge_after,
    )
    merged = next(log for log in merge_logs if "아쿠아스쿨" in str(log["after_text"]))
    assert merged["operation"] == "순서 이동+줄 병합"
    assert merged["before_position"] == "2~3"
    assert merged["after_position"] == 1

    split_before = "머리말\n금액 1000원"
    split_after = "금액\n1000원\n머리말"
    split_logs = make_plain_revision_logs(
        file_id="sample",
        document_id="D001",
        block_id="B001",
        user_id="tester",
        before_text=split_before,
        after_text=split_after,
    )
    split = next(log for log in split_logs if log["before_text"] == "금액 1000원")
    assert split["operation"] == "순서 이동+줄 분리"
    assert split["before_position"] == 2
    assert split["after_position"] == "1~2"


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
    keep_block(tmp_path, state, active_blocks(state["documents"][0])[0]["block_id"], "tester")
    split_document(tmp_path, state, document_id, 1, "tester", "경계 확인")
    second_id = state["documents"][1]["document_id"]
    reorder_document(tmp_path, state, second_id, 0, "tester")
    moving = active_blocks(state["documents"][0])[0]["block_id"]
    move_block(tmp_path, state, moving, document_id, 0, "tester")
    merge_documents(tmp_path, state, [d["document_id"] for d in state["documents"]], "tester")
    delete_block(tmp_path, state, active_blocks(state["documents"][0])[-1]["block_id"], "tester", "중복")
    assert len(state["documents"]) == 1
    assert len(state["history"]) == 9
