"""Decision-threshold tuning on cached token probabilities (decision rule M4).

For fixed token probabilities, the characters that a threshold redacts are exactly the non-space
characters of the tokens it flags. Character-level scores and leak rates for any global or
per-category threshold can therefore be computed with array operations, instead of decoding every
development document again for every candidate threshold. (Decoding also extends spans that end
inside a word to the whole word, so final scores can be slightly higher than these estimates.)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .inference import TokenProbs, flag_tokens
from .metrics import DIRECT_LABELS, fbeta


@dataclass
class TokenTable:
    """Token-level view of a scored corpus: probabilities and gold-character counts for every token."""

    types: list[str]
    p_o: np.ndarray            # P(O) per token
    type_probs: np.ndarray     # probability of each PII type per token (B and I summed), columns in ``types`` order
    argmax_o: np.ndarray       # True where "O" is the single most likely label
    gold_type: np.ndarray      # category of the gold span covering the token (index into ``types``), -1 if none
    gold: np.ndarray           # non-space characters of the token inside gold spans
    other: np.ndarray          # non-space characters of the token outside gold spans
    gold_total: int            # all non-space gold characters
    mention_tokens: np.ndarray  # token indices covering the letters and digits of each scorable mention
    mention_starts: np.ndarray  # start of each scorable mention within ``mention_tokens``
    mention_direct: np.ndarray  # whether each scorable mention is a direct identifier
    n_mentions: int            # all gold mentions (those without letters or digits can never leak)
    n_direct: int
    always_leaked: int         # mentions with characters no token covers
    always_leaked_direct: int

    @classmethod
    def build(cls, detector, docs: Sequence[dict], token_probs: Sequence[TokenProbs]) -> TokenTable:
        """``detector`` supplies the label layout (``types``, ``type_cols``, ``o_index``)."""
        p_o, type_probs_all, argmax_o, gold, other, gold_type = [], [], [], [], [], []
        type_index = {t: k for k, t in enumerate(detector.types)}
        m_tokens, m_starts, m_direct = [], [], []
        gold_total = n_mentions = n_direct = always = always_direct = 0
        base = n_mention_tokens = 0
        for doc, tp in zip(docs, token_probs):
            text, n = doc["text"], len(tp.offsets)
            nonspace = np.fromiter((not c.isspace() for c in text), bool, len(text))
            is_gold = np.zeros(len(text), bool)
            char_type = np.full(len(text), -1)
            for s, e, label, *_ in doc["spans"]:
                is_gold[s:e] = True
                char_type[s:e] = type_index.get(label, -1)
            gold_total += int((is_gold & nonspace).sum())
            cum_gold = np.concatenate([[0], np.cumsum(is_gold & nonspace)])
            cum_other = np.concatenate([[0], np.cumsum(~is_gold & nonspace)])
            char_token = np.full(len(text), -1)
            if n:
                starts, ends = tp.offsets[:, 0], tp.offsets[:, 1]
                gold.append(cum_gold[ends] - cum_gold[starts])
                other.append(cum_other[ends] - cum_other[starts])
                type_probs_all.append(np.stack([tp.probs[:, cols].sum(axis=1) for cols in detector.type_cols],
                                               axis=1).astype(np.float32))
                p_o.append(tp.probs[:, detector.o_index])
                argmax_o.append(tp.probs.argmax(axis=1) == detector.o_index)
                gold_type.append(np.array([max(char_type[s:e], default=-1) for s, e in tp.offsets]))
                for t, (s, e) in enumerate(tp.offsets):
                    char_token[s:e] = base + t
            for s, e, label, *_ in doc["spans"]:
                n_mentions += 1
                direct = label in DIRECT_LABELS
                n_direct += direct
                idx = char_token[[i for i in range(s, e) if text[i].isalnum()]]
                if len(idx) == 0:
                    continue
                if (idx < 0).any():
                    always += 1
                    always_direct += direct
                    continue
                idx = np.unique(idx)
                m_starts.append(n_mention_tokens)
                m_tokens.append(idx)
                m_direct.append(direct)
                n_mention_tokens += len(idx)
            base += n

        def cat(parts, dtype):
            return np.concatenate(parts).astype(dtype) if parts else np.zeros(0, dtype)

        n_types = len(detector.types)
        return cls(types=list(detector.types), p_o=cat(p_o, float),
                   type_probs=np.concatenate(type_probs_all) if type_probs_all else np.zeros((0, n_types), np.float32),
                   argmax_o=cat(argmax_o, bool), gold_type=cat(gold_type, int), gold=cat(gold, int),
                   other=cat(other, int), gold_total=gold_total,
                   mention_tokens=cat(m_tokens, int), mention_starts=np.array(m_starts, int),
                   mention_direct=np.array(m_direct, bool), n_mentions=n_mentions, n_direct=n_direct,
                   always_leaked=always, always_leaked_direct=always_direct)

    # -- flags and scores ------------------------------------------------------------------------
    def flags(self, threshold: float | Mapping[str, float] | None = None, argmax: bool = False,
              default: float = 0.5) -> np.ndarray:
        """Tokens flagged as PII by a global threshold, a per-category mapping, or argmax decoding."""
        if argmax:
            return ~self.argmax_o
        if isinstance(threshold, Mapping):
            tau = np.array([float(threshold.get(t, default)) for t in self.types])
        else:
            tau = np.full(len(self.types), default if threshold is None else float(threshold))
        return flag_tokens(self.p_o, self.type_probs, tau)

    def cost(self, flagged: np.ndarray, miss_costs: Mapping[str, float]) -> float:
        """Missed gold characters weighted by their category's cost, plus one per unnecessarily redacted character."""
        per_type = np.array([float(miss_costs.get(t, 1.0)) for t in self.types] + [1.0])  # last entry: gold_type -1
        missed = (self.gold * per_type[self.gold_type])[~flagged].sum()
        return float(missed + self.other[flagged].sum())

    def scores(self, flagged: np.ndarray, beta: float = 5.0) -> dict:
        tp = int(self.gold[flagged].sum())
        fp = int(self.other[flagged].sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / self.gold_total if self.gold_total else 0.0
        if len(self.mention_starts):
            covered = np.minimum.reduceat(flagged[self.mention_tokens].astype(np.int8), self.mention_starts) == 1
        else:
            covered = np.zeros(0, bool)
        leaked = int((~covered).sum()) + self.always_leaked
        leaked_direct = int((~covered & self.mention_direct).sum()) + self.always_leaked_direct
        return {"char_precision": precision, "char_recall": recall, "char_f1": fbeta(precision, recall, 1),
                f"char_f{beta:g}": fbeta(precision, recall, beta),
                "leak_rate": leaked / self.n_mentions if self.n_mentions else 0.0,
                "direct_leak_rate": leaked_direct / self.n_direct if self.n_direct else 0.0}


def tune_global(table: TokenTable, grid: Sequence[float], min_precision: float, beta: float = 5.0):
    """Threshold with the highest F-beta among those meeting the precision floor (ties → higher recall)."""
    rows = [{"tau": tau, **table.scores(table.flags(tau), beta)} for tau in grid]
    key = f"char_f{beta:g}"
    ok = [r for r in rows if r["char_precision"] >= min_precision] or rows
    best = max(ok, key=lambda r: (round(r[key], 6), r["char_recall"]))
    return best["tau"], rows


def tune_per_category(table: TokenTable, grid: Sequence[float], miss_costs: Mapping[str, float], base_tau: float,
                      relax: Sequence[str], passes: int = 2) -> dict[str, float]:
    """Relax the thresholds of the categories in ``relax`` to minimise the expected cost on the development set.

    A missed character of category ``c`` costs ``miss_costs[c]`` and an unnecessary redaction costs 1 (so a
    cost of beta squared matches the weighting of an F-beta score). The other categories keep ``base_tau``;
    each relaxed threshold is searched over the grid values up to ``base_tau``, one category at a time.
    """
    tau = {t: float(base_tau) for t in table.types}
    candidates = [g for g in grid if g <= base_tau]
    for _ in range(passes):
        for label in relax:
            costs = [(table.cost(table.flags({**tau, label: g}), miss_costs), -g) for g in candidates]
            tau[label] = -min(costs)[1]  # lowest cost; ties go to the stricter (higher) threshold
    return tau
