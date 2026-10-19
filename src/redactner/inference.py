"""Neural PII detector: a fine-tuned token classifier applied with document-scale sliding windows.

* **Sliding windows (M3):** the whole document is tokenised once into overlapping windows;
  probabilities of tokens seen by several windows are averaged (tokens are keyed by character
  offsets, which are identical across windows).
* **Cost-sensitive decoding (M4):** with one threshold, a token is PII when ``P(O) < tau``. With a
  threshold per category, each category ``c`` gets its own minimum confidence ``1 - tau_c`` and a
  token is PII when ``sum_c P(c) / (1 - tau_c) > 1``: the Bayes-optimal decision when missing a
  category costs ``tau_c / (1 - tau_c)`` times as much as an unnecessary redaction. It reduces to
  ``P(O) < tau`` when all thresholds are equal, and a relaxed category can never hide a token that
  is likely to belong to a strict one. The span type is the argmax of the type probabilities
  averaged over the span (span-level voting), so one name cannot split into PERSON + USERNAME.
  ``decoding="argmax"`` gives the plain baseline behaviour.
* **Word-level aggregation:** a word is redacted as a whole when any of its sub-word tokens is
  flagged, so no fragment of a name ("...ith" of "Smith") is left behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModelForTokenClassification, AutoTokenizer

from .types import Span
from .weights import join_split_files

META_FILE = "redactner_meta.json"
EDGE_PUNCT = ",;:"


def default_device() -> str:
    return "cuda" if torch.cuda.is_available() and torch.cuda.device_count() > 0 else "cpu"


@dataclass
class TokenProbs:
    offsets: np.ndarray  # (n_tokens, 2) character offsets, sorted by start
    probs: np.ndarray  # (n_tokens, n_labels)


class TokenClassifierDetector:
    """Wraps a Hugging Face token-classification checkpoint as a PII :class:`Span` detector."""

    def __init__(self, model_dir: str | Path, *, device: str | None = None, threshold: float | None = None,
                 decoding: str = "threshold", max_len: int | None = None, stride: int | None = None,
                 batch_size: int = 8) -> None:
        self.model_dir = Path(model_dir)
        meta_path = self.model_dir / META_FILE
        self.meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        self.device = torch.device(device or os.environ.get("REDACTNER_DEVICE") or default_device())
        join_split_files(self.model_dir)  # large weight files are stored in parts in the repository
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
        self.model = AutoModelForTokenClassification.from_pretrained(self.model_dir).to(self.device).eval()
        self.threshold = threshold if threshold is not None else float(self.meta.get("threshold", 0.5))
        # optional per-category thresholds tuned on the development set (used unless a threshold is given)
        self.category_thresholds = {k: float(v) for k, v in (self.meta.get("category_thresholds") or {}).items()}
        self.decoding = decoding
        self.max_len = max_len or int(self.meta.get("max_len", 512))
        self.stride = stride or int(self.meta.get("stride", 128))
        self.batch_size = batch_size
        base_model = self.meta.get("model_name")  # e.g. "answerdotai/ModernBERT-large" -> "ModernBERT-large"
        self.name = base_model.split("/")[-1] if base_model else self.meta.get("run_name", self.model_dir.name)

        id2label = {int(k): v for k, v in self.model.config.id2label.items()}
        self.o_index = next(i for i, lab in id2label.items() if lab == "O")
        self.types = sorted({lab[2:] for lab in id2label.values() if lab != "O"})
        self.type_cols = [[i for i, lab in id2label.items() if lab != "O" and lab[2:] == t] for t in self.types]
        self.b_cols = {t: next(i for i, lab in id2label.items() if lab == f"B-{t}") for t in self.types}
        self.i_cols = {t: next(i for i, lab in id2label.items() if lab == f"I-{t}") for t in self.types}
        self.id2label = id2label

    # -- probabilities -----------------------------------------------------------------------
    @torch.inference_mode()
    def token_probs(self, text: str) -> TokenProbs:
        enc = self.tokenizer(text, truncation=True, max_length=self.max_len, stride=self.stride,
                             return_overflowing_tokens=True, return_offsets_mapping=True)
        n_windows = len(enc["input_ids"])
        sums: dict[tuple[int, int], np.ndarray] = {}
        counts: dict[tuple[int, int], int] = {}
        for b in range(0, n_windows, self.batch_size):
            batch = self.tokenizer.pad({"input_ids": enc["input_ids"][b:b + self.batch_size],
                                        "attention_mask": enc["attention_mask"][b:b + self.batch_size]},
                                       return_tensors="pt")
            batch = {k: v.to(self.device) for k, v in batch.items()}
            amp = (torch.autocast("cuda", dtype=torch.bfloat16) if self.device.type == "cuda"
                   else contextlib.nullcontext())
            with amp:
                logits = self.model(**batch).logits
            probs = torch.softmax(logits.float(), dim=-1).cpu().numpy()
            for w in range(probs.shape[0]):
                for t, (s, e) in enumerate(enc["offset_mapping"][b + w]):
                    if s == e:
                        continue
                    key = (s, e)
                    if key in sums:
                        sums[key] += probs[w, t]
                        counts[key] += 1
                    else:
                        sums[key] = probs[w, t].copy()
                        counts[key] = 1
        if not sums:
            return TokenProbs(np.zeros((0, 2), dtype=int), np.zeros((0, len(self.id2label))))
        keys = sorted(sums)
        return TokenProbs(np.array(keys, dtype=int), np.stack([sums[k] / counts[k] for k in keys]))

    # -- decoding ----------------------------------------------------------------------------
    def detect(self, text: str, threshold: float | None = None) -> list[Span]:
        if not text.strip():
            return []
        tp = self.token_probs(text)
        return self.decode(text, tp, threshold)

    def thresholds_for(self, threshold: float | dict[str, float] | None = None) -> np.ndarray:
        """Decision threshold per type: an explicit value (or mapping) overrides the stored settings;
        otherwise the per-category thresholds are used, falling back to the global threshold."""
        if isinstance(threshold, dict):
            return np.array([float(threshold.get(t, self.threshold)) for t in self.types])
        if threshold is not None:
            return np.full(len(self.types), float(threshold))
        return np.array([self.category_thresholds.get(t, self.threshold) for t in self.types])

    def decode(self, text: str, tp: TokenProbs, threshold: float | dict[str, float] | None = None) -> list[Span]:
        if len(tp.offsets) == 0:
            return []
        type_probs = np.stack([tp.probs[:, cols].sum(axis=1) for cols in self.type_cols], axis=1)
        type_idx = type_probs.argmax(axis=1)
        p_o = tp.probs[:, self.o_index]
        if self.decoding == "argmax":
            flagged = tp.probs.argmax(axis=1) != self.o_index
        else:
            flagged = flag_tokens(p_o, type_probs, self.thresholds_for(threshold))

        spans: list[Span] = []
        group: list[int] = []

        def close() -> None:
            if not group:
                return
            s = int(tp.offsets[group[0], 0])
            e = int(tp.offsets[group[-1], 1])
            while s < e and text[s].isspace():
                s += 1
            while e > s and text[e - 1].isspace():
                e -= 1
            if e > s:
                t = int(type_probs[group].mean(axis=0).argmax())
                score = float((1.0 - p_o[group]).mean())
                spans.append(Span(s, e, self.types[t], round(score, 4), "model"))
            group.clear()

        # first non-space character of each token (SentencePiece offsets include the leading space)
        ns_start = [int(s) + len(text[s:e]) - len(text[s:e].lstrip()) for s, e in tp.offsets]
        prev_type = None
        for i in np.flatnonzero(flagged):
            t = self.types[int(type_idx[i])]
            if group:
                gap = text[tp.offsets[group[-1], 1]:ns_start[i]]
                contiguous = ns_start[i] >= int(tp.offsets[group[-1], 1]) and gap.strip() == ""
                new_entity = (not contiguous or "\n" in gap or "\t" in gap or t != prev_type
                              or (gap and tp.probs[i, self.b_cols[t]] > tp.probs[i, self.i_cols[t]]))
                if new_entity:
                    close()
            group.append(int(i))
            prev_type = t
        close()
        return snap_to_words(text, spans)


def flag_tokens(p_o: np.ndarray, type_probs: np.ndarray, tau: np.ndarray) -> np.ndarray:
    """Cost-sensitive decision rule: ``P(O) < tau`` for one threshold, ``sum_c P(c) / (1 - tau_c) > 1`` otherwise."""
    if np.all(tau == tau[0]):
        return p_o < tau[0]
    return (type_probs / np.clip(1.0 - tau, 1e-9, None)).sum(axis=1) > 1.0


def snap_to_words(text: str, spans: list[Span]) -> list[Span]:
    """Trim edge punctuation and extend spans that end inside a word to the whole word.

    Spans without a letter or digit are dropped. Spans that come to touch or overlap after
    extension are merged; the label of the more confident span is kept.
    """
    out: list[Span] = []
    for sp in sorted(spans, key=lambda x: x.start):
        s, e = sp.start, sp.end
        while s < e and (text[s].isspace() or text[s] in EDGE_PUNCT):
            s += 1
        while e > s and (text[e - 1].isspace() or text[e - 1] in EDGE_PUNCT):
            e -= 1
        while 0 < s < e and text[s - 1].isalnum() and text[s].isalnum():
            s -= 1
        while s < e < len(text) and text[e].isalnum() and text[e - 1].isalnum():
            e += 1
        if e <= s or not any(ch.isalnum() for ch in text[s:e]):
            continue  # nothing to redact: spans of punctuation alone carry no personal information
        if out and s < out[-1].end:
            prev = out.pop()
            best = prev if prev.score >= sp.score else sp
            out.append(Span(prev.start, max(prev.end, e), best.label, max(prev.score, sp.score), prev.source))
        else:
            out.append(Span(s, e, sp.label, sp.score, sp.source))
    return out


def timed_detect(detector, text: str) -> tuple[list[Span], float]:
    """Run ``detector.detect`` and return (spans, milliseconds)."""
    t0 = time.perf_counter()
    spans = detector.detect(text)
    return spans, (time.perf_counter() - t0) * 1000
