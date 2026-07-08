from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from utils.diff_utils import unified_diff, word_diff
from utils.export_manager import export_all
from utils.log_manager import ERROR_TYPES, read_logs
from utils.state_manager import list_states, load_state, save_state
from utils.text_loader import TextLoadError, ingest_upload
from utils.workspace import (
    active_blocks,
    build_initial_state,
    delete_block,
    document_text,
    edit_block,
    edit_document,
    insert_block,
    keep_block,
    merge_blocks,
    merge_documents,
    move_block,
    reorder_document,
    split_block,
    split_document,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
for folder in (
    "input_txt", "before", "after/by_file", "after/by_document", "logs_jsonl",
    "logs_excel", "working_state", "reports",
):
    (DATA_DIR / folder).mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="OCR Trainer", page_icon="🧾", layout="wide")


def refresh_state(file_id: str) -> None:
    st.session_state.ocr_state = load_state(DATA_DIR, file_id)


def perform(action, success: str, *args, **kwargs) -> None:
    try:
        action(DATA_DIR, st.session_state.ocr_state, *args, **kwargs)
        st.toast(success, icon="✅")
        refresh_state(st.session_state.ocr_state["file_id"])
        st.rerun()
    except Exception as exc:  # Streamlit must turn recoverable validation/storage failures into UI feedback.
        st.error(str(exc))


def state_choices() -> list[dict]:
    return [item for item in list_states(DATA_DIR) if item.get("file_id")]


def current_logs(state: dict) -> list[dict]:
    path = DATA_DIR / "logs_jsonl" / f"{state['file_id']}_corrections.jsonl"
    try:
        return read_logs(path)
    except ValueError as exc:
        st.error(str(exc))
        return []


st.title("범용 OCR TXT 검수 및 학습데이터 생성")
st.caption("자동 분석은 후보를 제안하고, 최종 문서 경계와 수정 내용은 사용자가 결정합니다.")

top_upload, top_select, top_user, top_finish = st.columns([2.2, 2.2, 1.2, 1.2])
with top_upload:
    upload = st.file_uploader("TXT 업로드", type=["txt"], accept_multiple_files=False)
    if upload is not None and st.button("업로드 분석", type="primary", use_container_width=True):
        try:
            result = ingest_upload(upload.name, upload.getvalue(), DATA_DIR)
            existing = DATA_DIR / "working_state" / f"{result['file_id']}_state.json"
            if existing.exists():
                st.session_state.ocr_state = load_state(DATA_DIR, result["file_id"])
                st.info("동일한 원본의 기존 작업 상태를 복구했습니다.")
            else:
                state = build_initial_state(result)
                save_state(DATA_DIR, state)
                st.session_state.ocr_state = state
            st.rerun()
        except (TextLoadError, ValueError, OSError) as exc:
            st.error(str(exc))

choices = state_choices()
with top_select:
    labels = {
        item["file_id"]: f"{item['filename'] or item['file_id']} {'✓' if item['completed'] else '•'}"
        for item in choices
    }
    selected_file = st.selectbox("작업 파일 선택", list(labels), format_func=lambda key: labels[key], index=None, placeholder="저장된 작업 선택")
    if selected_file and st.button("작업 불러오기", use_container_width=True):
        try:
            refresh_state(selected_file)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

with top_user:
    user_id = st.text_input("작업자 ID", value=st.session_state.get("user_id", "anonymous"))
    st.session_state.user_id = user_id.strip() or "anonymous"

state = st.session_state.get("ocr_state")
with top_finish:
    st.write("최종 산출물")
    if st.button("검수 완료·내보내기", type="primary", disabled=state is None, use_container_width=True):
        try:
            paths = export_all(DATA_DIR, state)
            st.session_state.export_paths = paths
            refresh_state(state["file_id"])
            st.success("TXT, JSONL, Excel 생성을 완료했습니다.")
        except Exception as exc:
            st.error(f"내보내기 실패: {exc}")

if state is None:
    st.info("TXT를 업로드하거나 저장된 작업을 선택하세요.")
    st.stop()

st.write(f"**file_id:** `{state['file_id']}` · 인코딩: `{state['encoding']}` · 자동 저장: 켜짐 · 상태: {'완료' if state.get('completed') else '작업 중'}")

if st.session_state.get("export_paths"):
    paths = st.session_state.export_paths
    downloads = st.columns(2)
    for index, (label, path_string) in enumerate(paths.items()):
        path = Path(path_string)
        if path.exists():
            downloads[index % 2].download_button(
                f"{label} 다운로드", data=path.read_bytes(), file_name=path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if path.suffix == ".xlsx" else "text/plain",
                key=f"download_{label}_{state['file_id']}",
            )

