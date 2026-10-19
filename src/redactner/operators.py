"""Redaction operators: what replaces a detected span.

``label`` → ``[PERSON_1]`` with per-document numbering by normalised value (the same person gets the
same tag everywhere, so "who did what" stays readable); ``mask`` → a fixed-length black-out so the
length of a name does not leak.
"""

from __future__ import annotations

import re

MASK = "█████"


def normalise_value(value: str) -> str:
    value = value.strip().lower()
    if sum(ch.isdigit() for ch in value) >= max(4, len(value) // 2):
        return re.sub(r"\D", "", value)
    return re.sub(r"\s+", " ", value)


class ReplacementBuilder:
    """Produces consistent replacement strings within one document."""

    def __init__(self, mode: str = "label") -> None:
        if mode not in ("label", "mask"):
            raise ValueError(f"unknown redaction mode: {mode}")
        self.mode = mode
        self._ids: dict[tuple[str, str], int] = {}
        self._counters: dict[str, int] = {}

    def __call__(self, label: str, value: str) -> str:
        if self.mode == "mask":
            return MASK
        tag = "REDACTED" if label == "CUSTOM" else label
        key = (tag, normalise_value(value))
        if key not in self._ids:
            self._counters[tag] = self._counters.get(tag, 0) + 1
            self._ids[key] = self._counters[tag]
        return f"[{tag}_{self._ids[key]}]"
