"""Hybrid detection: neural spans + validated rules + document-level propagation (M5, M6).

Fusion order: boundary repair → overlap resolution (validated rule label wins, otherwise the
higher score; the extent is always the union, for redaction safety) → document-level propagation
of confident names/usernames/e-mails to every other occurrence → policy (enabled types, allow-list).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .rules import RuleDetector, term_spans
from .types import Span

PROPAGATE_LABELS = ("PERSON", "USERNAME", "EMAIL")
# Capitalised words that are frequently part of names but also common English words/titles.
STOP_COMPONENTS = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dear", "the", "and", "for", "from", "with", "this",
    "that", "date", "name", "phone", "email", "address", "street", "road", "avenue", "may", "june", "july",
    "april", "august", "march", "january", "february", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "north", "south", "east",
    "west", "new", "saint", "st", "van", "von", "de", "del", "la", "le", "di", "da", "bin", "al",
}


# Form field labels the model sometimes tags together with their value (e.g. the "TFN" cell next to the number).
FIELD_LABELS = {
    "tfn", "abn", "acn", "dob", "d.o.b", "bsb", "ssn", "medicare", "phone", "mobile", "tel", "fax", "email",
    "e-mail", "name", "full name", "address", "date of birth", "account", "account number", "passport", "licence",
    "license", "username", "password", "pin", "iban", "swift", "bic", "url", "website",
}


def is_field_label(text: str, span: Span) -> bool:
    return text[span.start:span.end].strip(" :#.-").lower() in FIELD_LABELS


def repair_boundaries(text: str, span: Span) -> Span:
    """Trim whitespace/edge punctuation and extend spans that cut through a word."""
    s, e = span.start, span.end
    while s < e and (text[s].isspace() or text[s] in ",;:"):
        s += 1
    while e > s and (text[e - 1].isspace() or text[e - 1] in ",;:"):
        e -= 1
    while s > 0 and text[s - 1].isalnum() and text[s].isalnum():
        s -= 1
    while e < len(text) and text[e].isalnum() and text[e - 1].isalnum():
        e += 1
    if e <= s:
        return span
    return Span(s, e, span.label, span.score, span.source)


def _priority(span: Span) -> tuple:
    validated = span.source in ("rule", "deny", "user") and span.score >= 0.95
    return (validated, span.score, span.length)


def resolve_overlaps(spans: Iterable[Span]) -> list[Span]:
    """Merge overlapping spans into their union; the label comes from the strongest member."""
    ordered = sorted(spans, key=lambda sp: (sp.start, -sp.end))
    merged: list[list[Span]] = []
    for sp in ordered:
        if merged and sp.start < max(x.end for x in merged[-1]):
            merged[-1].append(sp)
        else:
            merged.append([sp])
    out = []
    for cluster in merged:
        best = max(cluster, key=_priority)
        start = min(x.start for x in cluster)
        end = max(x.end for x in cluster)
        out.append(Span(start, end, best.label, max(x.score for x in cluster), best.source))
    return out


def merge_adjacent(text: str, spans: Sequence[Span]) -> list[Span]:
    """Join same-type spans separated only by a space (or a comma for addresses)."""
    out: list[Span] = []
    for sp in sorted(spans, key=lambda x: x.start):
        if out and out[-1].label == sp.label:
            gap = text[out[-1].end:sp.start]
            if (gap.strip() == "" and len(gap) <= 1 and "\n" not in gap) or (
                    sp.label == "ADDRESS" and gap.strip() == "," and len(gap) <= 2):
                prev = out.pop()
                out.append(Span(prev.start, sp.end, prev.label, max(prev.score, sp.score), prev.source))
                continue
        out.append(sp)
    return out


def propagate(text: str, spans: Sequence[Span], min_score: float = 0.6) -> list[Span]:
    """Redact every other occurrence of confidently detected names, usernames and e-mails (M6)."""
    surfaces: dict[str, str] = {}
    for sp in spans:
        if sp.label not in PROPAGATE_LABELS or sp.score < min_score:
            continue
        surface = text[sp.start:sp.end].strip()
        if len(surface) >= 3:
            surfaces.setdefault(surface.lower(), sp.label)
        if sp.label == "PERSON":
            for part in re.findall(r"[A-Za-z][A-Za-z'’-]+", surface):
                if len(part) >= 3 and part.lower() not in STOP_COMPONENTS and part[0].isupper():
                    surfaces.setdefault(part.lower(), "PERSON")
    covered = [(sp.start, sp.end) for sp in spans]
    added: list[Span] = []
    for surface, label in surfaces.items():
        pattern = re.compile(rf"(?<![\w@.-]){re.escape(surface)}(?![\w@-])", re.IGNORECASE)
        for m in pattern.finditer(text):
            if label == "PERSON" and not m.group(0)[0].isupper():
                continue  # name components only when capitalised ("Brown" yes, "brown" no)
            if any(s <= m.start() and m.end() <= e for s, e in covered):
                continue
            added.append(Span(m.start(), m.end(), label, 0.9, "propagated"))
            covered.append((m.start(), m.end()))
    return added


class HybridDetector:
    """RedactNER detector: fine-tuned model + validated rules + propagation."""

    name = "hybrid"

    def __init__(self, model_detector=None, rule_detector: RuleDetector | None = None,
                 use_rules: bool = True, use_propagation: bool = True) -> None:
        self.model = model_detector
        self.rules = rule_detector or RuleDetector()
        self.use_rules = use_rules
        self.use_propagation = use_propagation

    def detect(self, text: str, threshold: float | None = None, deny_terms: Iterable[str] = ()) -> list[Span]:
        spans: list[Span] = []
        if self.model is not None:
            spans += self.model.detect(text, threshold=threshold)
        if self.use_rules:
            spans += self.rules.detect(text)
        spans += term_spans(text, deny_terms)
        spans = [repair_boundaries(text, sp) for sp in spans]
        spans = [sp for sp in spans if sp.source != "model" or not is_field_label(text, sp)]
        spans = merge_adjacent(text, resolve_overlaps(spans))
        if self.use_propagation:
            spans = resolve_overlaps(spans + propagate(text, spans))
        return spans


def apply_policy(text: str, spans: Iterable[Span], labels: Iterable[str] | None = None,
                 allow_terms: Iterable[str] = ()) -> list[Span]:
    """Keep enabled types (CUSTOM deny-list spans always) and drop allow-listed surfaces."""
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
