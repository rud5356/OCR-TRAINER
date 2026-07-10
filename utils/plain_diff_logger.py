from __future__ import annotations

import difflib
import itertools
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .diff_utils import unified_diff
from .id_utils import make_correction_id, utc_now
from .log_manager import LOG_FIELDS
from .state_manager import save_state
from .text_loader import atomic_write_text
from .workspace import active_blocks

CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
CIRCLED_RE = re.compile(f"[{CIRCLED_NUMBERS}]")
LINE_NUMBER_RE = re.compile(r"^\s*(?:\d{1,4}[\).:\-]\s+|\d{1,4}\s{2,})")
SPACE_RE = re.compile(r"\s+")
PHONE_RE = re.compile(r"[\dCOIl|]{2,4}\s*-\s*[\dCOIl|]{3,4}\s*-\s*[\dCOIl|]{4}", re.IGNORECASE)

# A blocking key (see _blocking_keys) shared by more lines than this is
# treated as uninformative rather than used to narrow candidates.
MAX_BLOCKING_BUCKET = 60

# OCR 서식에서 자주 필드 키 역할을 하는 단어들이다. 비교는 공백 제거
# 버전으로 하므로 "신 청 인"과 "신청인"은 같은 키로 취급된다.
FIELD_KEYWORDS = (
    "성명",
    "주소",
    "전화번호",
    "전화",
    "이메일",
    "HP",
    "상호",
    "명칭",
    "학명",
    "보통명",
    "신청인",
    "대표자",
    "사업장소재지",
    "소재지",
    "실수요자",
)

OPERATION_PRIORITY = {
    "OCR 오인식 수정": 1,
    "OCR 누락 보정": 2,
    "공백 정리": 3,
    "순서 이동+공백 정리": 3,
    "줄 병합": 4,
    "순서 이동+줄 병합": 4,
    "줄 분리": 5,
    "순서 이동+줄 분리": 5,
    "순서 이동": 6,
    "순서 이동+문장 수정": 6,
    "항목 재구성": 7,
    "문장 내용 수정": 8,
    "삭제 의심": 9,
    "추가 의심": 10,
}


@dataclass(frozen=True)
class LineInfo:
    """A raw OCR line plus comparison-only normalized forms."""

    index: int
    line_no: int
    raw: str
    clean: str
    nospace: str
    number: str | None
    keywords: frozenset[str]


def _strip_line_number(line: str) -> str:
    """Remove external line numbers without touching OCR field numbers like ①."""
    return LINE_NUMBER_RE.sub("", line or "", count=1)


def _normalize_space(text: str) -> str:
    return SPACE_RE.sub(" ", _strip_line_number(text).strip())


def _compact(text: str) -> str:
    """Comparison form that ignores OCR-created spaces and line breaks."""
    return "".join(_normalize_space(text).split())


def _extract_number(text: str) -> str | None:
    match = CIRCLED_RE.search(text or "")
    return match.group(0) if match else None


def _extract_keywords(text: str) -> frozenset[str]:
    compact = _compact(text).lower()
    return frozenset(keyword for keyword in FIELD_KEYWORDS if keyword.lower() in compact)


def _prepare_lines(text: str) -> tuple[list[str], list[LineInfo]]:
    """Preprocess lines once and ignore blank lines for matching.

    Blank line changes are usually layout noise in OCR review, while line
    numbers from the original file are preserved through line_no.
    """
    raw_lines = text.splitlines()
    prepared: list[LineInfo] = []
    for raw_index, raw in enumerate(raw_lines, start=1):
        clean = _normalize_space(raw)
        if not clean:
            continue
        prepared.append(
            LineInfo(
                index=len(prepared),
                line_no=raw_index,
                raw=raw.strip(),
                clean=clean,
                nospace=_compact(clean),
                number=_extract_number(clean),
                keywords=_extract_keywords(clean),
            )
        )
    return raw_lines, prepared


def _similarity(before: str, after: str) -> float:
    return difflib.SequenceMatcher(None, before or "", after or "", autojunk=False).ratio()


def _comparison_similarity(before: str, after: str) -> float:
    """Use both spaced and no-space forms, taking the better OCR-oriented score."""
    return max(_similarity(_normalize_space(before), _normalize_space(after)), _similarity(_compact(before), _compact(after)))


def _char_bag(text: str) -> Counter:
    return Counter(_compact(text))


