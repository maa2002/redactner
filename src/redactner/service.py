"""The application's operations: open a document, analyse it, apply the reviewed redactions.

The desktop window reaches this class through :mod:`redactner.bridge`, inside the process. Nothing
here knows about windows or networks, which keeps the rules in one place and easy to test:

* every input is checked here, whatever sent it;
* opened documents live in memory only, under a byte budget, and are forgotten after a long time
  without use. A background timer enforces that, not just the next request that happens to arrive;
* a manual redaction that cannot be applied is an error, never silently dropped.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .docx_engine import DocxDocument, DocxError, DocxRefused
from .labels import DEFAULT_PRESET, LABELS, PRESETS, label_info
from .pipeline import RedactionPipeline
from .types import MAX_TEXT_CHARS

MODES = ("label", "mask")
MIN_CONFIDENCE, MAX_CONFIDENCE = 0.001, 0.99  # the page's slider; 0.001 corresponds to tau = 0.999
MAX_TERMS, MAX_TERM_CHARS = 200, 200
MAX_EXTRA_SPANS = 5_000
MAX_EXCLUDED = 200_000
MAX_FILENAME_CHARS = 200
IDLE_SECONDS = 8 * 3600  # a document left open and untouched for a working day is forgotten
USER_LABELS = (*LABELS, "CUSTOM")


class ServiceError(Exception):
    """A request that cannot be carried out. ``status`` follows the HTTP meaning (404, 409, 413, 415, 422)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def to_tau(min_confidence: float | None) -> float | None:
    """The page speaks in *minimum confidence* (1 - P(O)); the decoder uses tau on P(O): tag when P(O) < tau."""
    return None if min_confidence is None else round(1.0 - min_confidence, 4)


# -- input checks ---------------------------------------------------------------------------------
def _fail(what: str) -> ServiceError:
    return ServiceError(422, f"Invalid request: {what}.")


def check_confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not MIN_CONFIDENCE <= value <= MAX_CONFIDENCE:
        raise _fail(f"threshold must be a number from {MIN_CONFIDENCE} to {MAX_CONFIDENCE}")
    return float(value)


