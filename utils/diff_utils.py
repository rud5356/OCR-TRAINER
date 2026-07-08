from __future__ import annotations

import difflib


def unified_diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="before", tofile="after", lineterm=""
        )
    )


def word_diff(before: str, after: str) -> str:
    before_words, after_words = before.split(), after.split()
    result = []
    for item in difflib.ndiff(before_words, after_words):
        marker, value = item[:2], item[2:]
        if marker == "- ":
            result.append(f"[-{value}-]")
        elif marker == "+ ":
            result.append(f"[+{value}+]")
        elif marker == "  ":
            result.append(value)
    return " ".join(result)


def diff_summary(before: str, after: str, limit: int = 500) -> str:
    value = word_diff(before, after)
    return value if len(value) <= limit else value[: limit - 1] + "…"