def _features(lines: list[LineInfo]) -> tuple[str | None, frozenset[str]]:
    number = next((line.number for line in lines if line.number), None)
    keywords: set[str] = set()
    for line in lines:
        keywords.update(line.keywords)
    return number, frozenset(keywords)


def _has_structural_conflict(before_lines: list[LineInfo], after_lines: list[LineInfo]) -> bool:
    """Prevent wrong matches such as ④주소 -> ①상호."""
    before_number, before_keywords = _features(before_lines)
    after_number, after_keywords = _features(after_lines)

    if before_number and after_number and before_number != after_number:
        return True
    if before_keywords and after_keywords and before_keywords.isdisjoint(after_keywords):
        return True
    return False


def _has_same_anchor(before_lines: list[LineInfo], after_lines: list[LineInfo]) -> bool:
    before_number, before_keywords = _features(before_lines)
    after_number, after_keywords = _features(after_lines)
    if before_number and after_number and before_number == after_number:
        return True
    return bool(before_keywords and after_keywords and not before_keywords.isdisjoint(after_keywords))


def _join_clean(lines: list[LineInfo], separator: str = " ") -> str:
    return separator.join(line.clean for line in lines)


def _join_log_text(lines: list[LineInfo]) -> str:
    """Use slash for multi-line BEFORE/AFTER snippets so Excel stays readable."""
    return " / ".join(line.clean for line in lines)


def _range_label(lines: list[LineInfo]) -> int | str | None:
    if not lines:
        return None
    start = lines[0].line_no
    end = lines[-1].line_no
    return start if start == end else f"{start}~{end}"


def _context(raw_lines: list[str], line_no: int | None, radius: int = 1) -> str:
    if line_no is None:
        return ""
    index = max(0, line_no - 1)
    start = max(0, index - radius)
    end = min(len(raw_lines), index + radius + 1)
    return "\n".join(f"{number + 1}: {raw_lines[number]}" for number in range(start, end))


def _is_subsequence(short: str, long: str) -> bool:
    if not short or not long:
        return False
    cursor = 0
    for char in long:
        if cursor < len(short) and short[cursor] == char:
            cursor += 1
    return cursor == len(short)


