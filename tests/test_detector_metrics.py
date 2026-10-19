"""Unit tests for span decoding, the user's term lists and the evaluation metrics."""

import numpy as np
from conftest import StubModel

from redactner.detector import PIIDetector, apply_policy, resolve_overlaps, term_spans
from redactner.inference import TokenClassifierDetector, TokenProbs, snap_to_words
from redactner.labels import LABELS
from redactner.metrics import CorpusScorer, TabScorer, fbeta, tab_ignored_chars
from redactner.operators import MASK, ReplacementBuilder
from redactner.types import Span


def found(spans, text):
    return {(text[s.start:s.end], s.label) for s in spans}


def tiny_decoder(types=("DATE", "PERSON")) -> TokenClassifierDetector:
    """A decoder with the label layout of a real checkpoint but no weights."""
    det = object.__new__(TokenClassifierDetector)
    id2label = {0: "O"}
    for t in types:
        id2label[len(id2label)] = f"B-{t}"
        id2label[len(id2label)] = f"I-{t}"
    det.id2label, det.o_index, det.types = id2label, 0, sorted(types)
    det.type_cols = [[i for i, lab in id2label.items() if lab != "O" and lab[2:] == t] for t in det.types]
    det.b_cols = {t: next(i for i, lab in id2label.items() if lab == f"B-{t}") for t in det.types}
    det.i_cols = {t: next(i for i, lab in id2label.items() if lab == f"I-{t}") for t in det.types}
    det.threshold, det.category_thresholds, det.decoding = 0.5, {}, "threshold"
    return det


TEXT = "Jane Smith on 3 May"
# columns: O, B-DATE, I-DATE, B-PERSON, I-PERSON
PROBS = TokenProbs(np.array([[0, 4], [5, 10], [11, 13], [14, 15], [16, 19]]),
                   np.array([[0.3, 0.0, 0.0, 0.7, 0.0],    # Jane
                             [0.3, 0.0, 0.0, 0.0, 0.7],    # Smith
                             [0.99, 0.0, 0.0, 0.0, 0.01],  # on
                             [0.6, 0.4, 0.0, 0.0, 0.0],    # 3
                             [0.6, 0.0, 0.4, 0.0, 0.0]]))  # May


def test_global_threshold_decoding():
    assert found(tiny_decoder().decode(TEXT, PROBS), TEXT) == {("Jane Smith", "PERSON")}


def test_per_category_thresholds_and_explicit_override():
    det = tiny_decoder()
    both = {("Jane Smith", "PERSON"), ("3 May", "DATE")}
    assert found(det.decode(TEXT, PROBS, {"DATE": 0.7}), TEXT) == both
    det.category_thresholds = {"DATE": 0.7}
    assert found(det.decode(TEXT, PROBS), TEXT) == both
    assert found(det.decode(TEXT, PROBS, 0.5), TEXT) == {("Jane Smith", "PERSON")}  # explicit global value wins


def test_word_snapping_and_edge_punctuation():
    text = "Contact Smithson, today"
    assert found(snap_to_words(text, [Span(8, 12, "PERSON")]), text) == {("Smithson", "PERSON")}
    assert found(snap_to_words(text, [Span(8, 17, "PERSON")]), text) == {("Smithson", "PERSON")}
    assert found(snap_to_words("He left. Then", [Span(7, 8, "USERNAME")]), "He left. Then") == set()
    merged = snap_to_words("Smithson", [Span(0, 3, "PERSON", 0.9), Span(3, 8, "PERSON", 0.6)])
    assert [(s.start, s.end) for s in merged] == [(0, 8)]


def test_term_spans_word_bounded():
    text = "Project Phoenix and phoenixes"
    assert found(term_spans(text, ["phoenix"]), text) == {("Phoenix", "CUSTOM")}


def test_user_terms_outrank_the_model_and_keep_the_union():
    out = resolve_overlaps([Span(0, 12, "PERSON", 0.9, "model"), Span(0, 7, "CUSTOM", 1.0, "deny")])
    assert len(out) == 1 and out[0].label == "CUSTOM" and (out[0].start, out[0].end) == (0, 12)


def test_detector_and_policy():
    text = "Priya Raman (priya@example.com) works at UTS."
    det = PIIDetector(StubModel({"Priya Raman": "PERSON", "priya@example.com": "EMAIL", "UTS": "ORGANIZATION"}))
    spans = det.detect(text, deny_terms=["works"])
    assert ("works", "CUSTOM") in found(spans, text)
    only_email = apply_policy(text, spans, labels=["EMAIL"])
    assert {s.label for s in only_email} == {"EMAIL", "CUSTOM"}
    allowed = apply_policy(text, spans, labels=None, allow_terms=["uts"])
    assert ("UTS", "ORGANIZATION") not in found(allowed, text)


def test_metrics_perfect_and_leak():
    text = "Jane Doe lives at 1 Main St"
    gold = [(0, 8, "PERSON"), (18, 27, "ADDRESS")]
    sc = CorpusScorer(LABELS)
    sc.add(text, gold, gold)
    s = sc.summary()
    assert s["char_f1"] == 1.0 and s["entity_f1"] == 1.0 and s["leak_rate"] == 0.0
    sc2 = CorpusScorer(LABELS)
    sc2.add(text, gold, [(0, 4, "PERSON")])  # surname and address left visible
    s2 = sc2.summary()
    assert s2["leak_rate"] == 1.0 and s2["char_precision"] == 1.0 and s2["entity_recall"] == 0.0
    assert abs(fbeta(1.0, 0.5, 1) - 2 / 3) < 1e-9


