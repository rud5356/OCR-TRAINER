from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import streamlit as st

from utils.diff_utils import unified_diff
from utils.export_manager import export_all
from utils.plain_diff_logger import save_plain_after_and_logs, save_plain_revision_logs
from utils.state_manager import list_states, load_state, save_state
from utils.text_loader import TextLoadError, ingest_upload
from utils.workspace import active_blocks, build_initial_state, ensure_plain_state

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
for folder in (
    "input_txt",
    "before",
    "after/by_file",
    "after/by_document",
    "logs_jsonl",
    "logs_excel",
    "working_state",
    "reports",
):
    (DATA_DIR / folder).mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="OCR Trainer", page_icon="🧾", layout="wide")


def schedule_app_shutdown(delay_seconds: float = 0.8) -> None:
    """Stop the local Streamlit server after the click response is rendered.

    Streamlit's st.stop() only stops the current script run; it does not close
    the server process. A short timer lets the user see the shutdown message,
    then os._exit(0) reliably terminates the local Streamlit process.
    """
    if st.session_state.get("_shutdown_scheduled"):
        return
    st.session_state["_shutdown_scheduled"] = True

    timer = threading.Timer(delay_seconds, lambda: os._exit(0))
    timer.daemon = True
    timer.start()


def refresh_state(file_id: str) -> None:
    """Load work and force the one-file model.

    Older saved data may have document/block splits.  For this simplified tool,
    the editable target is always the whole BEFORE TXT as one AFTER text.
    """
    state = load_state(DATA_DIR, file_id)
    converted = ensure_plain_state(DATA_DIR, state)
    st.session_state.ocr_state = state
    if converted:
        st.session_state.plain_notice = "기존 문서/블록 분할을 단일 텍스트 편집 모드로 전환했습니다."


def autosave_after_edit(force: bool = False) -> bool:
    """Save the AFTER text and regenerate correction logs whenever it changes.

    st.text_area only sends its value to the server when the user leaves the
    field or presses Ctrl+Enter (there is no live keystroke stream from the
    browser), so this fires on that commit, and again every couple of seconds
    from the heartbeat fragment below - not literally on every keystroke.
    Returns True if a save actually happened.
    """
    state = st.session_state.get("ocr_state")
    if state is None:
        return False
    editor_key = f"after_editor_{state['file_id']}"
    edited_text = st.session_state.get(editor_key)
    if edited_text is None:
        return False
    document = state["documents"][0]
    block = active_blocks(document)[0]
    if not force and edited_text == block.get("text", ""):
        return False

    try:
        logs = save_plain_after_and_logs(
            DATA_DIR,
            state,
            edited_text,
            st.session_state.get("user_id", "anonymous"),
            st.session_state.get("after_memo", ""),
        )
        refresh_state(state["file_id"])
        st.session_state["last_autosave_at"] = time.time()
        st.session_state["autosave_notice"] = f"자동 저장됨 · 수정 로그 {len(logs)}건"
        return True
    except Exception as exc:
        st.session_state["autosave_notice"] = f"자동 저장 실패: {exc}"
        return False


@st.fragment(run_every="2s")
def render_autosave_heartbeat() -> None:
    """Recheck every 2 seconds in case a change wasn't caught by on_change,
    and show a small status line so autosave is visible instead of silent."""
    if autosave_after_edit():
        st.rerun()
    last = st.session_state.get("last_autosave_at")
    if last:
        st.caption(f"자동저장 확인 · {int(time.time() - last)}초 전")
    notice = st.session_state.get("autosave_notice")
    if notice:
        st.caption(notice)


def state_choices() -> list[dict]:
    return [item for item in list_states(DATA_DIR) if item.get("file_id")]


def numbered_text(text: str, query: str = "", max_rows: int = 2000) -> str:
    """Show the original text with line numbers, without changing the text.

    Large TXT files can make Streamlit slow if every BEFORE line is rendered at
    once. The full text is still preserved in state and in the AFTER editor;
    this function only caps the read-only BEFORE preview for faster loading.
    """
    query = query.strip().lower()
    rows = []
    for index, line in enumerate(StringIO(text), start=1):
        line = line.rstrip("\r\n")
        if query and query not in line.lower():
            continue
        if len(rows) >= max_rows:
            rows.append(f"... BEFORE 미리보기는 처음 {max_rows}줄까지만 표시합니다. 검색어를 입력하면 해당 줄을 찾을 수 있습니다.")
            break
        rows.append(f"{index:>5}  {line}")
    return "\n".join(rows) if rows else "(표시할 줄이 없습니다.)"


