"""Redaction-centric and standard NER metrics on character spans (tokenizer-independent).

* character level (label-agnostic): precision/recall/F1/F5 over non-whitespace characters,
  answering "was the right text removed?";
* entity leak rate: share of gold mentions with any alphanumeric character left unredacted,
  over all categories and over direct identifiers only;
* typed entity level (strict = exact boundaries + type; also lenient overlap) micro P/R/F1, per type;
* TAB scores for the Text Anonymization Benchmark (see :class:`TabScorer`).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence

from .labels import PRESETS

GoldSpan = Sequence  # (start, end, label)
DIRECT_LABELS = tuple(PRESETS["direct"])  # categories that identify a person on their own

# Words the official TAB script does not require to be masked: the titles and abbreviations it lists, plus
# determiners, prepositions, conjunctions and particles (which it finds with a part-of-speech tagger and
# which are approximated here by a word list).
TAB_IGNORED_WORDS = frozenset("""
mr mrs ms no nr about
a an the this that these those
of in on at by for to from with into onto upon over under between among within without against during
before after since until through throughout across along around behind beyond near off out up down via per
than and or nor but yet so s not
""".split())


def tab_ignored_chars(text: str) -> set[int]:
    """Positions of the letters and digits that TAB does not require to be masked."""
    out: set[int] = set()
    for word in re.finditer(r"\w+", text):
        if word.group(0).lower() in TAB_IGNORED_WORDS:
            out.update(range(word.start(), word.end()))
    return out


def fbeta(p: float, r: float, beta: float) -> float:
    if p == 0 and r == 0:
        return 0.0
    b2 = beta * beta
    return (1 + b2) * p * r / (b2 * p + r)


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r


def char_mask(text: str, spans: Iterable[GoldSpan]) -> set[int]:
    covered = set()
    for s, e, *_ in spans:
        covered.update(i for i in range(s, e) if not text[i].isspace())
    return covered


class CorpusScorer:
    """Accumulates per-document counts and reports micro-averaged metrics."""

    def __init__(self, labels: Sequence[str], direct_labels: Iterable[str] = DIRECT_LABELS) -> None:
        self.labels = list(labels)
        self.direct_labels = set(direct_labels)
        self.char = Counter()
        self.strict = defaultdict(Counter)
        self.lenient = Counter()
        self.mentions = 0
        self.leaked = 0
        self.leaked_by_label = Counter()
        self.mentions_by_label = Counter()
        self.docs = 0
        self.docs_zero_leak = 0

    def add(self, text: str, gold: Iterable[GoldSpan], pred: Iterable[GoldSpan], ignore: set[int] = frozenset()) -> None:
        """Score one document; characters in ``ignore`` count neither for nor against a system."""
        gold = [tuple(g[:3]) for g in gold]
        pred = [tuple(p[:3]) for p in pred]
        g_chars, p_chars = char_mask(text, gold) - ignore, char_mask(text, pred) - ignore
        self.char["tp"] += len(g_chars & p_chars)
        self.char["fp"] += len(p_chars - g_chars)
        self.char["fn"] += len(g_chars - p_chars)

        # typed strict match
        g_set, p_set = set(gold), set(pred)
        for lab in self.labels:
            g_l = {x for x in g_set if x[2] == lab}
            p_l = {x for x in p_set if x[2] == lab}
            self.strict[lab]["tp"] += len(g_l & p_l)
            self.strict[lab]["fp"] += len(p_l - g_l)
            self.strict[lab]["fn"] += len(g_l - p_l)

        # untyped lenient match (any overlap)
        matched_pred = set()
        for g in gold:
            hit = [i for i, p in enumerate(pred) if p[0] < g[1] and g[0] < p[1]]
            if hit:
                self.lenient["tp"] += 1
                matched_pred.update(hit)
            else:
                self.lenient["fn"] += 1
        self.lenient["fp"] += len(pred) - len(matched_pred)

        # leak rate: a mention leaks if any alphanumeric character stays visible
        doc_leaks = 0
        for s, e, lab in gold:
            alnum = [i for i in range(s, e) if text[i].isalnum() and i not in ignore]
            self.mentions += 1
            self.mentions_by_label[lab] += 1
            if any(i not in p_chars for i in alnum):
                self.leaked += 1
                self.leaked_by_label[lab] += 1
                doc_leaks += 1
        self.docs += 1
        self.docs_zero_leak += doc_leaks == 0

    def summary(self) -> dict:
        cp, cr = _prf(self.char["tp"], self.char["fp"], self.char["fn"])
        st = Counter()
        for lab in self.labels:
            st.update(self.strict[lab])
        sp, sr = _prf(st["tp"], st["fp"], st["fn"])
        lp, lr = _prf(self.lenient["tp"], self.lenient["fp"], self.lenient["fn"])
        direct = sum(self.mentions_by_label[lab] for lab in self.direct_labels)
        direct_leaked = sum(self.leaked_by_label[lab] for lab in self.direct_labels)
        return {
            "char_precision": cp, "char_recall": cr, "char_f1": fbeta(cp, cr, 1), "char_f5": fbeta(cp, cr, 5),
            "entity_precision": sp, "entity_recall": sr, "entity_f1": fbeta(sp, sr, 1),
            "lenient_precision": lp, "lenient_recall": lr, "lenient_f1": fbeta(lp, lr, 1),
            "leak_rate": self.leaked / self.mentions if self.mentions else 0.0,
            "direct_leak_rate": direct_leaked / direct if direct else 0.0,
            "doc_zero_leak_rate": self.docs_zero_leak / self.docs if self.docs else 0.0,
            "mentions": self.mentions, "docs": self.docs,
        }

    def per_label(self) -> dict[str, dict]:
        out = {}
        for lab in self.labels:
            c = self.strict[lab]
            p, r = _prf(c["tp"], c["fp"], c["fn"])
            out[lab] = {"precision": p, "recall": r, "f1": fbeta(p, r, 1), "support": c["tp"] + c["fn"],
                        "leak_rate": (self.leaked_by_label[lab] / self.mentions_by_label[lab]
                                      if self.mentions_by_label[lab] else None)}
        return out


class TabScorer:
    """Text Anonymization Benchmark (TAB) scores, micro-averaged over annotators as in the official script.

    * **Entity-level recall** for direct (``er_direct``) and quasi-identifiers (``er_quasi``): an entity
      counts as protected only when every one of its mentions marked for masking is fully masked.
    * **Mention leak rates** for direct and quasi-identifier mentions.
    * **Word-level precision** with uniform weights: each masked word scores the share of annotators who
      also masked it.

    As in the official script, punctuation, titles such as *Mr* and function words such as *the* or *of* do
    not have to be masked (pass ``ignore=tab_ignored_chars(text)``). ``annotations`` maps each annotator to
    mentions given as ``(start, end, label, identifier_type, entity_id)`` with ``identifier_type`` ``DIRECT``
    or ``QUASI``.
    """

    def __init__(self) -> None:
        self.entities = Counter()
        self.protected = Counter()
        self.mentions = Counter()
        self.leaked = Counter()
        self.word_credit = 0.0
        self.masked_words = 0

    def add(self, text: str, annotations: Mapping[str, Sequence], pred: Iterable[GoldSpan],
            ignore: set[int] = frozenset()) -> None:
        pred = [tuple(p[:3]) for p in pred]
        masked = char_mask(text, pred)
        coverage = []
        for mentions in annotations.values():
            entities: dict[str, list[bool]] = defaultdict(lambda: [False, True])  # [is direct, all masked]
            covered: set[int] = set()
            for start, end, _label, kind, entity_id in mentions:
                ok = all(i in masked for i in range(start, end) if text[i].isalnum() and i not in ignore)
                group = "direct" if kind == "DIRECT" else "quasi"
                self.mentions[group] += 1
                self.leaked[group] += not ok
                entities[entity_id][0] |= kind == "DIRECT"
                entities[entity_id][1] &= ok
                covered.update(range(start, end))
            for is_direct, all_masked in entities.values():
                group = "direct" if is_direct else "quasi"
                self.entities[group] += 1
                self.protected[group] += all_masked
            coverage.append(covered)
        if not coverage:
            return
        for start, end, *_ in pred:
            for word in re.finditer(r"\w+", text[start:end]):
                ws, we = start + word.start(), start + word.end()
                agree = sum(all(i in covered for i in range(ws, we)) for covered in coverage)
                self.word_credit += agree / len(coverage)
                self.masked_words += 1

    def summary(self) -> dict:
        def ratio(a: float, b: float) -> float:
            return a / b if b else 0.0

        return {"er_direct": ratio(self.protected["direct"], self.entities["direct"]),
                "er_quasi": ratio(self.protected["quasi"], self.entities["quasi"]),
                "leak_direct": ratio(self.leaked["direct"], self.mentions["direct"]),
                "leak_quasi": ratio(self.leaked["quasi"], self.mentions["quasi"]),
                "word_precision": ratio(self.word_credit, self.masked_words),
                "entities_direct": self.entities["direct"], "entities_quasi": self.entities["quasi"]}