left, center, right = st.columns([1.05, 1.8, 1.05], gap="large")

with left:
    st.subheader("BEFORE 원본")
    with st.expander("원본 TXT", expanded=True):
        numbered = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(state["source_text"].splitlines(), 1))
        st.code(numbered, language=None, line_numbers=False)
    st.subheader("문서 경계 후보")
    candidates = state.get("boundary_candidates", [])
    if candidates:
        candidate_df = pd.DataFrame(candidates)
        st.dataframe(
            candidate_df[["line_number", "confidence", "score", "preview", "reasons"]],
            use_container_width=True, hide_index=True, height=320,
        )
    else:
        st.caption("탐지된 후보가 없습니다. 문서 블록 위치에서 직접 분리할 수 있습니다.")

documents = state["documents"]
document_ids = [document["document_id"] for document in documents]
with center:
    st.subheader("문서 및 블록 검수")
    selected_document_id = st.selectbox(
        "문서", document_ids,
        format_func=lambda doc_id: next(f"{d['document_id']} · {d['title']} ({len(active_blocks(d))}블록)" for d in documents if d["document_id"] == doc_id),
    )
    document = next(d for d in documents if d["document_id"] == selected_document_id)

    doc_tab, block_tab, structure_tab = st.tabs(["문서 정보", "블록 편집", "구조 변경"])
    with doc_tab:
        with st.form(f"doc_meta_{selected_document_id}"):
            title = st.text_input("문서 제목", value=document["title"])
            doc_type = st.text_input("문서 유형", value=document["doc_type"], help="고정 목록이 아닙니다. 자유롭게 입력하세요.")
            memo = st.text_input("메모", key=f"doc_memo_{selected_document_id}")
            if st.form_submit_button("제목/유형 저장"):
                perform(edit_document, "문서 정보를 저장했습니다.", selected_document_id, title, doc_type, st.session_state.user_id, memo)
        st.text_area("현재 문서 미리보기", document_text(document), height=350, disabled=True)

    blocks = active_blocks(document)
    with block_tab:
        if not blocks:
            st.warning("이 문서에는 활성 블록이 없습니다. 구조 변경 탭에서 블록을 삽입하세요.")
        else:
            block_ids = [block["block_id"] for block in blocks]
            selected_block_id = st.selectbox(
                "블록 선택", block_ids,
                format_func=lambda block_id: next(
                    f"{b['current_order'] + 1}. [{b['block_type']}] {b['text'][:70].replace(chr(10), ' ')}" for b in blocks if b["block_id"] == block_id
                ),
            )
            block = next(b for b in blocks if b["block_id"] == selected_block_id)
            with st.form(f"edit_{selected_block_id}"):
                edited_text = st.text_area("텍스트", value=block["text"], height=220)
                error_choice = st.selectbox("오류 유형", ERROR_TYPES, index=ERROR_TYPES.index("ocr_typo"))
                custom_error = st.text_input("사용자 정의 오류 유형", disabled=error_choice != "other", placeholder="other 선택 시 입력")
                error_type = custom_error.strip() if error_choice == "other" and custom_error.strip() else error_choice
                edit_memo = st.text_input("메모")
                edit_col, keep_col = st.columns(2)
                if edit_col.form_submit_button("EDIT 저장", use_container_width=True):
                    perform(edit_block, "블록을 수정했습니다.", selected_block_id, edited_text, st.session_state.user_id, error_type, edit_memo)
                if keep_col.form_submit_button("KEEP 기록", use_container_width=True):
                    perform(keep_block, "정상 OCR로 기록했습니다.", selected_block_id, st.session_state.user_id, edit_memo)
            st.caption("단어 단위 변경 미리보기")
            st.code(word_diff(block["original_text"], block["text"]) or "변경 없음", language=None)

    with structure_tab:
        st.markdown("##### 문서 경계")
        doc_position = document_ids.index(selected_document_id)
        order_a, order_b = st.columns(2)
        if order_a.button("문서 위로", disabled=doc_position == 0, use_container_width=True):
            perform(reorder_document, "문서 순서를 변경했습니다.", selected_document_id, doc_position - 1, st.session_state.user_id)
        if order_b.button("문서 아래로", disabled=doc_position == len(document_ids) - 1, use_container_width=True):
            perform(reorder_document, "문서 순서를 변경했습니다.", selected_document_id, doc_position + 1, st.session_state.user_id)
        split_position = st.number_input(
            "이 블록 번호부터 새 문서로 분리", min_value=1,
            max_value=max(1, len(blocks) - 1), value=1, disabled=len(blocks) < 2,
        )
        split_reason = st.text_input("분리 사유", key=f"doc_split_reason_{selected_document_id}")
        if st.button("DOC_SPLIT", disabled=len(blocks) < 2):
            perform(split_document, "문서를 분리했습니다.", selected_document_id, split_position, st.session_state.user_id, split_reason)
        merge_doc_ids = st.multiselect("병합할 문서 (표시 순서대로 병합)", document_ids)
        merge_doc_reason = st.text_input("병합 사유", key="doc_merge_reason")
        if st.button("DOC_MERGE"):
            perform(merge_documents, "문서를 병합했습니다.", merge_doc_ids, st.session_state.user_id, merge_doc_reason)

        st.divider()
        st.markdown("##### 블록 구조")
        if blocks:
            merge_ids = st.multiselect("병합할 블록", [b["block_id"] for b in blocks], format_func=lambda value: f"{value} · {next(b['text'][:40] for b in blocks if b['block_id'] == value)}")
            separator_option = st.selectbox("병합 구분자", ["줄바꿈", "빈 줄", "공백"])
            separators = {"줄바꿈": "\n", "빈 줄": "\n\n", "공백": " "}
            if st.button("MERGE"):
                perform(merge_blocks, "블록을 병합했습니다.", selected_document_id, merge_ids, st.session_state.user_id, separators[separator_option])

            split_target = st.selectbox("분리할 블록", [b["block_id"] for b in blocks], key="split_target")
            split_source = next(b["text"] for b in blocks if b["block_id"] == split_target)
            split_value = st.text_area("조각 사이에 ---SPLIT--- 입력", value=split_source, height=160)
            if st.button("SPLIT"):
                perform(split_block, "블록을 분리했습니다.", split_target, split_value.split("---SPLIT---"), st.session_state.user_id)

            move_target = st.selectbox("이동할 블록", [b["block_id"] for b in blocks], key="move_target")
            target_doc = st.selectbox("대상 문서", document_ids, key="move_doc")
            target_document = next(d for d in documents if d["document_id"] == target_doc)
            target_position = st.number_input("대상 위치 (0=맨 앞)", 0, len(active_blocks(target_document)), 0)
            if st.button("MOVE"):
                perform(move_block, "블록을 이동했습니다.", move_target, target_doc, target_position, st.session_state.user_id)

            delete_target = st.selectbox("삭제할 블록", [b["block_id"] for b in blocks], key="delete_target")
            delete_reason = st.text_input("삭제 사유", key="delete_reason")
            if st.button("DELETE"):
                perform(delete_block, "블록을 삭제했습니다.", delete_target, st.session_state.user_id, delete_reason)

        insert_position = st.number_input("삽입 위치 (0=맨 앞)", 0, len(blocks), len(blocks), key="insert_position")
        insert_text_value = st.text_area("삽입 텍스트", key="insert_text")
        if st.button("INSERT"):
            perform(insert_block, "블록을 삽입했습니다.", selected_document_id, insert_position, insert_text_value, st.session_state.user_id)