def test_direct_identifier_leak_rate_ignores_quasi_identifiers():
    text = "Jane Doe met us on 3 May"
    sc = CorpusScorer(LABELS)
    sc.add(text, [(0, 8, "PERSON"), (19, 24, "DATE")], [(0, 8, "PERSON")])
    s = sc.summary()
    assert s["leak_rate"] == 0.5 and s["direct_leak_rate"] == 0.0


def test_tab_scores_entity_recall_and_word_precision():
    text = "Mr John Smith was born in Oslo in 1970. Smith appealed."
    annotations = {
        "a1": [(3, 13, "PERSON", "DIRECT", "e1"), (40, 45, "PERSON", "DIRECT", "e1"), (26, 30, "LOCATION", "QUASI", "e2")],
        "a2": [(3, 13, "PERSON", "DIRECT", "f1"), (40, 45, "PERSON", "DIRECT", "f1")],
    }
    sc = TabScorer()
    sc.add(text, annotations, [(3, 13, "PERSON"), (26, 30, "LOCATION")])  # second "Smith" left visible
    s = sc.summary()
    assert s["er_direct"] == 0.0 and s["er_quasi"] == 1.0
    assert s["leak_direct"] == 0.5 and s["leak_quasi"] == 0.0
    # John and Smith masked by both annotators, Oslo by one of two
    assert abs(s["word_precision"] - (1 + 1 + 0.5) / 3) < 1e-9


def test_replacement_builder_consistent_numbering():
    rb = ReplacementBuilder("label")
    assert rb("PERSON", "John Smith") == rb("PERSON", "john  smith") == "[PERSON_1]"
    assert rb("PERSON", "Jane") == "[PERSON_2]"
    assert rb("PHONE", "0412 345 678") == rb("PHONE", "0412345678") == "[PHONE_1]"
    assert ReplacementBuilder("mask")("PERSON", "X") == MASK


def test_token_table_matches_decoded_scores_and_tunes_per_category():
    from redactner.thresholds import TokenTable, tune_global, tune_per_category

    det = tiny_decoder()
    docs = [{"text": TEXT, "spans": [[0, 10, "PERSON"], [14, 19, "DATE"]]}]
    table = TokenTable.build(det, docs, [PROBS])
    for tau in (0.5, 0.7):
        fast = table.scores(table.flags(tau))
        sc = CorpusScorer(LABELS)
        sc.add(TEXT, docs[0]["spans"], [(s.start, s.end, s.label) for s in det.decode(TEXT, PROBS, tau)])
        full = sc.summary()
        for key in ("char_precision", "char_recall", "leak_rate", "direct_leak_rate"):
            assert abs(fast[key] - full[key]) < 1e-9, (tau, key)
    assert table.scores(table.flags(0.5))["direct_leak_rate"] == 0.0
    assert tune_global(table, [0.5, 0.7], min_precision=0.8)[0] == 0.7
    tuned = tune_per_category(table, [0.5, 0.7], {"DATE": 4, "PERSON": 25}, base_tau=0.7, relax=["DATE"])
    assert tuned == {"DATE": 0.7, "PERSON": 0.7}  # relaxing DATE to 0.5 would leak "3 May"


def test_relaxed_category_cannot_hide_a_likely_strict_one():
    from redactner.inference import flag_tokens

    # P(O) = 0.85, P(DATE) = 0.10, P(PERSON) = 0.05: mostly "date-like", but a name is plausible
    p_o, type_probs = np.array([0.85]), np.array([[0.10, 0.05]])  # columns: DATE, PERSON
    assert flag_tokens(p_o, type_probs, np.array([0.995, 0.995]))[0]   # one strict threshold: redacted
    assert flag_tokens(p_o, type_probs, np.array([0.8, 0.995]))[0]     # DATE relaxed: still redacted
    assert not flag_tokens(p_o, np.array([[0.15, 0.0]]), np.array([0.8, 0.995]))[0]  # a pure date is not


def test_tab_ignores_titles_and_function_words():
    text = "Mr John Smith of the Ministry of Justice"
    ignore = tab_ignored_chars(text)
    assert text[0:2] == "Mr" and {0, 1} <= ignore and text.index("Justice") not in ignore
    annotations = {"a1": [(0, 13, "PERSON", "DIRECT", "e1"), (17, 40, "ORGANIZATION", "QUASI", "e2")]}
    pred = [(3, 13, "PERSON"), (21, 29, "ORGANIZATION"), (33, 40, "ORGANIZATION")]  # titles and "of" visible
    strict, lenient = TabScorer(), TabScorer()
    strict.add(text, annotations, pred)
    lenient.add(text, annotations, pred, ignore=ignore)
    assert strict.summary()["er_direct"] == 0.0 and lenient.summary()["er_direct"] == 1.0
    assert lenient.summary()["er_quasi"] == 1.0
    sc = CorpusScorer(LABELS)
    sc.add(text, [m[:3] for m in annotations["a1"]], pred, ignore=ignore)
    assert sc.summary()["leak_rate"] == 0.0
