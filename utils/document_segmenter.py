from __future__ import annotations

import re
from dataclasses import dataclass, asdict


PAGE_PATTERNS = (
    re.compile(r"^\s*page\s+\d+(?:\s+of\s+\d+)?\s*$", re.I),
    re.compile(r"^\s*-\s*\d+\s*-\s*$"),
)
KEYWORDS = re.compile(
    r"\b(invoice|permit|certificate|receipt|report|application|plan|confirmation|license|no\.|date)\b"
    r"|신청서|계획서|보고서|확인서|허가서|접수증|증명서|인보이스",
    re.I,
)


@dataclass
class BoundaryCandidate:
    line_index: int
    line_number: int
    preview: str
    score: int
    confidence: str
    reasons: list[str]


def _header_like(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 100:
        return False
    letters = [c for c in stripped if c.isalpha()]
    upper_ratio = sum(c.isupper() for c in letters) / max(1, len(letters))
    korean_or_upper = upper_ratio >= 0.78 or bool(re.fullmatch(r"[가-힣A-Z0-9 ()·._/-]{3,60}", stripped))
    return korean_or_upper and not stripped.endswith((".", "다.", ",", ";"))


def detect_boundary_candidates(text: str) -> list[dict]:
    """Return explainable, document-agnostic boundary hints; the user remains authoritative."""
    lines = text.splitlines()
    candidates: list[BoundaryCandidate] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        score, reasons = 0, []
        before_blank = i == 0 or not lines[i - 1].strip()
        after_blank = i == len(lines) - 1 or not lines[i + 1].strip()
        blank_run = 0
        j = i - 1
        while j >= 0 and not lines[j].strip():
            blank_run += 1
            j -= 1
        if i == 0:
            score += 5
            reasons.append("파일 시작")
        if blank_run >= 2:
            score += 2
            reasons.append("연속 빈 줄 뒤")
        if _header_like(stripped):
            score += 2
            reasons.append("제목 형태")
        if KEYWORDS.search(stripped):
            score += 2
            reasons.append("일반 문서 키워드")
        if re.match(r"^(?:\[.*\]|={3,}|#{1,3}\s+)", stripped):
            score += 2
            reasons.append("구분/제목 표식")
        if re.search(r"\b\d{4}[./-]\d{1,2}[./-]\d{1,2}\b", stripped) and (before_blank or after_blank):
            score += 1
            reasons.append("독립 날짜")
        if any(pattern.match(stripped) for pattern in PAGE_PATTERNS):
            score += 1
            reasons.append("페이지 표식")
        if score >= 2 or i == 0:
            confidence = "strong" if score >= 5 else "medium" if score >= 3 else "weak"
            candidates.append(BoundaryCandidate(i, i + 1, stripped[:160], score, confidence, reasons))
    return [asdict(candidate) for candidate in candidates]


def suggested_boundaries(text: str, candidates: list[dict] | None = None) -> list[int]:
    """Use only strong hints automatically to avoid aggressive over-segmentation."""
    lines = text.splitlines()
    candidates = candidates or detect_boundary_candidates(text)
    starts = {0}
    for candidate in candidates:
        if candidate["line_index"] > 0 and candidate["confidence"] == "strong":
            # Do not split on a page number alone.
            if candidate["reasons"] != ["페이지 표식"]:
                starts.add(candidate["line_index"])
    return sorted(index for index in starts if index < len(lines))


def split_text_at_boundaries(text: str, boundaries: list[int]) -> list[dict]:
    lines = text.splitlines()
    if not lines or not text.strip():
        return []
    starts = sorted({0, *(int(value) for value in boundaries if 0 <= int(value) < len(lines))})
    documents = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[start:end]).strip("\n")
        if chunk.strip():
            documents.append({"start_line": start + 1, "end_line": end, "text": chunk})
    return documents