def check_labels(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or any(label not in LABELS for label in value):
        raise _fail("labels must be a list of known categories")
    return list(value)


def check_terms(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if (not isinstance(value, (list, tuple)) or len(value) > MAX_TERMS
            or any(not isinstance(term, str) or len(term) > MAX_TERM_CHARS for term in value)):
        raise _fail(f"{name} must be at most {MAX_TERMS} short text entries")
    return list(value)


def check_mode(value: Any) -> str:
    if value not in MODES:
        raise _fail("mode must be label or mask")
    return value


def check_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if (not isinstance(value, (list, tuple)) or len(value) > MAX_EXCLUDED
            or any(not isinstance(item, str) or len(item) > 64 for item in value)):
        raise _fail("exclude_ids must be a list of entity ids")
    return list(value)


def check_extra_spans(value: Any, segment_lengths: Mapping[str, int]) -> list[dict]:
    """Manual redactions must point at real text with a known category; nothing is dropped silently."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_EXTRA_SPANS:
        raise _fail(f"extra_spans must be a list of at most {MAX_EXTRA_SPANS} items")
    spans = []
    for item in value:
        if not isinstance(item, Mapping):
            raise _fail("each extra span must be an object")
        segment_id, start, end, label = (item.get(key) for key in ("segment_id", "start", "end", "label"))
        if label not in USER_LABELS:
            raise _fail("an extra span has an unknown label")
        if not isinstance(segment_id, str) or segment_id not in segment_lengths:
            raise _fail("an extra span refers to a paragraph that does not exist")
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int)
                or not 0 <= start < end <= segment_lengths[segment_id]):
            raise _fail("an extra span lies outside its paragraph")
        spans.append({"segment_id": segment_id, "start": start, "end": end, "label": label})
    return spans


# -- in-memory document store ---------------------------------------------------------------------
@dataclass
class Entry:
    data: bytes
    filename: str
    doc: DocxDocument
    used: float = field(default_factory=time.time)  # when the document was last asked for
    analysis: dict | None = None
    analysis_id: str | None = None
    redacted: bytes | None = None
    report: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)  # one analyse or redact at a time per document

    @property
    def size(self) -> int:
        """Rough memory held for this document: the parsed parts dominate, not the compressed upload."""
        package = getattr(self.doc, "_package", None)
        unpacked = sum(len(raw) for raw in package.raw.values()) if package is not None else len(self.data) * 12
        text = sum(len(segment.text) for segment in self.doc.segments)
        analysed = 8 * text + 400 * len(self.analysis["entities"]) if self.analysis else 0
        return len(self.data) + 2 * unpacked + 4 * text + analysed + len(self.redacted or b"")


class DocumentStore:
    """Opened documents, kept in memory only, under a byte budget and a limit on idle time.

    The limit counts from the last use, so a long review is never cut off while it is being worked on.
    """

    def __init__(self, ttl_seconds: float = IDLE_SECONDS, budget_bytes: int = 600 * 2**20,
                 sweep_seconds: float = 30.0) -> None:
        self.ttl = ttl_seconds
        self.budget = budget_bytes
        self._items: dict[str, Entry] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        # expired documents leave memory even when no further request arrives
        self._sweeper = threading.Thread(target=self._sweep, args=(sweep_seconds,), name="redactner-store", daemon=True)
        self._sweeper.start()

    def _sweep(self, interval: float) -> None:
        while not self._closed.wait(interval):
            with self._lock:
                self._purge()

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, v in self._items.items() if now - v.used > self.ttl]:
            del self._items[key]

    def put(self, entry: Entry) -> str:
        with self._lock:
            self._purge()
            if entry.size > self.budget:
                raise ServiceError(413, "The document is too large to hold in memory")
            while self._items and sum(e.size for e in self._items.values()) + entry.size > self.budget:
                del self._items[min(self._items, key=lambda k: self._items[k].used)]  # least recently used first
            doc_id = uuid.uuid4().hex
            self._items[doc_id] = entry
            return doc_id

    def get(self, doc_id: str) -> Entry:
        with self._lock:
            self._purge()
            entry = self._items.get(doc_id) if isinstance(doc_id, str) else None
            if entry is None:
                raise ServiceError(404, "Document not found or expired.")
            entry.used = time.time()
            return entry

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            return self._items.pop(doc_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def close(self) -> None:
        self._closed.set()
        self.clear()


# -- the service ----------------------------------------------------------------------------------
class RedactionService:
    """Upload, analyse, review and redact documents with the fine-tuned model behind ``detector``."""

    def __init__(self, detector, *, max_upload_mb: int = 20) -> None:
        self.pipeline = RedactionPipeline(detector)
        self.store = DocumentStore()
        self.max_upload_mb = max_upload_mb
        self.max_upload_bytes = max_upload_mb * 2**20
        self._infer_lock = threading.Lock()  # one model, so inference runs one document at a time
        model = detector.model
        self.model_info = {"name": getattr(model, "name", "model"),
                           "device": getattr(getattr(model, "device", None), "type", "cpu"),
                           "threshold": float(getattr(model, "threshold", 0.5)),
                           "category_thresholds": bool(getattr(model, "category_thresholds", None))}

    def close(self) -> None:
        self.store.close()

    # -- information ------------------------------------------------------------------------------
    def health(self) -> dict:
        return {"status": "ok", "model": self.model_info}

    def config(self) -> dict:
        default_threshold = round(1.0 - self.model_info["threshold"], 3)  # as minimum confidence
        return {
            "labels": label_info(),  # Always Redact terms come back as CUSTOM and are always applied
            "presets": PRESETS, "default_preset": DEFAULT_PRESET,
            "modes": [{"id": "label", "name": "Labels ([PERSON_1])"}, {"id": "mask", "name": "Black-Out (█████)"}],
            "default_threshold": default_threshold, "min_threshold": MIN_CONFIDENCE, "max_threshold": MAX_CONFIDENCE,
            "tuned_thresholds": self.model_info["category_thresholds"],
            "max_upload_mb": self.max_upload_mb, "model": self.model_info,
        }

    # -- documents --------------------------------------------------------------------------------
    def _open(self, data: bytes) -> DocxDocument:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ServiceError(400, "Empty upload. Send the .docx file itself.")
        if len(data) > self.max_upload_bytes:
            raise ServiceError(413, f"File is larger than {self.max_upload_mb} MB.")
        try:
            return DocxDocument.from_bytes(bytes(data), max_bytes=self.max_upload_bytes)
        except DocxRefused as exc:
            raise ServiceError(415, f"Document refused: {exc}.") from exc
        except DocxError as exc:
            raise ServiceError(422, f"Not a readable .docx file: {exc}.") from exc

    def upload(self, data: bytes, filename: str = "document.docx") -> dict:
        doc = self._open(data)
        characters = sum(len(segment.text) + 1 for segment in doc.segments)
        if characters > MAX_TEXT_CHARS:  # said now, not as a failure halfway through the analysis
            raise ServiceError(413, f"The document has too much text to analyse ({characters:,} characters; "
                                    f"the limit is {MAX_TEXT_CHARS:,}).")
        name =(filename if isinstance(filename, str) and filename else "document.docx")[:MAX_FILENAME_CHARS]
        doc_id = self.store.put(Entry(data=bytes(data), filename=name, doc=doc))
        return {"doc_id": doc_id, "filename": name, "stats": doc.stats, "warnings": doc.warnings}

    def analyze(self, doc_id: str, *, labels: Any = None, threshold: Any = None, deny_terms: Any = None,
                allow_terms: Any = None) -> dict:
        """Find personal information; ``threshold`` is the minimum model confidence chosen in the page."""
        labels, confidence = check_labels(labels), check_confidence(threshold)
        deny, allow = check_terms(deny_terms, "deny_terms"), check_terms(allow_terms, "allow_terms")
        entry = self.store.get(doc_id)
        with entry.lock:
            with self._infer_lock:
                analysis = self.pipeline.analyze(entry.doc, labels=labels, threshold=to_tau(confidence),
                                                 deny_terms=deny, allow_terms=allow)
            entry.analysis_id = analysis["analysis_id"] = uuid.uuid4().hex
            entry.analysis, entry.redacted, entry.report = analysis, None, None
        return analysis

    def redact(self, doc_id: str, *, mode: Any = "label", exclude_ids: Any = None, extra_spans: Any = None,
               analysis_id: Any = None) -> dict:
        """Apply the reviewed detections. ``analysis_id`` (optional) guards against a review of an older analysis."""
        mode, excluded = check_mode(mode), check_ids(exclude_ids)
        entry = self.store.get(doc_id)
        with entry.lock:
            if entry.analysis is None:
                raise ServiceError(409, "Analyse the document first.")
            if analysis_id is not None and analysis_id != entry.analysis_id:
                raise ServiceError(409, "The document was analysed again after this review. Review the new result.")
            lengths = {segment.id: len(segment.text) for segment in entry.doc.segments}
            extra = check_extra_spans(extra_spans, lengths)
            data, report = self.pipeline.redact(entry.doc, entry.analysis["entities"], mode, excluded, extra)
            report["warnings"] = sorted(set(report.get("warnings", [])) | set(entry.doc.warnings))
            entry.redacted, entry.report = data, report
        return {"report": report}

    def redacted_document(self, doc_id: str) -> bytes:
        entry = self.store.get(doc_id)
        if entry.redacted is None:
            raise ServiceError(409, "Nothing has been redacted yet.")
        return entry.redacted

    def report(self, doc_id: str) -> dict:
        entry = self.store.get(doc_id)
        if entry.report is None:
            raise ServiceError(409, "Nothing has been redacted yet.")
        return entry.report

    def delete(self, doc_id: str) -> dict:
        return {"deleted": self.store.delete(doc_id)}