with right:
    logs = current_logs(state)
    st.subheader("진행 현황")
    total_blocks = sum(len(active_blocks(document)) for document in documents)
    modified_blocks = sum(sum(bool(block.get("modified")) for block in document["blocks"]) for document in documents)
    st.metric("문서", len(documents))
    metric_a, metric_b = st.columns(2)
    metric_a.metric("활성 블록", total_blocks)
    metric_b.metric("수정 블록", modified_blocks)
    st.progress(min(1.0, modified_blocks / max(1, total_blocks)), text=f"블록 검수 진행률(수정/기록 기준 제외): {modified_blocks}/{total_blocks}")
    if logs:
        operation_counts = pd.Series([log.get("operation") for log in logs]).value_counts().rename_axis("operation").reset_index(name="count")
        st.dataframe(operation_counts, hide_index=True, use_container_width=True)
        st.subheader("현재 수정 로그")
        log_df = pd.DataFrame(logs)
        st.dataframe(log_df[["correction_id", "timestamp", "document_id", "operation", "error_type"]].iloc[::-1], hide_index=True, use_container_width=True, height=260)
        latest = logs[-1]
        st.caption("최근 작업 diff")
        st.code(latest.get("diff") or unified_diff(str(latest.get("before_text") or ""), str(latest.get("after_text") or "")) or "변경 없음", language="diff")
    else:
        st.caption("아직 수정 로그가 없습니다.")