def plain_document_and_block(state: dict) -> tuple[dict, dict]:
    """Return the single document/block that backs the whole-text editor."""
    ensure_plain_state(DATA_DIR, state)
    document = state["documents"][0]
    blocks = active_blocks(document)
    if not blocks:
        raise ValueError("편집할 텍스트를 찾을 수 없습니다.")
    return document, blocks[0]


def render_downloads(state: dict) -> None:
    paths = st.session_state.get("export_paths")
    if not paths:
        return

    st.success("산출물이 생성되었습니다.")
    downloads = st.columns(2)
    for index, (label, path_string) in enumerate(paths.items()):
        path = Path(path_string)
        if not path.exists():
            continue
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if path.suffix == ".xlsx" else "text/plain"
        downloads[index % 2].download_button(
            f"{label} 다운로드",
            data=path.read_bytes(),
            file_name=path.name,
            mime=mime,
            key=f"download_{label}_{state['file_id']}",
        )


def build_preview_logs(state: dict, document: dict, block: dict, after_text: str, max_logs: int = 500) -> list[dict]:
    """Load the last saved correction logs without recomputing diff on page load.

    OCR-aware matching can be expensive on large TXT files.  Logs are generated
    when the user presses Save; normal page reruns should only display the saved
    JSONL result so the app opens quickly.
    """
    logs_path = DATA_DIR / "logs_jsonl" / f"{state['file_id']}_corrections.jsonl"
    if not logs_path.exists():
        return []

    records: list[dict] = []
    try:
        with logs_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if len(records) >= max_logs:
                    st.caption(f"수정 로그 파일이 커서 화면에는 처음 {max_logs}건만 빠르게 불러왔습니다.")
                    break
                records.append(json.loads(line))
        return records
    except (OSError, json.JSONDecodeError) as exc:
        st.warning(f"저장된 수정 로그를 읽지 못했습니다: {exc}")
        return []


def _normalize_editor_value(value):
    """Undo pandas' round-trip quirks so edited rows compare cleanly against
    the originals and don't write NaN/stringified numbers into the log.

    BEFORE줄/AFTER줄 mixes plain ints (9) with ranges ("9~16") in the same
    column, so the data editor treats the whole column as text and returns
    every position as a string (e.g. "9") even when nothing was edited.
    """
    if isinstance(value, float):
        if pd.isna(value):
            return None
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def log_to_row(log: dict) -> dict:
    return {
        "유형": log.get("operation"),
        "오류": log.get("error_type"),
        "BEFORE줄": log.get("before_position"),
        "AFTER줄": log.get("after_position"),
        "BEFORE": log.get("before_text"),
        "AFTER": log.get("after_text"),
        "사유": log.get("reason"),
    }


def save_edited_logs(state: dict, full_logs: list[dict], edited_rows: list[dict]) -> None:
    """Persist edits made directly in the correction-log table.

    Only the columns shown in the table are overwritten; every other field
    (confidence, memo, timestamps, context, ...) is kept from the original
    record. NOTE: the next AFTER autosave still rebuilds the whole log from a
    fresh BEFORE/AFTER diff, which will overwrite these manual edits - by
    request this tradeoff is left unsolved for now.
    """
    updated = list(full_logs)
    for index, row in enumerate(edited_rows):
        record = dict(updated[index])
        record["operation"] = row.get("유형")
        record["error_type"] = row.get("오류")
        record["before_position"] = row.get("BEFORE줄")
        record["after_position"] = row.get("AFTER줄")
        record["before_text"] = row.get("BEFORE")
        record["after_text"] = row.get("AFTER")
        record["reason"] = row.get("사유")
        record["diff"] = unified_diff(str(record["before_text"] or ""), str(record["after_text"] or ""))
        updated[index] = record
    save_plain_revision_logs(DATA_DIR, state["file_id"], updated)


