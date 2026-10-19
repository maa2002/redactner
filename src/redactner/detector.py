"""The detector behind the app, the CLI and the API: the fine-tuned model plus the reviewer's own lists.

Detection is entirely model-based. Two user choices are layered on top of the model's spans:
terms on the *Always Redact* list are found as whole words and labelled ``CUSTOM``, and values on
the *Never Redact* list are dropped, together with categories the user switched off.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .types import Span


def term_spans(text: str, terms: Iterable[str], label: str = "CUSTOM") -> list[Span]:
    """Case-insensitive, word-bounded occurrences of the user's Always Redact terms."""
    out = []
    for term in {t.strip() for t in terms if t and t.strip()}:
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        out += [Span(m.start(), m.end(), label, 1.0, "deny") for m in pattern.finditer(text)]
    return out


def _priority(span: Span) -> tuple:
    # explicit user decisions outrank model predictions; then confidence, then length
    return (span.source in ("deny", "user"), span.score, span.length)


def resolve_overlaps(spans: Iterable[Span]) -> list[Span]:
    """Merge overlapping spans into their union; the label comes from the strongest member."""
    ordered = sorted(spans, key=lambda sp: (sp.start, -sp.end))
    clusters: list[list[Span]] = []
    for sp in ordered:
        if clusters and sp.start < max(x.end for x in clusters[-1]):
            clusters[-1].append(sp)
        else:
            clusters.append([sp])
    out = []
    for cluster in clusters:
        best = max(cluster, key=_priority)
        start = min(x.start for x in cluster)
        end = max(x.end for x in cluster)
        out.append(Span(start, end, best.label, max(x.score for x in cluster), best.source))
    return out


def apply_policy(text: str, spans: Iterable[Span], labels: Iterable[str] | None = None,
                 allow_terms: Iterable[str] = ()) -> list[Span]:
    """Keep enabled categories (Always Redact spans always) and drop Never Redact values."""
    enabled = set(labels) if labels is not None else None
    allow = {t.strip().lower() for t in allow_terms if t and t.strip()}
    out = []
    for sp in spans:
        if enabled is not None and sp.label not in enabled and sp.label != "CUSTOM":
            continue
        if text[sp.start:sp.end].strip().lower() in allow:
            continue
        out.append(sp)
    return out


class PIIDetector:
    """Fine-tuned token classifier plus the Always Redact list."""

    def __init__(self, model) -> None:
        self.model = model

    def detect(self, text: str, threshold: float | None = None, deny_terms: Iterable[str] = ()) -> list[Span]:
        spans = self.model.detect(text, threshold=threshold) if text.strip() else []
        return resolve_overlaps(spans + term_spans(text, deny_terms))
