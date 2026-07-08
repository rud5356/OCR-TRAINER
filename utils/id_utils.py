from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def make_file_id(filename: str, content: bytes) -> str:
    """Create a readable, collision-resistant ID for one uploaded TXT file."""
    stem = Path(filename).stem
    safe_stem = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", stem).strip("_") or "ocr"
    digest = hashlib.sha256(content).hexdigest()[:10]
    return f"{safe_stem[:60]}_{digest}"


def make_document_id(index: int) -> str:
    return f"D{index:03d}"


def make_block_id(document_id: str) -> str:
    return f"{document_id}_B_{uuid.uuid4().hex[:10]}"


def make_correction_id() -> str:
    return f"C_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}_{uuid.uuid4().hex[:6]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