def handle_uploaded_txt(upload) -> None:
    """Load a TXT as soon as the user selects it.

    이전 화면은 파일 선택 후 별도 업로드 버튼을 눌러야 해서 "업로드가 안 된 것"
    처럼 보일 수 있었다. 여기서는 선택 즉시 처리하고, 같은 파일이 rerun 때마다
    반복 처리되지 않도록 파일 내용 hash를 세션에 저장한다.
    """
    raw = upload.getvalue()
    upload_signature = f"{upload.name}:{hashlib.sha1(raw).hexdigest()}"
    active_file_id = (st.session_state.get("ocr_state") or {}).get("file_id")
    already_active = (
        st.session_state.get("last_upload_signature") == upload_signature
        and st.session_state.get("last_uploaded_file_id") == active_file_id
    )

    force_reload = st.button("다시 불러오기", width="stretch")
    if already_active and not force_reload:
        st.caption(f"업로드 완료: {upload.name}")
        return

    try:
        result = ingest_upload(upload.name, raw, DATA_DIR)
        existing = DATA_DIR / "working_state" / f"{result['file_id']}_state.json"
        if existing.exists():
            refresh_state(result["file_id"])
            st.info("기존 작업을 불러왔습니다.")
        else:
            state = build_initial_state(result)
            save_state(DATA_DIR, state)
            st.session_state.ocr_state = state

        st.session_state.last_upload_signature = upload_signature
        st.session_state.last_uploaded_file_id = result["file_id"]
        st.session_state.export_paths = {}
        st.success(f"TXT 업로드 완료: {upload.name}")
        st.rerun()
    except (TextLoadError, ValueError, OSError) as exc:
        st.error(f"TXT 업로드 실패: {exc}")


st.title("OCR TXT 단순 오타 검수")
st.caption("BEFORE 원본을 그대로 AFTER에 넣어두고, 오타·줄바꿈·글자 이동 같은 단순 수정 로그만 찍습니다.")

top_upload, top_select, top_user, top_finish, top_shutdown = st.columns([2.1, 2.1, 1.1, 1.2, 1.0])
with top_upload:
    upload = st.file_uploader("TXT 파일 선택", type=["txt"], accept_multiple_files=False)
    if upload is not None:
        handle_uploaded_txt(upload)

choices = state_choices()
with top_select:
    labels = {
        item["file_id"]: f"{item['filename'] or item['file_id']} {'완료' if item['completed'] else '작업 중'}"
        for item in choices
    }
    selected_file = st.selectbox(
        "저장된 작업 선택",
        list(labels),
        format_func=lambda key: labels[key],
        index=None,
        placeholder="작업 선택",
    )
    if selected_file and st.button("작업 불러오기", width="stretch"):
        try:
            refresh_state(selected_file)
            st.session_state.export_paths = {}
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

with top_user:
    user_id = st.text_input("작업자 ID", value=st.session_state.get("user_id", "anonymous"))
    st.session_state.user_id = user_id.strip() or "anonymous"

with top_shutdown:
    st.write("앱")
    if st.button("앱 종료", width="stretch", help="현재 Streamlit 서버를 종료합니다. 저장하지 않은 편집 내용은 먼저 저장하세요."):
        st.warning("앱을 종료합니다. 터미널의 Streamlit 서버도 곧 멈춥니다.")
        schedule_app_shutdown()
        st.stop()

state = st.session_state.get("ocr_state")

if state is None:
    st.info("TXT를 업로드하거나 저장된 작업을 선택하세요.")
    st.stop()

if st.session_state.get("plain_notice"):
    st.info(st.session_state.pop("plain_notice"))

if st.session_state.get("export_notice"):
    st.success(st.session_state.pop("export_notice"))

document, block = plain_document_and_block(state)
source_text = state["source_text"]
saved_after = block["text"]

editor_key = f"after_editor_{state['file_id']}"
if st.session_state.get("editor_file_id") != state["file_id"]:
    st.session_state.editor_file_id = state["file_id"]
    st.session_state[editor_key] = saved_after
elif editor_key not in st.session_state:
    st.session_state[editor_key] = saved_after

current_after = st.session_state.get(editor_key, saved_after)
preview_logs = build_preview_logs(state, document, block, current_after)