def _phone_ocr_normalized(text: str) -> str:
    table = str.maketrans({"C": "0", "c": "0", "O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})
    return re.sub(r"[^0-9]", "", (text or "").translate(table))


def _phone_digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text or "")


def _is_phone_ocr_fix(before: str, after: str) -> bool:
    if not PHONE_RE.search(before or "") or not PHONE_RE.search(after or ""):
        return False
    before_fixed = _phone_ocr_normalized(before)
    after_digits = _phone_digits(after)
    if len(after_digits) < 9:
        return False
    return before_fixed == after_digits and _phone_digits(before) != after_digits


def _is_missing_fix(before: str, after: str) -> bool:
    before_compact = _compact(before)
    after_compact = _compact(after)
    if len(before_compact) < 2 or len(after_compact) <= len(before_compact):
        return False
    if before_compact[0] != after_compact[0] or before_compact[-1] != after_compact[-1]:
        return False
    length_ratio = len(before_compact) / max(len(after_compact), 1)
    return length_ratio >= 0.45 and _is_subsequence(before_compact, after_compact)


def _is_prefix_fix(before: str, after: str) -> bool:
    """BEFORE is an exact prefix of AFTER - e.g. a form label whose value was
    unreadable OCR noise and has now been filled in/corrected.

    This is strong evidence on its own, so unlike _is_missing_fix it isn't
    gated by a length-ratio check. But that also means it must not be used on
    the same-line-number fast path: a label on one line with its value
    genuinely on the *next* line looks identical at the same line number, and
    the dedicated merge detector should get the first chance at that instead.
    """
    before_compact = _compact(before)
    after_compact = _compact(after)
    return len(before_compact) >= 2 and before_compact != after_compact and after_compact.startswith(before_compact)


def _classify_pair(before: LineInfo, after: LineInfo, allow_prefix_fix: bool = True) -> tuple[str, str, str, float] | None:
    """Classify a one-line BEFORE/AFTER pair using the requested priority.

    allow_prefix_fix=False disables the _is_prefix_fix shortcut for callers
    that match same-line-number pairs first (see _is_prefix_fix docstring).
    """
    if _has_structural_conflict([before], [after]):
        return None

    before_text = before.clean
    after_text = after.clean
    score = _comparison_similarity(before_text, after_text)
    moved = before.line_no != after.line_no

    if before.clean == after.clean:
        if moved:
            return (
                "순서 이동",
                "layout_order_error",
                f"BEFORE {before.line_no}번째 줄의 문장이 AFTER {after.line_no}번째 줄로 이동되었습니다.",
                0.99,
            )
        return None

    if _is_phone_ocr_fix(before_text, after_text):
        return "OCR 오인식 수정", "ocr_typo", "전화번호의 OCR 오인식 문자를 수정했습니다.", 0.99
    if _is_missing_fix(before_text, after_text) or (allow_prefix_fix and _is_prefix_fix(before_text, after_text)):
        return "OCR 누락 보정", "ocr_missing_text", "OCR에서 누락된 글자를 보정했습니다.", 0.94
    if before.nospace == after.nospace and before.clean != after.clean:
        if moved:
            return (
                "순서 이동+공백 정리",
                "layout_order_error",
                f"BEFORE {before.line_no}번째 줄의 문장이 AFTER {after.line_no}번째 줄로 이동되었고, 공백이 정리되었습니다.",
                0.99,
            )
        return "공백 정리", "spacing_error", "공백 또는 띄어쓰기를 정리했습니다.", 0.98
    if before.nospace and after.nospace and _char_bag(before_text) == _char_bag(after_text) and before.nospace != after.nospace:
        return "순서 이동", "layout_order_error", "같은 글자가 다른 순서로 이동했습니다.", round(max(score, 0.90), 4)

    threshold = 0.78
    if _has_same_anchor([before], [after]):
        threshold = 0.68
    if score >= threshold:
        if moved:
            return (
                "순서 이동+문장 수정",
                "layout_order_error",
                f"BEFORE {before.line_no}번째 줄의 문장이 AFTER {after.line_no}번째 줄로 이동되었고, 문장 내용이 수정되었습니다.",
                round(score, 4),
            )
        return "문장 내용 수정", "other", "문장 내용이 수정되었습니다.", round(score, 4)
    return None


def _merge_score(before_window: list[LineInfo], after_line: LineInfo) -> float | None:
    """Score consecutive BEFORE lines against one AFTER line for line merge."""
    if _has_structural_conflict(before_window, [after_line]):
        return None

    before_joined = _join_clean(before_window, " ")
    score = _comparison_similarity(before_joined, after_line.clean)
    before_compact = _compact(before_joined)
    after_compact = after_line.nospace

    # 줄 병합은 여러 BEFORE 줄이 AFTER 한 줄로 합쳐지는 경우다.
    # AFTER가 BEFORE 묶음보다 훨씬 짧으면 "신 청 인 / 실 자 -> 신청인" 같은
    # 잘못된 병합이 되므로 후보에서 제외한다.
    if len(before_compact) > len(after_compact) * 1.05:
        return None

    if before_compact == after_compact:
        score = 1.0
    elif before_compact and before_compact in after_compact:
        score = max(score, 0.92)

    if _has_same_anchor(before_window, [after_line]):
        score = min(1.0, score + 0.06)

    threshold = 0.88
    if _has_same_anchor(before_window, [after_line]):
        threshold = 0.80
    return score if score >= threshold else None


def _split_score(before_line: LineInfo, after_window: list[LineInfo]) -> float | None:
    """Score one BEFORE line against consecutive AFTER lines for line split."""
    if _has_structural_conflict([before_line], after_window):
        return None
    after_joined = _join_clean(after_window, " ")
    score = _comparison_similarity(before_line.clean, after_joined)
    after_compact = _compact(after_joined)
    if len(after_compact) > len(before_line.nospace) * 1.05:
        return None
    if before_line.nospace == after_compact:
        score = 1.0
    if _has_same_anchor([before_line], after_window):
        score = min(1.0, score + 0.06)
    return score if score >= 0.88 else None


def _new_log(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_raw_lines: list[str],
    after_raw_lines: list[str],
    before_lines: list[LineInfo],
    after_lines: list[LineInfo],
    operation: str,
    error_type: str,
    reason: str,
    confidence: float,
    memo: str,
    before_text: str | None = None,
    after_text: str | None = None,
) -> dict:
    before_text = _join_log_text(before_lines) if before_text is None else before_text
    after_text = _join_log_text(after_lines) if after_text is None else after_text
    before_start = before_lines[0].line_no if before_lines else None
    after_start = after_lines[0].line_no if after_lines else None

    record = {field: None for field in LOG_FIELDS}
    record.update(
        {
            "file_id": file_id,
            "document_id": document_id,
            "correction_id": make_correction_id(),
            "timestamp": utc_now(),
            "user_id": user_id or "anonymous",
            "operation": operation,
            "error_type": error_type,
            "block_id": block_id,
            "before_position": _range_label(before_lines),
            "after_position": _range_label(after_lines),
            "before_text": before_text,
            "after_text": after_text,
            "before_context": _context(before_raw_lines, before_start),
            "after_context": _context(after_raw_lines, after_start),
            "diff": unified_diff(before_text, after_text),
            "reason": reason,
            "confidence": round(confidence, 4),
            "memo": memo,
        }
    )
    return record


def _detect_line_merges(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_raw_lines: list[str],
    after_raw_lines: list[str],
    before_lines: list[LineInfo],
    after_lines: list[LineInfo],
    used_before: set[int],
    used_after: set[int],
    memo: str,
) -> list[dict]:
    logs: list[dict] = []

    # Trying every (start, length) window against every AFTER line is
    # O(after_count * before_count * 4). Instead, only try windows that
    # contain at least one line sharing a blocking key with the AFTER line -
    # this is what keeps a document with hundreds of unresolved lines from
    # taking tens of seconds here.
    before_positions_by_key: dict[str, list[int]] = {}
    for position, before in enumerate(before_lines):
        if before.index in used_before:
            continue
        for key in _blocking_keys(before):
            before_positions_by_key.setdefault(key, []).append(position)
    useful_before_keys = {key for key, group in before_positions_by_key.items() if len(group) <= MAX_BLOCKING_BUCKET}

    for after in after_lines:
        if after.index in used_after:
            continue

        after_keys = _blocking_keys(after)
        narrowing_keys = after_keys & useful_before_keys
        matched_positions: set[int] = set()
        for key in narrowing_keys or after_keys:
            matched_positions.update(before_positions_by_key.get(key, ()))
        candidate_starts = {
            start
            for position in matched_positions
            for start in range(max(0, position - 4), position + 1)
        }

        best: tuple[float, int, int, list[LineInfo]] | None = None
        for start in candidate_starts:
            if before_lines[start].index in used_before:
                continue
            for length in range(5, 1, -1):
                end = start + length
                if end > len(before_lines):
                    continue
                window = before_lines[start:end]
                if any(line.index in used_before for line in window):
                    continue
                # 줄 병합은 반드시 원본의 연속 라인만 대상으로 한다.
                if window[-1].line_no - window[0].line_no != length - 1:
                    continue
                score = _merge_score(window, after)
                if score is None:
                    continue
                anchor_bonus = 1 if _has_same_anchor(window, [after]) else 0
                candidate = (score, anchor_bonus, length, window)
                if best is None or candidate[:3] > best[:3]:
                    best = candidate

        if best is None:
            continue

        score, _, _, window = best
        used_after.add(after.index)
        used_before.update(line.index for line in window)
        moved = window[0].line_no != after.line_no
        logs.append(
            _new_log(
                file_id=file_id,
                document_id=document_id,
                block_id=block_id,
                user_id=user_id,
                before_raw_lines=before_raw_lines,
                after_raw_lines=after_raw_lines,
                before_lines=window,
                after_lines=[after],
                operation="순서 이동+줄 병합" if moved else "줄 병합",
                error_type="line_merge",
                reason=(
                    f"BEFORE {_range_label(window)}번째 줄의 여러 줄이 AFTER {after.line_no}번째 줄로 이동되었고, 한 줄로 병합되었습니다."
                    if moved
                    else "여러 줄로 분리된 항목명과 값을 한 줄로 병합했습니다."
                ),
                confidence=score,
                memo=memo,
            )
        )
    return logs


def _detect_line_splits(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_raw_lines: list[str],
    after_raw_lines: list[str],
    before_lines: list[LineInfo],
    after_lines: list[LineInfo],
    used_before: set[int],
    used_after: set[int],
    memo: str,
) -> list[dict]:
    logs: list[dict] = []

    # Same reasoning as _detect_line_merges: only try AFTER windows that
    # contain a line sharing a blocking key with the BEFORE line, instead of
    # every (start, length) window for every remaining BEFORE line.
    after_positions_by_key: dict[str, list[int]] = {}
    for position, after in enumerate(after_lines):
        if after.index in used_after:
            continue
        for key in _blocking_keys(after):
            after_positions_by_key.setdefault(key, []).append(position)
    useful_after_keys = {key for key, group in after_positions_by_key.items() if len(group) <= MAX_BLOCKING_BUCKET}

    for before in before_lines:
        if before.index in used_before:
            continue

        before_keys = _blocking_keys(before)
        narrowing_keys = before_keys & useful_after_keys
        matched_positions: set[int] = set()
        for key in narrowing_keys or before_keys:
            matched_positions.update(after_positions_by_key.get(key, ()))
        candidate_starts = {
            start
            for position in matched_positions
            for start in range(max(0, position - 4), position + 1)
        }

        best: tuple[float, int, list[LineInfo]] | None = None
        for start in candidate_starts:
            if after_lines[start].index in used_after:
                continue
            for length in range(5, 1, -1):
                end = start + length
                if end > len(after_lines):
                    continue
                window = after_lines[start:end]
                if any(line.index in used_after for line in window):
                    continue
                if window[-1].line_no - window[0].line_no != length - 1:
                    continue
                score = _split_score(before, window)
                if score is None:
                    continue
                candidate = (score, length, window)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        if best is None:
            continue
        score, _, window = best
        used_before.add(before.index)
        used_after.update(line.index for line in window)
        moved = before.line_no != window[0].line_no
        logs.append(
            _new_log(
                file_id=file_id,
                document_id=document_id,
                block_id=block_id,
                user_id=user_id,
                before_raw_lines=before_raw_lines,
                after_raw_lines=after_raw_lines,
                before_lines=[before],
                after_lines=window,
                operation="순서 이동+줄 분리" if moved else "줄 분리",
                error_type="line_split",
                reason=(
                    f"BEFORE {before.line_no}번째 줄의 문장이 AFTER {_range_label(window)}번째 줄로 이동되었고, 여러 줄로 분리되었습니다."
                    if moved
                    else "한 줄에 있던 항목을 여러 줄로 분리했습니다."
                ),
                confidence=score,
                memo=memo,
            )
        )
    return logs


def _detect_same_position_pairs(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_raw_lines: list[str],
    after_raw_lines: list[str],
    before_lines: list[LineInfo],
    after_lines: list[LineInfo],
    used_before: set[int],
    used_after: set[int],
    memo: str,
) -> list[dict]:
    """Fast-path simple edits that stayed on the same physical line.

    This avoids comparing every changed line with every other line on large OCR
    files.  Line merge/split candidates are excluded by a length-ratio guard so
    they can still be handled by the dedicated merge/split detectors.
    """
    before_by_line_no = {line.line_no: line for line in before_lines if line.index not in used_before}
    logs: list[dict] = []
    for after in after_lines:
        if after.index in used_after:
            continue
        before = before_by_line_no.get(after.line_no)
        if before is None:
            continue

        classified = _classify_pair(before, after, allow_prefix_fix=False)
        if classified is None:
            continue

        operation, error_type, reason, confidence = classified
        shorter = max(min(len(before.nospace), len(after.nospace)), 1)
        length_ratio = max(len(before.nospace), len(after.nospace)) / shorter
        safe_operations = {"OCR 오인식 수정", "OCR 누락 보정", "공백 정리"}
        if operation == "문장 내용 수정" and length_ratio > 1.35:
            continue
        if operation not in safe_operations and operation != "문장 내용 수정":
            continue

        used_before.add(before.index)
        used_after.add(after.index)
        logs.append(
            _new_log(
                file_id=file_id,
                document_id=document_id,
                block_id=block_id,
                user_id=user_id,
                before_raw_lines=before_raw_lines,
                after_raw_lines=after_raw_lines,
                before_lines=[before],
                after_lines=[after],
                operation=operation,
                error_type=error_type,
                reason=reason,
                confidence=confidence,
                memo=memo,
            )
        )
    return logs


def _blocking_keys(line: LineInfo) -> frozenset[str]:
    """Cheap signature so _detect_general_pairs only compares lines that
    could plausibly match, instead of every BEFORE against every AFTER.

    A real match almost always still shares its first/last couple characters
    (OCR damage rarely eats an entire line) or shares a circled-number/
    keyword anchor. Restricting comparisons to lines sharing at least one key
    turns an O(n*m) full cross-product into roughly O(n*k) - on a document
    with hundreds of reordered lines that is the difference between an
    autosave taking under a second and one taking tens of seconds.
    """
    keys: set[str] = set()
    if line.nospace:
        keys.add(f"pre:{line.nospace[:2]}")
        keys.add(f"suf:{line.nospace[-2:]}")
    if line.number:
        keys.add(f"num:{line.number}")
    keys.update(f"kw:{keyword}" for keyword in line.keywords)
    return frozenset(keys)


def _detect_general_pairs(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_raw_lines: list[str],
    after_raw_lines: list[str],
    before_lines: list[LineInfo],
    after_lines: list[LineInfo],
    used_before: set[int],
    used_after: set[int],
    memo: str,
) -> list[dict]:
    after_by_key: dict[str, list[LineInfo]] = {}
    for after in after_lines:
        if after.index in used_after:
            continue
        for key in _blocking_keys(after):
            after_by_key.setdefault(key, []).append(after)

    # A key shared by too many lines (e.g. a common field keyword repeated
    # across a whole form) narrows nothing - using it would put half the
    # document in every candidate set. Drop those and rely on the line's
    # other, more specific keys instead.
    useful_keys = {key for key, group in after_by_key.items() if len(group) <= MAX_BLOCKING_BUCKET}

    candidates: list[tuple[int, float, int, int, tuple[str, str, str, float]]] = []
    for before in before_lines:
        if before.index in used_before:
            continue
        before_keys = _blocking_keys(before)
        narrowing_keys = before_keys & useful_keys
        candidate_afters: dict[int, LineInfo] = {}
        for key in narrowing_keys or before_keys:
            for after in after_by_key.get(key, ()):
                candidate_afters[after.index] = after
        for after in candidate_afters.values():
            classified = _classify_pair(before, after)
            if classified is None:
                continue
            operation, _, _, confidence = classified
            priority = OPERATION_PRIORITY.get(operation, 99)
            # 가까운 줄을 조금 선호하되, 우선순위/유사도가 먼저다.
            distance = abs(before.line_no - after.line_no)
            candidates.append((priority, -confidence, distance, before.index * 100000 + after.index, classified))

    logs: list[dict] = []
    by_index = {(line.index, "before"): line for line in before_lines}
    by_index.update({(line.index, "after"): line for line in after_lines})

    for _, _, _, packed_index, classified in sorted(candidates):
        before_index = packed_index // 100000
        after_index = packed_index % 100000
        if before_index in used_before or after_index in used_after:
            continue
        before = by_index[(before_index, "before")]
        after = by_index[(after_index, "after")]
        operation, error_type, reason, confidence = classified
        used_before.add(before.index)
        used_after.add(after.index)
        logs.append(
            _new_log(
                file_id=file_id,
                document_id=document_id,
                block_id=block_id,
                user_id=user_id,
                before_raw_lines=before_raw_lines,
                after_raw_lines=after_raw_lines,
                before_lines=[before],
                after_lines=[after],
                operation=operation,
                error_type=error_type,
                reason=reason,
                confidence=confidence,
                memo=memo,
            )
        )
    return logs


def _detect_reconstruction(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_raw_lines: list[str],
    after_raw_lines: list[str],
    before_lines: list[LineInfo],
    after_lines: list[LineInfo],
    used_before: set[int],
    used_after: set[int],
    memo: str,
) -> list[dict]:
    """Try a last meaningful match before falling back to 추가 의심."""
    logs: list[dict] = []
    remaining_before = [line for line in before_lines if line.index not in used_before]
    for after in after_lines:
        if after.index in used_after:
            continue

        likely_parts = [
            line
            for line in remaining_before
            if line.index not in used_before
            and not _has_structural_conflict([line], [after])
            and (line.nospace in after.nospace or bool(line.keywords & after.keywords) or line.number == after.number)
        ][:10]

        best: tuple[float, tuple[LineInfo, ...]] | None = None
        for size in range(2, min(5, len(likely_parts)) + 1):
            for combo in itertools.combinations(likely_parts, size):
                joined = _join_clean(list(combo), " ")
                score = _comparison_similarity(joined, after.clean)
                coverage = sum(len(line.nospace) for line in combo) / max(len(after.nospace), 1)
                if coverage < 0.65:
                    continue
                if score >= 0.82 or (_has_same_anchor(list(combo), [after]) and score >= 0.72):
                    if best is None or score > best[0]:
                        best = (score, combo)

        if best is None:
            continue

        score, combo = best
        used_after.add(after.index)
        used_before.update(line.index for line in combo)
        logs.append(
            _new_log(
                file_id=file_id,
                document_id=document_id,
                block_id=block_id,
                user_id=user_id,
                before_raw_lines=before_raw_lines,
                after_raw_lines=after_raw_lines,
                before_lines=list(combo),
                after_lines=[after],
                operation="항목 재구성",
                error_type="item_reconstruction",
                reason="BEFORE의 여러 위치에 흩어진 조각을 조합해 AFTER 항목으로 재구성했습니다.",
                confidence=score,
                memo=memo,
            )
        )
    return logs


def _fallback_logs(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_raw_lines: list[str],
    after_raw_lines: list[str],
    before_lines: list[LineInfo],
    after_lines: list[LineInfo],
    used_before: set[int],
    used_after: set[int],
    memo: str,
) -> list[dict]:
    logs: list[dict] = []
    for before in before_lines:
        if before.index in used_before:
            continue
        logs.append(
            _new_log(
                file_id=file_id,
                document_id=document_id,
                block_id=block_id,
                user_id=user_id,
                before_raw_lines=before_raw_lines,
                after_raw_lines=after_raw_lines,
                before_lines=[before],
                after_lines=[],
                operation="삭제 의심",
                error_type="delete_suspected",
                reason="AFTER에서 직접 대응되는 문장을 찾지 못했습니다. 검수 필요.",
                confidence=0.35,
                memo=memo,
            )
        )

    for after in after_lines:
        if after.index in used_after:
            continue
        logs.append(
            _new_log(
                file_id=file_id,
                document_id=document_id,
                block_id=block_id,
                user_id=user_id,
                before_raw_lines=before_raw_lines,
                after_raw_lines=after_raw_lines,
                before_lines=[],
                after_lines=[after],
                operation="추가 의심",
                error_type="add_suspected",
                reason="BEFORE에서 직접 대응되는 문장을 찾지 못했습니다. 검수 필요.",
                confidence=0.35,
                memo=memo,
            )
        )
    return logs


def _sort_logs(logs: list[dict]) -> list[dict]:
    def line_start(value) -> int:
        if value is None:
            return 10**9
        if isinstance(value, int):
            return value
        match = re.match(r"(\d+)", str(value))
        return int(match.group(1)) if match else 10**9

    return sorted(
        logs,
        key=lambda log: (
            min(line_start(log.get("after_position")), line_start(log.get("before_position"))),
            OPERATION_PRIORITY.get(str(log.get("operation")), 99),
        ),
    )


def _mark_unchanged_lines(before_lines: list[LineInfo], after_lines: list[LineInfo], used_before: set[int], used_after: set[int]) -> None:
    """Mark truly unchanged lines so they never become 삭제/추가 의심.

    We compare the space-normalized clean text, not the no-space form, because
    "신 청 인" -> "신청인" must remain available for 공백 정리.
    If the line number changed, it must remain available for 순서 이동.
    """
    matcher = difflib.SequenceMatcher(
        None,
        [line.clean for line in before_lines],
        [line.clean for line in after_lines],
        autojunk=False,
    )
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag != "equal":
            continue

        equal_count = before_end - before_start
        mark_shifted_block = equal_count >= 8
        for before, after in zip(before_lines[before_start:before_end], after_lines[after_start:after_end]):
            # If only a few identical lines appear at a different location, keep
            # them available for the requested "순서 이동" log.  But when a large
            # equal block merely shifted because an earlier line was merged or
            # split, marking it as changed makes the diff explode on big OCR
            # files.  Large equal blocks are therefore treated as unchanged.
            if before.line_no != after.line_no and not mark_shifted_block:
                continue
            used_before.add(before.index)
            used_after.add(after.index)


def _remaining_lines(lines: list[LineInfo], used: set[int]) -> list[LineInfo]:
    """Return only unmatched lines so later OCR comparisons stay small."""
    return [line for line in lines if line.index not in used]


def make_plain_revision_logs(
    *,
    file_id: str,
    document_id: str,
    block_id: str,
    user_id: str,
    before_text: str,
    after_text: str,
    memo: str = "",
) -> list[dict]:
    """Generate OCR review logs with OCR-aware matching.

    The matching order follows the requested workflow:
    1. preprocess lines,
    2. detect structural keys/numbers,
    3. match consecutive BEFORE lines merged into one AFTER line,
    4. match remaining similar lines,
    5. only then emit 삭제 의심/추가 의심 fallbacks.
    """
    # 업로드 직후에는 BEFORE와 AFTER가 완전히 같다. 이때 무거운 매칭 루프를
    # 돌릴 이유가 없으므로 즉시 종료해서 대용량 TXT 로딩 시간을 줄인다.
    normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
    if normalize(before_text) == normalize(after_text):
        return []

    before_raw_lines, before_lines = _prepare_lines(before_text)
    after_raw_lines, after_lines = _prepare_lines(after_text)
    used_before: set[int] = set()
    used_after: set[int] = set()
    logs: list[dict] = []

    _mark_unchanged_lines(before_lines, after_lines, used_before, used_after)

    logs.extend(
        _detect_same_position_pairs(
            file_id=file_id,
            document_id=document_id,
            block_id=block_id,
            user_id=user_id,
            before_raw_lines=before_raw_lines,
            after_raw_lines=after_raw_lines,
            before_lines=_remaining_lines(before_lines, used_before),
            after_lines=_remaining_lines(after_lines, used_after),
            used_before=used_before,
            used_after=used_after,
            memo=memo,
        )
    )

    logs.extend(
        _detect_line_merges(
            file_id=file_id,
            document_id=document_id,
            block_id=block_id,
            user_id=user_id,
            before_raw_lines=before_raw_lines,
            after_raw_lines=after_raw_lines,
            before_lines=_remaining_lines(before_lines, used_before),
            after_lines=_remaining_lines(after_lines, used_after),
            used_before=used_before,
            used_after=used_after,
            memo=memo,
        )
    )
    logs.extend(
        _detect_line_splits(
            file_id=file_id,
            document_id=document_id,
            block_id=block_id,
            user_id=user_id,
            before_raw_lines=before_raw_lines,
            after_raw_lines=after_raw_lines,
            before_lines=_remaining_lines(before_lines, used_before),
            after_lines=_remaining_lines(after_lines, used_after),
            used_before=used_before,
            used_after=used_after,
            memo=memo,
        )
    )
    logs.extend(
        _detect_general_pairs(
            file_id=file_id,
            document_id=document_id,
            block_id=block_id,
            user_id=user_id,
            before_raw_lines=before_raw_lines,
            after_raw_lines=after_raw_lines,
            before_lines=_remaining_lines(before_lines, used_before),
            after_lines=_remaining_lines(after_lines, used_after),
            used_before=used_before,
            used_after=used_after,
            memo=memo,
        )
    )
    logs.extend(
        _detect_reconstruction(
            file_id=file_id,
            document_id=document_id,
            block_id=block_id,
            user_id=user_id,
            before_raw_lines=before_raw_lines,
            after_raw_lines=after_raw_lines,
            before_lines=_remaining_lines(before_lines, used_before),
            after_lines=_remaining_lines(after_lines, used_after),
            used_before=used_before,
            used_after=used_after,
            memo=memo,
        )
    )
    logs.extend(
        _fallback_logs(
            file_id=file_id,
            document_id=document_id,
            block_id=block_id,
            user_id=user_id,
            before_raw_lines=before_raw_lines,
            after_raw_lines=after_raw_lines,
            before_lines=_remaining_lines(before_lines, used_before),
            after_lines=_remaining_lines(after_lines, used_after),
            used_before=used_before,
            used_after=used_after,
            memo=memo,
        )
    )
    return _sort_logs(logs)


def save_plain_revision_logs(data_dir: Path, file_id: str, logs: list[dict]) -> Path:
    """Write current logs as JSONL.

    The file represents the current BEFORE-vs-AFTER comparison. Re-saving the
    same AFTER text will not create duplicate JSONL rows because the current
    comparison result is rewritten as the current truth.
    """
    path = data_dir / "logs_jsonl" / f"{file_id}_corrections.jsonl"
    text = "".join(json.dumps(log, ensure_ascii=False, default=str) + "\n" for log in logs)
    atomic_write_text(path, text)
    return path


def save_plain_after_and_logs(
    data_dir: Path,
    state: dict,
    after_text: str,
    user_id: str,
    memo: str = "",
) -> list[dict]:
    """Save the edited AFTER text and regenerate OCR-aware correction logs."""
    document = state["documents"][0]
    block = active_blocks(document)[0]
    block["text"] = after_text
    block["modified"] = after_text != state.get("source_text", "")
    state["completed"] = False
    state.pop("completed_at", None)

    logs = make_plain_revision_logs(
        file_id=state["file_id"],
        document_id=document["document_id"],
        block_id=block["block_id"],
        user_id=user_id,
        before_text=state.get("source_text", ""),
        after_text=after_text,
        memo=memo,
    )
    state["history"] = [log["correction_id"] for log in logs]
    save_state(data_dir, state)
    save_plain_revision_logs(data_dir, state["file_id"], logs)
    return logs
