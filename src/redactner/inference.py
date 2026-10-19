"""Neural PII detector: a fine-tuned token classifier applied with document-scale sliding windows.

* **Sliding windows (M3):** the whole document is tokenised once into overlapping windows;
  probabilities of tokens seen by several windows are averaged (tokens are keyed by character
  offsets, which are identical across windows).
* **Cost-sensitive decoding (M4):** a token is PII when ``P(O) < tau``; the span type is the argmax
  of the type probabilities averaged over the span (span-level voting), so one name cannot split
  into PERSON + USERNAME. ``decoding="argmax"`` gives the plain baseline behaviour.
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

META_FILE = "redactner_meta.json"


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
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
        self.model = AutoModelForTokenClassification.from_pretrained(self.model_dir).to(self.device).eval()
        self.threshold = threshold if threshold is not None else float(self.meta.get("threshold", 0.5))
        self.decoding = decoding
        self.max_len = max_len or int(self.meta.get("max_len", 512))
        self.stride = stride or int(self.meta.get("stride", 128))
        self.batch_size = batch_size
        self.name = self.meta.get("run_name", self.model_dir.name)

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

    def decode(self, text: str, tp: TokenProbs, threshold: float | None = None) -> list[Span]:
        tau = self.threshold if threshold is None else threshold
        type_probs = np.stack([tp.probs[:, cols].sum(axis=1) for cols in self.type_cols], axis=1)
        p_o = tp.probs[:, self.o_index]
        if self.decoding == "argmax":
            flagged = tp.probs.argmax(axis=1) != self.o_index
        else:
            flagged = p_o < tau

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
            t = self.types[int(type_probs[i].argmax())]
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
        return spans


def timed_detect(detector, text: str) -> tuple[list[Span], float]:
    """Run ``detector.detect`` and return (spans, milliseconds)."""
    t0 = time.perf_counter()
    spans = detector.detect(text)
    return spans, (time.perf_counter() - t0) * 1000
