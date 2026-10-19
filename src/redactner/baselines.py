"""Off-the-shelf comparison systems, wrapped as span detectors with the same output as the model.

* **GLiNER-PII** (``nvidia/gliner-PII``): a zero-shot span extractor (GLiNER on DeBERTa-v3-large)
  fine-tuned for PII on Nemotron-PII. It is prompted with label names from that dataset, which are
  mapped onto our categories. Long documents are read in overlapping windows that fit the model's
  384-token input.
* **spaCy** ``en_core_web_trf``: a general-purpose named entity recogniser (RoBERTa-base trained on
  OntoNotes 5). Its person, organisation, place and date entities are mapped onto our categories; it
  has no notion of contact details or identification numbers.

Both packages belong to the optional ``experiments`` dependency group (``uv sync --group experiments``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .detector import resolve_overlaps
from .inference import default_device
from .types import Span

# GLiNER-PII prompt label -> category (label names follow the Nemotron-PII schema the model was trained on)
GLINER_LABELS = {
    "first_name": "PERSON", "last_name": "PERSON", "name": "PERSON",
    "email": "EMAIL", "phone_number": "PHONE", "fax_number": "PHONE",
    "street_address": "ADDRESS", "postcode": "ADDRESS",
    "city": "LOCATION", "state": "LOCATION", "county": "LOCATION", "coordinate": "LOCATION",
    "date": "DATE", "date_of_birth": "DATE",
    "ssn": "ID_NUMBER", "national_id": "ID_NUMBER", "tax_id": "ID_NUMBER", "medical_record_number": "ID_NUMBER",
    "customer_id": "ID_NUMBER", "employee_id": "ID_NUMBER", "certificate_license_number": "ID_NUMBER",
    "unique_id": "ID_NUMBER", "license_plate": "ID_NUMBER",
    "account_number": "FINANCIAL", "credit_debit_card": "FINANCIAL", "bank_routing_number": "FINANCIAL",
    "swift_bic": "FINANCIAL",
    "user_name": "USERNAME", "password": "CREDENTIAL", "pin": "CREDENTIAL", "cvv": "CREDENTIAL",
    "api_key": "CREDENTIAL",
    "ipv4": "IP_ADDRESS", "ipv6": "IP_ADDRESS", "mac_address": "IP_ADDRESS", "url": "URL",
    "company_name": "ORGANIZATION",
}

# spaCy OntoNotes entity type -> category; all other types (money, quantities, nationalities, ...) are ignored
SPACY_LABELS = {"PERSON": "PERSON", "ORG": "ORGANIZATION", "GPE": "LOCATION", "LOC": "LOCATION",
                "FAC": "LOCATION", "DATE": "DATE"}


# GLiNER's own word splitter: words (with inner hyphens or underscores) and single punctuation marks
GLINER_TOKEN = re.compile(r"\w+(?:[-_]\w+)*|\S")


def word_windows(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Character ranges of overlapping windows of ``size`` GLiNER tokens (``overlap`` tokens shared)."""
    words = [m.span() for m in GLINER_TOKEN.finditer(text)]
    if not words:
        return []
    out, step = [], max(1, size - overlap)
    for i in range(0, len(words), step):
        out.append((words[i][0], words[min(i + size, len(words)) - 1][1]))
        if i + size >= len(words):
            break
    return out


def finalise(spans: Iterable[Span], threshold: float = 0.0) -> list[Span]:
    """Keep spans scoring at least ``threshold`` and merge overlaps (e.g. from overlapping windows)."""
    return resolve_overlaps([s for s in spans if s.score >= threshold])


class GlinerDetector:
    name = "GLiNER-PII"

    def __init__(self, model_name: str = "nvidia/gliner-PII", device: str | None = None, threshold: float = 0.3,
                 window_words: int = 350, overlap_words: int = 60, batch_size: int = 8) -> None:
        from gliner import GLiNER

        self.model = GLiNER.from_pretrained(model_name).to(device or default_device()).eval()
        self.threshold = threshold
        self.window_words, self.overlap_words, self.batch_size = window_words, overlap_words, batch_size

    def raw_predictions(self, texts: Sequence[str], min_score: float = 0.1) -> list[list[Span]]:
        """All spans scoring at least ``min_score``, before overlap resolution (for threshold tuning)."""
        chunks = [(i, s, e) for i, text in enumerate(texts)
                  for s, e in word_windows(text, self.window_words, self.overlap_words)]
        results = self.model.inference([texts[i][s:e] for i, s, e in chunks], list(GLINER_LABELS),
                                       threshold=min_score, batch_size=self.batch_size)
        out: list[list[Span]] = [[] for _ in texts]
        for (i, offset, _), entities in zip(chunks, results):
            for ent in entities:
                if ent["end"] > ent["start"]:
                    out[i].append(Span(offset + ent["start"], offset + ent["end"], GLINER_LABELS[ent["label"]],
                                       round(float(ent["score"]), 4), "gliner"))
        return out

    def detect_batch(self, texts: Sequence[str], threshold: float | None = None) -> list[list[Span]]:
        tau = self.threshold if threshold is None else threshold
        return [finalise(spans, tau) for spans in self.raw_predictions(texts, min_score=tau)]


class SpacyDetector:
    name = "spaCy (en_core_web_trf)"

    def __init__(self, model_name: str = "en_core_web_trf", batch_size: int = 16) -> None:
        import spacy

        # only the transformer and the entity recogniser are needed
        self.nlp = spacy.load(model_name, exclude=["tagger", "parser", "attribute_ruler", "lemmatizer"])
        self.batch_size = batch_size

    def detect_batch(self, texts: Sequence[str]) -> list[list[Span]]:
        out = []
        for doc in self.nlp.pipe(texts, batch_size=self.batch_size):
            out.append([Span(ent.start_char, ent.end_char, SPACY_LABELS[ent.label_], 1.0, "spacy")
                        for ent in doc.ents if ent.label_ in SPACY_LABELS and ent.end_char > ent.start_char])
        return out


def save_predictions(path, docs: Sequence[dict], preds: Sequence[Sequence[Span]]) -> None:
    """Cache predictions as JSON lines (one document per line, keyed by document id)."""
    import json
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for doc, spans in zip(docs, preds):
            fh.write(json.dumps({"id": doc["id"], "spans": [[s.start, s.end, s.label, s.score] for s in spans]}) + "\n")


def load_predictions(path, docs: Sequence[dict], source: str) -> list[list[Span]] | None:
    """Cached predictions for exactly these documents, or ``None`` when the cache is missing or stale."""
    import json
    from pathlib import Path

    if not Path(path).exists():
        return None
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    if [r["id"] for r in rows] != [d["id"] for d in docs]:
        return None
    return [[Span(s, e, lab, score, source) for s, e, lab, score in r["spans"]] for r in rows]
