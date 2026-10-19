"""Core data types shared by detectors, the DOCX engine, evaluation and the API."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Span:
    """A character span ``text[start:end]`` labelled as one PII type.

    ``source`` records provenance: ``model`` (neural NER), ``rule`` (pattern
    recogniser), ``propagated`` (document-level consistency), ``deny`` (user
    deny-list) or ``user`` (added manually in the review UI).
    """

    start: int
    end: int
    label: str
    score: float = 1.0
    source: str = "model"

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"invalid span bounds: {self.start}..{self.end}")

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        return asdict(self)
