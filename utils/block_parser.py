from __future__ import annotations

import re

from .id_utils import make_block_id


NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[가-힣A-Za-z][.)]|[-*•])\s+")
FIELD = re.compile(r"^\s*[^:\n]{1,30}\s*:\s*\S+")


def classify_block(text: str) -> str:
    stripped = text.strip()
    if NUMBERED.match(stripped):
        return "list_item"
    if FIELD.match(stripped):
        return "field"
    if len(stripped) <= 80 and not stripped.endswith((".", "다.")):
        return "heading"
    return "paragraph"


def parse_blocks(text: str, document_id: str) -> list[dict]:
    """Split on blank lines and obvious item/header transitions, preserving all text."""
    lines = text.splitlines()
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if not line.strip():
            if current:
                groups.append(current)
                current = []
            continue
        starts_new = bool(current and (NUMBERED.match(line) or FIELD.match(line)))
        if starts_new:
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)
    blocks = []
    for order, group in enumerate(groups):
        block_text = "\n".join(group).strip()
        blocks.append(
            {
                "block_id": make_block_id(document_id),
                "original_text": block_text,
                "text": block_text,
                "original_order": order,
                "current_order": order,
                "block_type": classify_block(block_text),
                "modified": False,
                "deleted": False,
            }
        )
    return blocks

