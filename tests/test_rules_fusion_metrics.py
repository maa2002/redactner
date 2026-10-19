"""Unit tests for pattern recognisers, fusion and the character-level metrics."""

from redactner.fusion import HybridDetector, apply_policy, merge_adjacent, propagate, repair_boundaries, resolve_overlaps
from redactner.labels import LABELS
from redactner.metrics import CorpusScorer, fbeta
from redactner.operators import MASK, ReplacementBuilder
from redactner.rules import RuleDetector, medicare_valid, term_spans
from redactner.types import Span

TEXT = ("Dear Ms Priya Raman, your Medicare card 2123 45670 1 and TFN 123 456 782 were updated. "
        "Call 0412 345 678, email priya.raman@example.com.au or visit https://linkedin.com/in/praman. "
        "Card 4111 1111 1111 1111, IBAN GB82 WEST 1234 5698 7654 32, IP 192.168.10.24, BSB 062-000.")


def found(spans, text=TEXT):
    return {(text[s.start:s.end], s.label) for s in spans}


def test_rules_find_validated_identifiers():
    got = found(RuleDetector().detect(TEXT))
    for expected in [("2123 45670 1", "ID_NUMBER"), ("123 456 782", "ID_NUMBER"), ("0412 345 678", "PHONE"),
                     ("priya.raman@example.com.au", "EMAIL"), ("https://linkedin.com/in/praman", "URL"),
                     ("4111 1111 1111 1111", "FINANCIAL"), ("GB82 WEST 1234 5698 7654 32", "FINANCIAL"),
                     ("192.168.10.24", "IP_ADDRESS"), ("062-000", "FINANCIAL")]:
        assert expected in got, expected


def test_checksums_reject_invalid_numbers():
    assert medicare_valid("2123 45670 1")
    assert not medicare_valid("2123 45671 1")
    bad = "TFN 123 456 789 and card 4111 1111 1111 1112"
    assert not [s for s in RuleDetector().detect(bad) if s.label in ("ID_NUMBER", "FINANCIAL")]


def test_no_false_positives_on_plain_numbers():
    text = "Invoice INV-20931 for 3 May 2024 totals $1,250.00 (clause 12.3)."
    assert not [s for s in RuleDetector().detect(text) if s.label in ("FINANCIAL", "ID_NUMBER", "PHONE")]


def test_overlap_resolution_prefers_validated_rule_and_keeps_union():
    spans = [Span(0, 12, "PHONE", 0.97, "rule"), Span(0, 12, "ID_NUMBER", 1.0, "rule"), Span(5, 15, "ID_NUMBER", 0.6, "model")]
    out = resolve_overlaps(spans)
    assert len(out) == 1 and out[0].label == "ID_NUMBER" and (out[0].start, out[0].end) == (0, 15)


def test_boundary_repair_extends_partial_words():
    text = "Contact Smithson today"
    sp = repair_boundaries(text, Span(8, 12, "PERSON"))
    assert text[sp.start:sp.end] == "Smithson"


def test_merge_adjacent_names():
    text = "John Smith signed"
    out = merge_adjacent(text, [Span(0, 4, "PERSON"), Span(5, 10, "PERSON")])
    assert [(s.start, s.end) for s in out] == [(0, 10)]


def test_propagation_redacts_other_mentions_and_components():
    text = "Priya Raman wrote this. Later, Ms Raman called. raman is lowercase here."
    added = propagate(text, [Span(0, 11, "PERSON", 0.95)])
    got = {text[s.start:s.end] for s in added}
    assert "Raman" in got
    assert all(text[s.start].isupper() for s in added)  # capitalised components only


def test_hybrid_rules_only_and_policy():
    det = HybridDetector(None)
    spans = det.detect(TEXT, deny_terms=["Priya Raman"])
    labels = {s.label for s in spans}
    assert "CUSTOM" in labels and "EMAIL" in labels
    only_email = apply_policy(TEXT, spans, labels=["EMAIL"])
    assert {s.label for s in only_email} <= {"EMAIL", "CUSTOM"}
    allowed = apply_policy(TEXT, spans, labels=None, allow_terms=["priya.raman@example.com.au"])
    assert ("priya.raman@example.com.au", "EMAIL") not in found(allowed)


class FakeModel:
    """Stands in for the neural detector: returns fixed spans."""

    def __init__(self, spans):
        self.spans = spans

    def detect(self, text, threshold=None):
        return list(self.spans)


def test_hybrid_drops_bare_field_labels_from_model():
    text = "TFN\t232 322 182"
    det = HybridDetector(FakeModel([Span(0, 3, "ID_NUMBER", 0.9, "model"), Span(4, 15, "ID_NUMBER", 0.9, "model")]))
    assert found(det.detect(text), text) == {("232 322 182", "ID_NUMBER")}


def test_term_spans_word_bounded():
    text = "Project Phoenix and phoenixes"
    assert found(term_spans(text, ["phoenix"]), text) == {("Phoenix", "CUSTOM")}


def test_metrics_perfect_and_leak():
    text = "Jane Doe lives at 1 Main St"
    gold = [(0, 8, "PERSON"), (18, 27, "ADDRESS")]
    sc = CorpusScorer(LABELS)
    sc.add(text, gold, gold)
    s = sc.summary()
    assert s["char_f1"] == 1.0 and s["entity_f1"] == 1.0 and s["leak_rate"] == 0.0
    sc2 = CorpusScorer(LABELS)
    sc2.add(text, gold, [(0, 4, "PERSON")])  # surname left visible
    s2 = sc2.summary()
    assert s2["leak_rate"] == 1.0 and s2["char_precision"] == 1.0 and s2["entity_recall"] == 0.0
    assert abs(fbeta(1.0, 0.5, 1) - 2 / 3) < 1e-9


def test_replacement_builder_consistent_numbering():
    rb = ReplacementBuilder("label")
    assert rb("PERSON", "John Smith") == rb("PERSON", "john  smith") == "[PERSON_1]"
    assert rb("PERSON", "Jane") == "[PERSON_2]"
    assert rb("PHONE", "0412 345 678") == rb("PHONE", "0412345678") == "[PHONE_1]"
    assert ReplacementBuilder("mask")("PERSON", "X") == MASK
