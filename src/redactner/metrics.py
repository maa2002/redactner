"""Redaction-centric and standard NER metrics on character spans (tokenizer-independent).

* character level (label-agnostic): precision/recall/F1/F5 over non-whitespace characters,
  answering "was the right text removed?";
* entity leak rate: share of gold mentions with any alphanumeric character left unredacted;
* typed entity level (strict = exact boundaries + type; also lenient overlap) micro P/R/F1, per type.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence

GoldSpan = Sequence  # (start, end, label)


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

    def __init__(self, labels: Sequence[str]) -> None:
        self.labels = list(labels)
        self.char = Counter()
        self.strict = defaultdict(Counter)
        self.lenient = Counter()
        self.mentions = 0
        self.leaked = 0
        self.leaked_by_label = Counter()
        self.mentions_by_label = Counter()
        self.docs = 0
        self.docs_zero_leak = 0

    def add(self, text: str, gold: Iterable[GoldSpan], pred: Iterable[GoldSpan]) -> None:
        gold = [tuple(g[:3]) for g in gold]
        pred = [tuple(p[:3]) for p in pred]
        g_chars, p_chars = char_mask(text, gold), char_mask(text, pred)
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
            alnum = [i for i in range(s, e) if text[i].isalnum()]
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
        return {
            "char_precision": cp, "char_recall": cr, "char_f1": fbeta(cp, cr, 1), "char_f5": fbeta(cp, cr, 5),
            "entity_precision": sp, "entity_recall": sr, "entity_f1": fbeta(sp, sr, 1),
            "lenient_precision": lp, "lenient_recall": lr, "lenient_f1": fbeta(lp, lr, 1),
            "leak_rate": self.leaked / self.mentions if self.mentions else 0.0,
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
