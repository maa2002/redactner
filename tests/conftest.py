"""Shared test doubles: the fine-tuned model is replaced by a stub, so the tests need no GPU or checkpoint."""

import re

from redactner.detector import PIIDetector
from redactner.types import Span


class StubModel:
    """Stands in for the token classifier: tags every occurrence of the given values."""

    name = "stub-model"
    threshold = 0.5

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def detect(self, text: str, threshold=None) -> list[Span]:
        spans = []
        for value, label in self.values.items():
            spans += [Span(m.start(), m.end(), label, 0.9, "model") for m in re.finditer(re.escape(value), text)]
        return spans


def stub_detector(values: dict[str, str]) -> PIIDetector:
    return PIIDetector(StubModel(values))
