from __future__ import annotations

import os
from pathlib import Path

from .id_utils import make_file_id

SUPPORTED_ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr")


class TextLoadError(ValueError):
    pass


def decode_text(raw: bytes) -> tuple[str, str]:
    """Decode OCR text without silently replacing damaged characters."""
    if not raw:
        raise TextLoadError("빈 TXT 파일입니다.")
    for encoding in SUPPORTED_ENCODINGS:
        try:
            text = raw.decode(encoding)
            if text.strip():
                return normalize_newlines(text), encoding
        except UnicodeDecodeError:
            continue
    raise TextLoadError("인코딩을 판별할 수 없습니다. UTF-8, CP949 또는 EUC-KR TXT를 사용하세요.")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def atomic_write_text(path: Path, text: str) -> None:
    """Write through a temporary file so an interrupted save does not corrupt state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def ingest_upload(filename: str, raw: bytes, data_dir: Path) -> dict[str, str]:
    text, encoding = decode_text(raw)
    file_id = make_file_id(filename, raw)
    before_path = data_dir / "before" / f"{file_id}_before.txt"
    # Identical uploads resolve to the same ID and never overwrite a different original.
    if not before_path.exists():
        atomic_write_text(before_path, text)
    return {
        "file_id": file_id,
        "filename": filename,
        "encoding": encoding,
        "text": text,
        "before_path": str(before_path),
    }