with top_finish:
    st.write("최종 산출물")
    if st.button("검수 완료 · 내보내기", type="primary", width="stretch"):
        try:
            # Make sure the very latest AFTER edits are saved before exporting.
            autosave_after_edit(force=True)
            paths = export_all(DATA_DIR, st.session_state.ocr_state)
            st.session_state.export_paths = paths
            st.session_state.export_notice = "AFTER TXT, JSONL, Excel 생성을 완료했습니다."
            st.rerun()
        except Exception as exc:
            st.error(f"내보내기 실패: {exc}")

st.write(
    f"**file_id:** `{state['file_id']}` · 인코딩 `{state['encoding']}` · "
    f"현재 수정 로그 {len(preview_logs)}건"
)
render_downloads(state)

left, center, right = st.columns([1.05, 1.75, 1.05], gap="large")

with left:
    st.subheader("BEFORE 원본")
    before_query = st.text_input("BEFORE 검색", placeholder="검색어")
    st.code(numbered_text(source_text, before_query), language=None, line_numbers=False)

with center:
    st.subheader("AFTER")
    st.caption(
        "처음에는 BEFORE와 완전히 같습니다. 여기서 오타, 줄바꿈, 글자 위치만 편하게 고치면 됩니다. "
        "다른 곳을 클릭하거나 Ctrl+Enter를 누르면 자동으로 저장됩니다."
    )

    st.text_area("AFTER 텍스트", key=editor_key, height=640, on_change=autosave_after_edit)
    st.text_input("메모", key="after_memo", placeholder="선택 입력")

    save_col, status_col = st.columns([1, 3], vertical_alignment="center")
    with save_col:
        if st.button("지금 저장", width="stretch"):
            # force=True always re-saves, so this is a simple manual override.
            autosave_after_edit(force=True)
            st.success("저장했습니다.")
    with status_col:
        render_autosave_heartbeat()

    with st.expander("전체 diff 보기", expanded=False):
        st.caption("대용량 TXT에서는 diff 계산이 오래 걸릴 수 있어 필요할 때만 실행합니다.")
        if st.button("전체 diff 계산", key=f"full_diff_{state['file_id']}"):
            with st.spinner("전체 diff를 계산하는 중입니다..."):
                st.code(unified_diff(source_text, current_after) or "변경 없음", language="diff")

with right:
    st.subheader("수정 로그")
    if not preview_logs:
        st.info("저장된 수정 로그가 없습니다. AFTER를 수정한 뒤 저장하면 로그가 생성됩니다.")
    else:
        counts = pd.Series([log.get("operation") for log in preview_logs]).value_counts().rename_axis("operation").reset_index(name="count")
        st.dataframe(counts, hide_index=True)
        display_logs = preview_logs[:500]
        if len(preview_logs) > len(display_logs):
            st.caption(f"화면 속도를 위해 수정 로그 {len(preview_logs)}건 중 처음 {len(display_logs)}건만 표시합니다.")
        st.caption(
            "표를 직접 고칠 수 있습니다. 단, AFTER를 다시 고쳐서 자동저장되면 "
            "이 표는 새 비교 결과로 다시 만들어지며 직접 고친 내용은 덮어써집니다."
        )

        original_rows = [log_to_row(log) for log in display_logs]
        edited_df = st.data_editor(
            pd.DataFrame(original_rows),
            hide_index=True,
            height=380,
            num_rows="fixed",
            key=f"log_editor_{state['file_id']}",
        )
        # A mixed int/"9~16"-string column (BEFORE줄/AFTER줄) round-trips through
        # the data editor as text, so compare both sides through the same
        # normalization instead of raw equality - otherwise unedited numeric
        # cells look "changed" on every rerun and silently overwrite the log.
        normalize_row = lambda row: {column: _normalize_editor_value(value) for column, value in row.items()}
        original_rows_normalized = [normalize_row(row) for row in original_rows]
        edited_rows = [normalize_row(row) for row in edited_df.to_dict("records")]

        if edited_rows != original_rows_normalized:
            save_edited_logs(state, preview_logs, edited_rows)
            st.toast("수정 로그를 저장했습니다.", icon="✅")

        selected = st.selectbox("상세 diff", list(range(len(display_logs))), format_func=lambda i: f"{i + 1}. {display_logs[i]['operation']}")
        selected_log = display_logs[selected]
        st.code(
            selected_log.get("diff")
            or unified_diff(str(selected_log.get("before_text") or ""), str(selected_log.get("after_text") or ""))
            or "변경 없음",
            language="diff",
        )
