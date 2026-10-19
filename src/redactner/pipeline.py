"""End-to-end facade: DOCX → detection → review → redacted DOCX.

The same object backs the CLI, the web API and notebook 03's demo cell.
"""

from __future__ import annotations

import bisect
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from .docx_engine import DocxDocument
from .fusion import HybridDetector, apply_policy, resolve_overlaps
from .labels import PRESETS
from .operators import ReplacementBuilder
from .rules import RuleDetector
from .types import Span


def build_detectors(model_dir: str | Path | None, device: str | None = None) -> dict[str, HybridDetector]:
    """The three detector configurations exposed by the app: hybrid, model only, rules only."""
    rules = RuleDetector()
    detectors = {"rules": HybridDetector(None, rules, use_rules=True, use_propagation=False)}
    if model_dir and Path(model_dir).exists():
        from .inference import TokenClassifierDetector  # heavy import (torch) only when a model exists

        model = TokenClassifierDetector(model_dir, device=device)
        detectors["hybrid"] = HybridDetector(model, rules, use_rules=True, use_propagation=True)
        detectors["model"] = HybridDetector(model, use_rules=False, use_propagation=False)
    return detectors


def split_by_segment(span: Span, starts: list[int], lengths: list[int]) -> list[tuple[int, int, int]]:
    """Map a document-level span onto (segment index, local start, local end) pieces."""
    pieces = []
    i = max(0, bisect.bisect_right(starts, span.start) - 1)
    while i < len(starts) and starts[i] < span.end:
        s = max(span.start, starts[i]) - starts[i]
        e = min(span.end, starts[i] + lengths[i]) - starts[i]
        if e > s:
            pieces.append((i, s, e))
        i += 1
    return pieces


class RedactionPipeline:
    def __init__(self, detectors: dict[str, HybridDetector]) -> None:
        self.detectors = detectors

    @property
    def default_detector(self) -> str:
        return "hybrid" if "hybrid" in self.detectors else "rules"

    # -- analysis ----------------------------------------------------------------------------
    def detect_text(self, text: str, detector: str | None = None, labels: Iterable[str] | None = None,
                    threshold: float | None = None, deny_terms: Iterable[str] = (),
                    allow_terms: Iterable[str] = ()) -> list[Span]:
        det = self.detectors[detector or self.default_detector]
        spans = det.detect(text, threshold=threshold, deny_terms=list(deny_terms))
        return apply_policy(text, spans, labels if labels is not None else PRESETS["strict"], allow_terms)

    def analyze(self, doc: DocxDocument, **kwargs) -> dict:
        t0 = time.perf_counter()
        text, starts = doc.document_text()
        spans = self.detect_text(text, **kwargs)
        lengths = [len(seg.text) for seg in doc.segments]
        entities = []
        for sp in spans:
            for seg_idx, s, e in split_by_segment(sp, starts, lengths):
                seg = doc.segments[seg_idx]
                entities.append({"id": f"e{len(entities)}", "segment_id": seg.id, "start": s, "end": e,
                                 "label": sp.label, "score": round(sp.score, 4), "source": sp.source,
                                 "text": seg.text[s:e]})
        loc = {seg.id: seg.location for seg in doc.segments}
        hidden = {seg.id for seg in doc.segments if getattr(seg, "hidden", False)}
        visible = [e for e in entities if e["segment_id"] not in hidden]  # skip duplicate text-box copies
        return {
            "segments": [seg.to_dict() for seg in doc.segments],  # text + layout (style, table, marks, …)
            "entities": entities,
            "summary": {"by_label": dict(Counter(e["label"] for e in visible)),
                        "by_location": dict(Counter(loc[e["segment_id"]] for e in visible))},
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    # -- redaction ---------------------------------------------------------------------------
    @staticmethod
    def redact(doc: DocxDocument, entities: list[dict], mode: str = "label", exclude_ids: Iterable[str] = (),
               extra_spans: Iterable[dict] = ()) -> tuple[bytes, dict]:
        t0 = time.perf_counter()
        seg_text = {seg.id: seg.text for seg in doc.segments}
        loc = {seg.id: seg.location for seg in doc.segments}
        excluded = RedactionPipeline._mirror_exclusions(doc, entities, set(exclude_ids))
        chosen = [e for e in entities if e["id"] not in excluded]
        for x in extra_spans:
            if x["segment_id"] in seg_text and 0 <= x["start"] < x["end"] <= len(seg_text[x["segment_id"]]):
                chosen.append({**x, "source": "user", "text": seg_text[x["segment_id"]][x["start"]:x["end"]]})

        builder = ReplacementBuilder(mode)
        replacements: dict[str, list[tuple[int, int, str]]] = {}
        by_segment: dict[str, list[Span]] = {}
        for e in chosen:
            by_segment.setdefault(e["segment_id"], []).append(
                Span(e["start"], e["end"], e["label"], float(e.get("score", 1.0)), e.get("source", "user")))
        counts_label, counts_loc, values = Counter(), Counter(), set()
        for seg in doc.segments:  # document order, so [TYPE_n] numbering matches the UI's live preview
            if seg.id not in by_segment:
                continue
            for sp in resolve_overlaps(by_segment[seg.id]):
                value = seg.text[sp.start:sp.end]
                replacements.setdefault(seg.id, []).append((sp.start, sp.end, builder(sp.label, value)))
                if not getattr(seg, "hidden", False):
                    counts_label[sp.label] += 1
                    counts_loc[loc[seg.id]] += 1
                values.add(value)
        # The safety-net scan replaces every remaining copy of a redacted value, except values the
        # reviewer explicitly chose to keep somewhere, which would otherwise be wiped anyway.
        kept = sorted({e["text"] for e in entities if e["id"] in excluded})
        data, report = doc.redact(replacements, redacted_values=sorted(values), keep_values=kept,
                                  scrub_metadata=True)
        report.update({"counts_by_label": dict(counts_label), "counts_by_location": dict(counts_loc),
                       "elapsed_ms": round((time.perf_counter() - t0) * 1000)})
        return data, report

    @staticmethod
    def _mirror_exclusions(doc: DocxDocument, entities: list[dict], excluded: set[str]) -> set[str]:
        """Apply a "keep" decision to hidden duplicate copies (legacy text-box fallbacks) of the same text."""
        by_id = {seg.id: seg for seg in doc.segments}
        kept_spots = {(by_id[e["segment_id"]].text, e["start"], e["end"]) for e in entities
                      if e["id"] in excluded and e["segment_id"] in by_id}
        mirrored = set(excluded)
        for e in entities:
            seg = by_id.get(e["segment_id"])
            if seg is not None and getattr(seg, "hidden", False) and (seg.text, e["start"], e["end"]) in kept_spots:
                mirrored.add(e["id"])
        return mirrored

    def redact_bytes(self, data: bytes, mode: str = "label", **analyze_kwargs) -> tuple[bytes, dict]:
        """One-shot: analyse a DOCX and redact everything detected (CLI / scripting)."""
        doc = DocxDocument.from_bytes(data)
        analysis = self.analyze(doc, **analyze_kwargs)
        out, report = self.redact(doc, analysis["entities"], mode=mode)
        report["analysis_ms"] = analysis["elapsed_ms"]
        report["warnings"] = sorted(set(report.get("warnings", [])) | set(doc.warnings))
        return out, report
